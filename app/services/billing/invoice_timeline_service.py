from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app

from app.extensions import db
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception

try: # pragma: no cover - optional dependency in some deployments
  from app.services.billing.invoice_services import InvoiceService

  _INVOICE_SERVICE_AVAILABLE = True
except Exception: # pragma: no cover
  InvoiceService = None # type: ignore
  _INVOICE_SERVICE_AVAILABLE = False

try: # pragma: no cover - optional dependency in some deployments
  from app.models.matter import Matter, MatterMemo
  from app.models.legacy_finance import ExternalInvoiceCaseMap

  _LEGACY_MODELS_AVAILABLE = True
except Exception: # pragma: no cover
  Matter = None # type: ignore
  MatterMemo = None # type: ignore
  ExternalInvoiceCaseMap = None # type: ignore
  _LEGACY_MODELS_AVAILABLE = False


_ALLOWED_ACTIONS = {
  "invoice.create",
  "invoice.publish",
  "invoice.status_change",
  "invoice.tax_issued",
  "invoice.payment.verify",
  "invoice.payment.force_paid",
  "invoice.mark_paid",
  "invoice.payment.unverify",
  "invoice.deposit.apply",
  "invoice.deposit.cancel_apply",
}

_STATUS_LABELS = {
  "draft": "Draft",
  "sent": "Issued",
  "void": "Void",
  "tax_issued": "Tax documentation",
  "cash_issued": "Payment receipt",
  "processed": "Tax recorded",
  "pre_overdue": "Advanced cost",
  "paid": "Paid",
  "unpaid": "Unpaid",
  "pending": "Payment pending",
  "payment_pending": "Payment pending",
}


def _timeline_to_case_memo_enabled() -> bool:
  return ConfigService.get_bool(
    "INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED",
    current_app.config.get("INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED", False),
  )


def _parse_meta(meta: Any) -> Dict[str, Any]:
  if isinstance(meta, dict):
    return meta
  if isinstance(meta, str):
    try:
      return json.loads(meta)
    except Exception:
      return {}
  return {}


def _human_status(value: Optional[str]) -> Optional[str]:
  if not value:
    return None
  v = str(value).strip().lower()
  return _STATUS_LABELS.get(v, value)


def _format_amount(invoice: Dict[str, Any]) -> Optional[str]:
  total = invoice.get("total")
  if total is None:
    return None
  currency = (invoice.get("currency") or "USD").upper()
  try:
    amt = float(total)
    return f"{amt:,.0f} {currency}"
  except Exception:
    return f"{total} {currency}".strip()


def _format_date_block(invoice: Dict[str, Any]) -> str:
  issue = (invoice.get("issue_date") or "").strip()
  due = (invoice.get("due_date") or "").strip()
  parts = []
  if issue:
    parts.append(f"Issued {issue}")
  if due:
    parts.append(f"due {due}")
  if not parts:
    return ""
  return "(" + ", ".join(parts) + ")"


def _format_summary(invoice: Dict[str, Any]) -> str:
  number = (invoice.get("number") or "").strip()
  if not number:
    number = f"#{invoice.get('id')}" if invoice.get("id") else "#New"
  amount = _format_amount(invoice)
  date_block = _format_date_block(invoice)
  summary = number
  if amount:
    summary += f" - {amount}"
  if date_block:
    summary += f" {date_block}"
  return summary


def _build_message(action: str, invoice: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
  summary = _format_summary(invoice)
  number = (invoice.get("number") or "").strip() or f"#{invoice.get('id')}"
  if action == "invoice.create":
    return f"[Invoice] Draft : {summary}"
  if action == "invoice.publish":
    return f"[Invoice] Issued: {summary}"
  if action == "invoice.status_change":
    old_status = _human_status(meta.get("old_status"))
    new_status = _human_status(meta.get("new_status"))
    if old_status or new_status:
      return f"[Invoice] status change: {number} - {old_status or '-'} to {new_status or '-'}"
    return f"[Invoice] status change: {summary}"
  if action == "invoice.tax_issued":
    return f"[Invoice] Tax documentation recorded: {number}"
  if action == "invoice.payment.verify":
    ok = meta.get("ok")
    reason = (meta.get("reason") or "").strip()
    if ok is True:
      return f"[Invoice] Payment verified: {number}"
    if ok is False:
      return f"[Invoice] Payment verification failed: {number}" + (f" ({reason})" if reason else "")
    return f"[Invoice] Payment verification pending: {number}"
  if action == "invoice.payment.force_paid":
    return f"[Invoice] Payment force-marked paid: {number}"
  if action == "invoice.mark_paid":
    return f"[Invoice] Marked paid: {number}"
  if action == "invoice.payment.unverify":
    return f"[Invoice] Payment verification reversed: {number}"
  if action == "invoice.deposit.apply":
    return f"[Invoice] Retainer applied: {number}"
  if action == "invoice.deposit.cancel_apply":
    return f"[Invoice] Retainer application canceled: {number}"
  return None


def _resolve_matter_ids(invoice_id: int, invoice: Dict[str, Any]) -> List[str]:
  if not _LEGACY_MODELS_AVAILABLE:
    return []

  matter_ids: List[str] = []
  seen = set()

  def _add(mid: Optional[str]) -> None:
    if not mid:
      return
    m = str(mid).strip()
    if not m or m in seen:
      return
    seen.add(m)
    matter_ids.append(m)

  if ExternalInvoiceCaseMap is not None:
    rows = (
      db.session.query(ExternalInvoiceCaseMap.matter_id)
      .filter(ExternalInvoiceCaseMap.external_invoice_id == int(invoice_id))
      .filter(
        (ExternalInvoiceCaseMap.is_deleted == False) # noqa: E712
        | (ExternalInvoiceCaseMap.is_deleted.is_(None))
      )
      .all()
    )
    for row in rows:
      _add(getattr(row, "matter_id", None))

  _add(invoice.get("ipm_case_id"))
  if not matter_ids:
    ref = (invoice.get("ipm_case_ref") or "").strip()
    if ref and Matter is not None:
      row = (
        db.session.query(Matter.matter_id)
        .filter(Matter.our_ref == ref)
        .filter((Matter.is_deleted.is_(False)) | (Matter.is_deleted.is_(None)))
        .first()
      )
      if row:
        _add(getattr(row, "matter_id", None))

  if not matter_ids or Matter is None:
    return matter_ids

  valid_rows = (
    db.session.query(Matter.matter_id)
    .filter(Matter.matter_id.in_(matter_ids))
    .filter((Matter.is_deleted.is_(False)) | (Matter.is_deleted.is_(None)))
    .all()
  )
  valid = {r.matter_id for r in valid_rows if getattr(r, "matter_id", None)}
  return [m for m in matter_ids if m in valid]


def record_invoice_timeline_event(
  *,
  action: str,
  invoice_id: Optional[int],
  meta: Any = None,
  request_id: Optional[str] = None,
  actor_id: Optional[int] = None,
  actor_name: Optional[str] = None,
) -> None:
  if not invoice_id or action not in _ALLOWED_ACTIONS:
    return
  # Case-view "Matter Notes" is a user memo area.
  # Keep invoice audit/timeline from auto-appending there unless explicitly enabled.
  if not _timeline_to_case_memo_enabled():
    return
  if not _INVOICE_SERVICE_AVAILABLE or not _LEGACY_MODELS_AVAILABLE:
    return

  try:
    invoice = InvoiceService.get_by_id(int(invoice_id)) if InvoiceService else None
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="invoice_timeline_service.fetch_invoice",
      log_key="invoice_timeline_service.fetch_invoice",
      log_window_seconds=300,
    )
    return
  if not invoice:
    return

  try:
    meta_obj = _parse_meta(meta)
    message = _build_message(action, invoice, meta_obj)
    if not message:
      return
    matter_ids = _resolve_matter_ids(int(invoice_id), invoice)
    if not matter_ids or MatterMemo is None:
      return

    for mid in matter_ids:
      memo = MatterMemo(
        matter_id=mid,
        body=message,
        created_by_id=actor_id,
        created_by_name=actor_name,
      )
      db.session.add(memo)

    in_tx = False
    try:
      in_tx_fn = getattr(db.session, "in_transaction", None)
      if callable(in_tx_fn):
        in_tx = bool(in_tx_fn())
      else:
        get_tx = getattr(db.session, "get_transaction", None)
        in_tx = bool(get_tx()) if callable(get_tx) else False
    except Exception:
      in_tx = False

    if in_tx:
      with db.session.begin_nested():
        db.session.flush()
    else:
      db.session.commit()
  except Exception as exc:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_timeline_service.record_event.rollback",
        log_key="invoice_timeline_service.record_event.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context="invoice_timeline_service.record_event",
      log_key="invoice_timeline_service.record_event",
      log_window_seconds=300,
    )
    if current_app.config.get("TESTING"):
      raise
