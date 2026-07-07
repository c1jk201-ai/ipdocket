from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.extensions import db
from app.services.ops.operation_context import OperationContext
from app.utils.error_logging import report_swallowed_exception

from ..auth import log_audit
from ..db import (
  build_client_deposit_audit_meta,
  cancel_uncanceled_deposit_applies_for_invoice,
  row_to_dict,
)

_FINALIZED_BILLING_STATUSES = {"tax_issued", "cash_issued", "processed"}


@dataclass(frozen=True)
class InvoiceDeleteHooks:
  resolve_billing_status: Callable[[Any], str]
  resolve_payment_status: Callable[[Any], str]


@dataclass(frozen=True)
class InvoiceDeleteResult:
  deleted_invoice_ids: list[int]
  deleted_numbers: list[str]
  snapshots: list[dict[str, Any]]
  canceled_deposit_entries: list[dict[str, Any]]


class InvoiceDeleteError(Exception):
  def __init__(self, message: str, *, invoice_id: int | None = None) -> None:
    super().__init__(message)
    self.message = message
    self.invoice_id = invoice_id


class InvoiceDeleteNotFoundError(InvoiceDeleteError):
  pass


class InvoiceDeleteBlockedError(InvoiceDeleteError):
  pass


class InvoiceDeleteExecutionError(InvoiceDeleteError):
  pass


def _load_invoice_for_delete(conn, invoice_id: int) -> dict[str, Any] | None:
  row = conn.execute(
    "SELECT id, number, status, billing_status, payment_status, payment_verified FROM invoices WHERE id=?",
    (int(invoice_id),),
  ).fetchone()
  if not row:
    return None
  return row_to_dict(row)


def _build_snapshot(invoice: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(invoice["id"]),
    "number": invoice["number"],
    "status": invoice.get("status"),
    "billing_status": invoice.get("billing_status"),
    "payment_status": invoice.get("payment_status"),
  }


def _ensure_invoice_deletable(
  invoice: dict[str, Any], hooks: InvoiceDeleteHooks, *, invoice_id: int
) -> None:
  try:
    billing_status = hooks.resolve_billing_status(invoice)
  except Exception:
    billing_status = ((invoice.get("billing_status") or "")).strip().lower()
  try:
    payment_status = hooks.resolve_payment_status(invoice)
  except Exception:
    payment_status = ((invoice.get("payment_status") or "")).strip().lower()
  try:
    payment_verified = int(invoice.get("payment_verified") or 0)
  except Exception:
    payment_verified = 0
  legacy_status = ((invoice.get("status") or "")).strip().lower()

  if (
    billing_status in _FINALIZED_BILLING_STATUSES
    or legacy_status in _FINALIZED_BILLING_STATUSES
  ):
    raise InvoiceDeleteBlockedError(
      "A tax-recorded invoice cannot be deleted. Change the billing status first.",
      invoice_id=invoice_id,
    )
  if payment_status == "paid" or legacy_status == "paid" or payment_verified == 1:
    raise InvoiceDeleteBlockedError(
      "A paid invoice cannot be deleted. Cancel or reverse the payment status first.",
      invoice_id=invoice_id,
    )


def delete_invoices(
  conn,
  invoice_ids: list[int | str],
  hooks: InvoiceDeleteHooks,
  *,
  created_by_user_id: int | None,
  skip_missing: bool,
  error_context: str,
) -> InvoiceDeleteResult:
  normalized_ids: list[int] = []
  seen_ids: set[int] = set()
  for raw_invoice_id in invoice_ids:
    invoice_id = int(raw_invoice_id)
    if invoice_id in seen_ids:
      continue
    seen_ids.add(invoice_id)
    normalized_ids.append(invoice_id)

  snapshots: list[dict[str, Any]] = []
  deleted_numbers: list[str] = []
  for invoice_id in normalized_ids:
    invoice = _load_invoice_for_delete(conn, invoice_id)
    if not invoice:
      if skip_missing:
        continue
      raise InvoiceDeleteNotFoundError("Invoice not found", invoice_id=invoice_id)
    _ensure_invoice_deletable(invoice, hooks, invoice_id=invoice_id)
    snapshots.append(_build_snapshot(invoice))
    deleted_numbers.append(str(invoice.get("number") or invoice_id))

  canceled_deposit_entries: list[dict[str, Any]] = []
  try:
    if not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")
  except Exception as begin_exc:
    report_swallowed_exception(
      begin_exc,
      context=f"{error_context}.begin_immediate",
      log_key=f"{error_context}.begin_immediate",
      log_window_seconds=300,
    )

  try:
    for snapshot in snapshots:
      invoice_id = int(snapshot["id"])
      try:
        canceled = cancel_uncanceled_deposit_applies_for_invoice(
          conn,
          invoice_id,
          memo="auto_cancel_on_delete",
          created_by=created_by_user_id,
          begin_immediate=False,
          commit_if_started=False,
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context=f"{error_context}.cancel_deposit_before_delete",
          log_key=f"{error_context}.cancel_deposit_before_delete",
          log_window_seconds=300,
        )
        raise InvoiceDeleteExecutionError(
          "Delete Retainer applicationCancel Auto Process failed. Retainer applicationCancel Delete.",
          invoice_id=invoice_id,
        ) from exc

      for entry in canceled or []:
        normalized_entry = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(normalized_entry, dict) and not normalized_entry.get(
          "related_invoice_id"
        ):
          normalized_entry["related_invoice_id"] = invoice_id
        if isinstance(normalized_entry, dict):
          canceled_deposit_entries.append(normalized_entry)

      conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    conn.commit()
  except InvoiceDeleteExecutionError:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context=f"{error_context}.rollback",
        log_key=f"{error_context}.rollback",
        log_window_seconds=300,
      )
    raise
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context=f"{error_context}.rollback",
        log_key=f"{error_context}.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context=error_context,
      log_key=error_context,
      log_window_seconds=300,
    )
    raise InvoiceDeleteExecutionError(
      "Delete Error .  retry."
    ) from exc

  return InvoiceDeleteResult(
    deleted_invoice_ids=[int(snapshot["id"]) for snapshot in snapshots],
    deleted_numbers=deleted_numbers,
    snapshots=snapshots,
    canceled_deposit_entries=canceled_deposit_entries,
  )


def log_invoice_delete_cancel_audits(
  canceled_deposit_entries: list[dict[str, Any]], *, error_context: str
) -> None:
  for entry in canceled_deposit_entries:
    try:
      audit_meta = build_client_deposit_audit_meta(
        entry_id=entry.get("cancel_entry_id"),
        business_profile_id=entry.get("business_profile_id"),
        client_id=entry.get("client_id"),
        currency=entry.get("currency"),
        amount_minor=entry.get("amount_minor"),
        entry_type="cancel_apply",
        memo=entry.get("memo"),
        related_invoice_id=entry.get("related_invoice_id"),
        related_entry_id=entry.get("apply_entry_id"),
        balance_before_minor=entry.get("balance_before_minor"),
        balance_after_minor=entry.get("balance_after_minor"),
      )
      log_audit(
        "invoice.deposit.cancel_apply",
        "invoice",
        entry.get("related_invoice_id"),
        audit_meta,
      )
    except Exception as log_exc:
      report_swallowed_exception(
        log_exc,
        context=f"{error_context}.audit",
        log_key=f"{error_context}.audit",
        log_window_seconds=300,
      )


def record_single_invoice_delete_operation(snapshot: dict[str, Any]) -> None:
  try:
    with db.session.begin():
      with OperationContext(
        action="invoice.delete",
        risk_level="HIGH",
        undo_supported=True,
        undo_deadline_at=datetime.utcnow() + timedelta(days=7),
        targets_json={"invoice_id": snapshot["id"], "number": snapshot["number"]},
        summary_json={"snapshot": snapshot},
        preop_backup_required=False,
      ) as op:
        op.add_change(
          entity_type="Invoice",
          entity_id=str(snapshot["id"]),
          change_type="delete",
          before=snapshot,
        )
        op.commit()
  except Exception:
    db.session.rollback()


def record_bulk_invoice_delete_operation(
  snapshots: list[dict[str, Any]], deleted_numbers: list[str]
) -> None:
  try:
    with db.session.begin():
      with OperationContext(
        action="invoice.bulk_delete",
        risk_level="HIGH",
        undo_supported=True,
        undo_deadline_at=datetime.utcnow() + timedelta(days=7),
        targets_json={"invoice_ids": [snapshot["id"] for snapshot in snapshots]},
        summary_json={
          "count": len(snapshots),
          "invoice_numbers": deleted_numbers,
        },
        preop_backup_required=False,
      ) as op:
        for snapshot in snapshots:
          op.add_change(
            entity_type="Invoice",
            entity_id=str(snapshot["id"]),
            change_type="delete",
            before=snapshot,
          )
        op.commit()
  except Exception:
    db.session.rollback()
