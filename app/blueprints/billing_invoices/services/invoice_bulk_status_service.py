from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.services.billing.tax_issue_types import FINAL_TAX_BILLING_STATUSES
from app.utils.error_logging import report_swallowed_exception

from ..auth import log_audit
from ..db import build_client_deposit_audit_meta, cancel_uncanceled_deposit_applies_for_invoice


@dataclass(frozen=True)
class BulkInvoiceStatusHooks:
  transition_allowed: Callable[[str, str, Any, str], bool]
  resolve_billing_status: Callable[[Any], str]
  resolve_payment_status: Callable[[Any], str]
  compute_billing_payment_from_status: Callable[[str, int], tuple[str, str]]
  derive_legacy_status_from_split: Callable[[str, str], str]
  sync_legacy_status: Callable[..., str]
  billing_transitions: Any
  payment_transitions: Any


@dataclass(frozen=True)
class BulkInvoiceStatusUpdateResult:
  updated_invoice_ids: list[int]
  updated_invoices: list[str]
  invalid_invoices: list[str]
  bank_activity_to_sync: list[str]


def _current_tax_issued_at(timezone_name: str) -> str:
  try:
    return datetime.now(ZoneInfo(timezone_name)).isoformat(timespec="seconds")
  except Exception:
    return datetime.now().isoformat()


def _log_void_cancel_deposit_audits(
  invoice_id: int, canceled_entries: list[dict[str, Any]]
) -> None:
  for entry in canceled_entries:
    try:
      audit_meta = build_client_deposit_audit_meta(
        entry_id=entry.get("cancel_entry_id"),
        business_profile_id=entry.get("business_profile_id"),
        client_id=entry.get("client_id"),
        currency=entry.get("currency"),
        amount_minor=entry.get("amount_minor"),
        entry_type="cancel_apply",
        memo=entry.get("memo"),
        related_invoice_id=invoice_id,
        related_entry_id=entry.get("apply_entry_id"),
        balance_before_minor=entry.get("balance_before_minor"),
        balance_after_minor=entry.get("balance_after_minor"),
      )
      log_audit("invoice.deposit.cancel_apply", "invoice", invoice_id, audit_meta)
    except Exception as log_exc:
      report_swallowed_exception(
        log_exc,
        context="billing_invoices.invoices_status.bulk_update_status.void.cancel_deposit.audit",
        log_key="billing_invoices.invoices_status.bulk_update_status.void.cancel_deposit.audit",
        log_window_seconds=300,
      )


def apply_bulk_invoice_status_update(
  conn,
  invoice_ids: list[str],
  *,
  mode: str,
  new_status: str,
  hooks: BulkInvoiceStatusHooks,
  created_by_user_id: int | None,
  timezone_name: str,
  tax_issue_type: str | None = None,
  tax_issue_source: str | None = None,
) -> BulkInvoiceStatusUpdateResult:
  updated_invoice_ids: list[int] = []
  updated_invoices: list[str] = []
  invalid_invoices: list[str] = []
  bank_activity_to_sync: list[str] = []

  for invoice_id in invoice_ids:
    row = conn.execute(
      "SELECT id, number, status, billing_status, currency, payment_verified, payment_status, is_outgoing FROM invoices WHERE id=?",
      (int(invoice_id),),
    ).fetchone()
    if not row:
      continue

    if mode == "payment":
      current_payment = hooks.resolve_payment_status(row)
      if not hooks.transition_allowed(
        current_payment, new_status, hooks.payment_transitions, "unpaid"
      ):
        invalid_invoices.append(row["number"])
        continue
      if (
        new_status == "paid"
        and (row["currency"] or "").upper() == "USD"
        and not int(row["payment_verified"] or 0)
      ):
        continue
      should_log_unverify = False
      if new_status in ("unpaid", "pending"):
        try:
          if int(row["payment_verified"] or 0) == 1 or (
            str(row["payment_status"]).lower() != new_status
          ):
            should_log_unverify = True
        except Exception:
          should_log_unverify = True
      if new_status in ("unpaid", "pending"):
        conn.execute(
          "UPDATE invoices SET payment_status=?, payment_verified=0 WHERE id=?",
          (new_status, int(invoice_id)),
        )
      else:
        conn.execute(
          "UPDATE invoices SET payment_status=? WHERE id=?",
          (new_status, int(invoice_id)),
        )
      hooks.sync_legacy_status(conn, int(invoice_id))
      updated_invoice_ids.append(int(invoice_id))
      updated_invoices.append(row["number"])
      if should_log_unverify:
        try:
          log_audit("invoice.payment.unverify", "invoice", int(invoice_id))
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.invoices_status.bulk_update_status.log_audit_unverify",
            log_key="billing_invoices.invoices_status.bulk_update_status.log_audit_unverify",
            log_window_seconds=300,
          )
      continue

    current_billing = hooks.resolve_billing_status(row)
    if not hooks.transition_allowed(
      current_billing, new_status, hooks.billing_transitions, "draft"
    ):
      invalid_invoices.append(row["number"])
      continue

    if new_status == "tax_issued":
      now_kst = _current_tax_issued_at(timezone_name)
      conn.execute(
        """
        UPDATE invoices
        SET tax_issued_at=?, billing_status=?,
          tax_issue_type=?, tax_issue_source=?
        WHERE id=?
        """,
        (
          now_kst,
          "tax_issued",
          tax_issue_type or "tax_invoice",
          tax_issue_source or "bulk_update",
          int(invoice_id),
        ),
      )
      hooks.sync_legacy_status(
        conn,
        int(invoice_id),
        billing_status="tax_issued",
        payment_status=hooks.resolve_payment_status(row),
      )
      updated_invoice_ids.append(int(invoice_id))
      updated_invoices.append(row["number"])
      continue

    if new_status == "void":
      try:
        if not conn.in_transaction:
          conn.execute("BEGIN IMMEDIATE")
      except Exception as begin_exc:
        report_swallowed_exception(
          begin_exc,
          context="billing_invoices.invoices_status.bulk_update_status.void.begin_immediate",
          log_key="billing_invoices.invoices_status.bulk_update_status.void.begin_immediate",
          log_window_seconds=300,
        )

      try:
        canceled_entries = cancel_uncanceled_deposit_applies_for_invoice(
          conn,
          int(invoice_id),
          memo="auto_cancel_on_void",
          created_by=created_by_user_id,
          begin_immediate=False,
          commit_if_started=False,
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices_status.bulk_update_status.void.cancel_deposit",
          log_key="billing_invoices.invoices_status.bulk_update_status.void.cancel_deposit",
          log_window_seconds=300,
        )
        invalid_invoices.append(row["number"])
        continue
      _log_void_cancel_deposit_audits(int(invoice_id), canceled_entries or [])

    billing_status, payment_status = hooks.compute_billing_payment_from_status(
      new_status, (row["payment_verified"] if row else 0)
    )
    legacy_status = hooks.derive_legacy_status_from_split(billing_status, payment_status)
    if new_status == "void":
      conn.execute(
        "UPDATE invoices SET status=?, billing_status=?, payment_status=?, payment_verified=0, payment_meta=NULL WHERE id=?",
        (legacy_status, billing_status, payment_status, int(invoice_id)),
      )
    else:
      conn.execute(
        "UPDATE invoices SET status=?, billing_status=?, payment_status=? WHERE id=?",
        (legacy_status, billing_status, payment_status, int(invoice_id)),
      )

    old_billing = str(row.get("billing_status") or row.get("status") or "").strip().lower()
    if old_billing in FINAL_TAX_BILLING_STATUSES:
      try:
        conn.execute(
          "UPDATE invoices SET tax_issued_at=NULL, tax_issue_type=NULL, tax_issue_source=NULL, tax_issue_note=NULL WHERE id=?",
          (int(invoice_id),),
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices_status.bulk_update_status.clear_tax_issued_at",
          log_key="billing_invoices.invoices_status.bulk_update_status.clear_tax_issued_at",
          log_window_seconds=300,
        )
      if row.get("number"):
        bank_activity_to_sync.append(row["number"])
    updated_invoice_ids.append(int(invoice_id))
    updated_invoices.append(row["number"])

  return BulkInvoiceStatusUpdateResult(
    updated_invoice_ids=updated_invoice_ids,
    updated_invoices=updated_invoices,
    invalid_invoices=invalid_invoices,
    bank_activity_to_sync=bank_activity_to_sync,
  )
