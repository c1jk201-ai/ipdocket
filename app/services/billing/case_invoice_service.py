from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models.ip_records import ExternalInvoiceCaseLink, ExternalInvoiceCaseMap, Matter
from app.services.billing.db_core import get_db as _billing_get_db
from app.services.billing.db_core import safe_json_parse
from app.services.billing.invoice_bridge import fetch_invoices_by_ids
from app.services.billing.utils import to_minor
from app.utils.error_logging import report_swallowed_exception

try:
  from app.services.billing.invoice_services import InvoiceService, PaymentService

  _SERVICES_AVAILABLE = True
except ImportError: # pragma: no cover
  _SERVICES_AVAILABLE = False


_BILLING_STATUS_META: dict[str, dict[str, str]] = {
  "draft": {"label": "Draft", "pill": "draft"},
  "sent": {"label": "Issued", "pill": "sent"},
  "tax_issued": {"label": "Tax recorded", "pill": "tax_issued"},
  "cash_issued": {"label": "Tax recorded", "pill": "tax_issued"},
  "processed": {"label": "Tax recorded", "pill": "tax_issued"},
  "pre_overdue": {"label": "Advanced cost", "pill": "pre_overdue"},
  "void": {"label": "Void", "pill": "void"},
}

_PAYMENT_STATUS_META: dict[str, dict[str, str]] = {
  "unpaid": {"label": "Unpaid", "pill": "unpaid"},
  "pending": {"label": "Payment pending", "pill": "pending"},
  "paid": {"label": "Paid", "pill": "paid"},
  "none": {"label": "-", "pill": "none"},
}


def _parse_date(value: Optional[str]) -> Optional[date]:
  if not value:
    return None
  try:
    return date.fromisoformat(str(value)[:10])
  except Exception:
    return None


def _item_is_estimated(item: Dict[str, Any]) -> bool:
  value = item.get("is_estimated")
  if value is None:
    return False
  try:
    return int(value) != 0
  except Exception:
    return bool(value)


def _summarize_line_items(items: List[Dict[str, Any]], *, max_items: int = 3) -> str:
  descriptions: List[str] = []
  for item in items:
    if _item_is_estimated(item):
      continue
    desc = str(item.get("description") or "").strip()
    if desc:
      descriptions.append(desc)
  if not descriptions:
    return ""
  if len(descriptions) <= max_items:
    return ", ".join(descriptions)
  extra = len(descriptions) - max_items
  return ", ".join(descriptions[:max_items]) + f" +{extra}"


def _first_meaningful_line(value: Any, *, max_length: int = 140) -> str:
  raw = str(value or "")
  if not raw:
    return ""
  line = ""
  for token in raw.replace("\r", "\n").split("\n"):
    candidate = token.strip()
    if not candidate:
      continue
    # Invoice notes often start with '*' bullet markers.
    candidate = candidate.lstrip("*").strip()
    if candidate:
      line = candidate
      break
  if not line:
    return ""
  if len(line) <= max_length:
    return line
  return line[: max_length - 1].rstrip() + "…"


def _resolve_invoice_title(row: Dict[str, Any], *, line_items_summary: str = "") -> str:
  for key in ("internal_reference", "title"):
    value = str(row.get(key) or "").strip()
    if value:
      return value
  summary = str(line_items_summary or "").strip()
  if summary:
    return summary
  return _first_meaningful_line(row.get("notes"))


def _parse_payment_meta_minor(meta_val: Any, currency: Optional[str]) -> int:
  """Best-effort parse of legacy payment_meta into minor units."""
  meta = safe_json_parse(meta_val, {})
  if not isinstance(meta, dict):
    return 0

  deposit = meta.get("deposit")
  if deposit is None and isinstance(meta.get("deposits"), list):
    total = 0
    for rec in meta.get("deposits") or []:
      if not isinstance(rec, dict):
        continue
      try:
        total += int(str(rec.get("deposit") or "0").replace(",", "").strip() or 0)
      except Exception:
        continue
    deposit = total

  if deposit is None:
    return 0

  cur = (meta.get("currency") or currency or "USD").strip().upper()
  if not cur:
    cur = "USD"
  if cur == "USD":
    try:
      return int(str(deposit or "0").replace(",", "").replace(" ", "") or 0)
    except Exception:
      return 0
  try:
    return int(to_minor(Decimal(str(deposit).replace(",", "")), cur))
  except Exception:
    return 0


def _active_link_filter(model):
  # is_deleted is Boolean in Postgres.
  # We compare with False (or 0 if using an adapter that maps 0 to False, but explicit False is safer for Boolean columns).
  return or_(model.is_deleted == False, model.is_deleted.is_(None)) # noqa: E712


def _fetch_invoice_ids_from_billing_db(*, matter_id: str, our_ref: Optional[str]) -> list[int]:
  """
  Fallback: invoice (invoices ) ipm_case_id / ipm_case_ref Link Invoice .
  Also checks external_invoice_case_map links from the matter view.
  """
  mid = (matter_id or "").strip()
  if not mid and not (our_ref or "").strip():
    return []
  try:
    conn = _billing_get_db()
  except Exception:
    return []
  try:
    our_ref_str = (our_ref or "").strip()
    if mid and our_ref_str:
      sql = (
        "SELECT id FROM invoices "
        "WHERE ipm_case_id = ? OR ipm_case_ref = ? "
        "ORDER BY id DESC"
      )
      params: list[str] = [mid, our_ref_str]
    elif mid:
      sql = "SELECT id FROM invoices WHERE ipm_case_id = ? ORDER BY id DESC"
      params = [mid]
    elif our_ref_str:
      sql = "SELECT id FROM invoices WHERE ipm_case_ref = ? ORDER BY id DESC"
      params = [our_ref_str]
    else:
      return []

    rows = conn.execute(sql, params).fetchall()
    out: list[int] = []
    for r in rows or []:
      try:
        out.append(int(r[0]))
      except Exception:
        try:
          out.append(int(r["id"])) # type: ignore[index]
        except Exception:
          continue
    return out
  except Exception:
    return []
  finally:
    try:
      conn.close()
    except Exception as exc:
      # Best-effort cleanup should not block invoice lookup fallback.
      report_swallowed_exception(
        exc,
        context="case_invoice_service._fetch_invoice_ids_from_billing_db.close_conn",
        log_key="case_invoice_service._fetch_invoice_ids_from_billing_db.close_conn",
        log_window_seconds=300,
      )


def fetch_case_invoice_ids(matter_id: str) -> list[int]:
  mid = (matter_id or "").strip()
  if not mid:
    return []

  ids: list[int] = []
  seen: set[int] = set()

  def _add(v: int) -> None:
    if v in seen:
      return
    seen.add(v)
    ids.append(v)

  # our_ref (ipm_case_ref fallback)
  our_ref: Optional[str] = None
  try:
    m = Matter.query.get(mid)
    if m:
      our_ref = (getattr(m, "our_ref", None) or "").strip() or None
  except Exception:
    our_ref = None

  if ExternalInvoiceCaseMap is not None:
    rows = (
      db.session.query(ExternalInvoiceCaseMap)
      .filter(ExternalInvoiceCaseMap.matter_id == mid)
      .filter(_active_link_filter(ExternalInvoiceCaseMap))
      .order_by(ExternalInvoiceCaseMap.id.desc())
      .all()
    )
    for r in rows:
      try:
        _add(int(r.external_invoice_id))
      except Exception:
        continue

  if ExternalInvoiceCaseLink is not None:
    rows = (
      db.session.query(ExternalInvoiceCaseLink)
      .filter(ExternalInvoiceCaseLink.matter_id == mid)
      .filter(_active_link_filter(ExternalInvoiceCaseLink))
      .order_by(ExternalInvoiceCaseLink.id.desc())
      .all()
    )
    for r in rows:
      try:
        _add(int(r.external_invoice_id))
      except Exception:
        continue

  # Fallback: invoices.ipm_case_id / ipm_case_ref Link Invoice 
  for inv_id in _fetch_invoice_ids_from_billing_db(matter_id=mid, our_ref=our_ref):
    _add(int(inv_id))

  return ids


def _invoice_open_url(invoice_id: int) -> Optional[str]:
  base = (current_app.config.get("INVOICE_MODULE_VIEW_BASE_URL") or "").strip()
  if not base:
    return None
  return f"{base.rstrip('/')}/{int(invoice_id)}"


def _invoice_pdf_url(invoice_id: int) -> Optional[str]:
  base = (current_app.config.get("INVOICE_MODULE_VIEW_BASE_URL") or "").strip()
  if not base:
    return None
  return f"{base.rstrip('/')}/{int(invoice_id)}.pdf"


def _normalize_billing_status(status: Any) -> str:
  normalized = str(status or "").strip().lower()
  if normalized in _BILLING_STATUS_META:
    return normalized
  return ""


def _normalize_payment_status(status: Any) -> str:
  normalized = str(status or "").strip().lower()
  if normalized == "overpaid":
    return "paid"
  if normalized in _PAYMENT_STATUS_META:
    return normalized
  return ""


def _derive_billing_payment_status(row: Dict[str, Any]) -> tuple[str, str]:
  billing = _normalize_billing_status(row.get("billing_status"))
  payment = _normalize_payment_status(row.get("payment_status"))
  legacy = str(row.get("status") or "").strip().lower()

  if not billing:
    if legacy in _BILLING_STATUS_META:
      billing = legacy
    elif legacy in ("payment_pending", "paid"):
      billing = "sent"
    else:
      billing = "draft"

  if not payment:
    if legacy == "paid":
      payment = "paid"
    elif legacy in ("payment_pending", "pre_overdue"):
      payment = "pending"
    elif legacy == "void":
      payment = "none"
    else:
      payment = "unpaid"

  try:
    payment_verified = int(row.get("payment_verified") or 0)
  except Exception:
    payment_verified = 0
  if payment_verified == 1 and billing != "void":
    payment = "paid"
  elif billing == "void":
    payment = "none"

  return billing, payment


def _is_void_invoice_row(row: Dict[str, Any]) -> bool:
  billing, _payment = _derive_billing_payment_status(row)
  return billing == "void"


def _status_meta(
  code: str, mapping: Dict[str, Dict[str, str]], *, fallback_pill: str = "none"
) -> Dict[str, str]:
  meta = mapping.get(code or "")
  if meta:
    return {"code": code, "label": meta["label"], "pill": meta["pill"]}
  raw = str(code or "").strip()
  return {
    "code": code,
    "label": raw or "-",
    "pill": fallback_pill,
  }


def _build_case_invoice_row(
  *,
  invoice_id: int,
  row: Dict[str, Any],
  total_minor: int,
  paid_total: int,
  today: date,
  line_items_summary: str = "",
) -> Dict[str, Any]:
  currency = (row.get("currency") or "USD").upper()
  billing_status, payment_status = _derive_billing_payment_status(row)

  if (
    payment_status == "paid"
    and int(total_minor or 0) > 0
    and paid_total < int(total_minor or 0)
  ):
    paid_total = int(total_minor or 0)

  outstanding = max(0, int(total_minor or 0) - int(paid_total or 0))
  due_at = _parse_date(row.get("due_date"))
  status = _resolve_status(row, outstanding, paid_total, due_at, today)
  title = _resolve_invoice_title(row, line_items_summary=line_items_summary)

  billing_meta = _status_meta(billing_status, _BILLING_STATUS_META)
  payment_meta = _status_meta(payment_status, _PAYMENT_STATUS_META)

  return {
    "invoice_id": invoice_id,
    "invoice_no": row.get("number") or f"#{invoice_id}",
    "title": title,
    "issued_at": row.get("issue_date"),
    "due_at": row.get("due_date"),
    "status": status,
    "billing_status": billing_meta["code"],
    "billing_status_label": billing_meta["label"],
    "billing_status_pill": billing_meta["pill"],
    "payment_status": payment_meta["code"],
    "payment_status_label": payment_meta["label"],
    "payment_status_pill": payment_meta["pill"],
    "is_overdue": status == "OVERDUE",
    "total": int(total_minor or 0),
    "paid": int(paid_total or 0),
    "outstanding": outstanding,
    "currency": currency,
    "line_items_summary": line_items_summary,
    "open_url": _invoice_open_url(invoice_id),
    "pdf_url": _invoice_pdf_url(invoice_id),
  }


def fetch_case_invoices(matter_id: str) -> Dict[str, Any]:
  invoice_ids = fetch_case_invoice_ids(matter_id)
  if not invoice_ids:
    return {"summary": _empty_summary(), "invoices": []}

  invoices: list[dict[str, Any]] = []
  currency = "USD"
  today = date.today()

  if _SERVICES_AVAILABLE:
    for inv_id in invoice_ids:
      row = InvoiceService.get_by_id(inv_id)
      if not row:
        continue
      if _is_void_invoice_row(row):
        continue
      currency = (row.get("currency") or "USD").upper()
      total_minor = row.get("total_minor")
      try:
        total_minor = int(total_minor) if total_minor is not None else None
      except Exception:
        total_minor = None
      if total_minor is None:
        try:
          total_minor = int(to_minor(Decimal(str(row.get("total") or 0)), currency))
        except Exception:
          total_minor = 0
      paid_total = 0
      try:
        paid_total = int(PaymentService.get_total_paid(inv_id))
      except Exception:
        paid_total = 0
      # Legacy payment_meta fallback (and USD deposits)
      try:
        meta_paid = _parse_payment_meta_minor(row.get("payment_meta"), currency)
      except Exception:
        meta_paid = 0
      if meta_paid and meta_paid > paid_total:
        paid_total = meta_paid
      line_items_summary = ""
      try:
        line_items_summary = _summarize_line_items(InvoiceService.get_line_items(inv_id))
      except Exception:
        line_items_summary = ""
      invoices.append(
        _build_case_invoice_row(
          invoice_id=inv_id,
          row=row,
          total_minor=int(total_minor or 0),
          paid_total=int(paid_total or 0),
          today=today,
          line_items_summary=line_items_summary,
        )
      )
  else:
    rows = fetch_invoices_by_ids(invoice_ids)
    for inv_id in invoice_ids:
      row = rows.get(inv_id)
      if not row:
        continue
      if _is_void_invoice_row(row):
        continue
      currency = (row.get("currency") or "USD").upper()
      total = 0
      try:
        total = int(to_minor(Decimal(str(row.get("total") or 0)), currency))
      except Exception:
        total = 0
      invoices.append(
        _build_case_invoice_row(
          invoice_id=inv_id,
          row=row,
          total_minor=total,
          paid_total=0,
          today=today,
          line_items_summary="",
        )
      )

  summary = _summarize_invoices(invoices, currency)
  return {"summary": summary, "invoices": invoices}


def _resolve_status(
  row: dict[str, Any],
  outstanding: int,
  paid_total: int,
  due_at: Optional[date],
  today: date,
) -> str:
  billing, payment_status = _derive_billing_payment_status(row)
  if billing == "draft":
    return "DRAFT"
  if payment_status == "paid":
    return "PAID"
  if outstanding <= 0 and (paid_total > 0 or billing == "paid"):
    return "PAID"
  if paid_total > 0 and outstanding > 0:
    return "PARTIAL"
  if due_at and due_at < today and outstanding > 0:
    return "OVERDUE"
  return "SENT"


def _summarize_invoices(invoices: List[Dict[str, Any]], currency: str) -> Dict[str, Any]:
  total_billed = sum(int(inv.get("total") or 0) for inv in invoices)
  total_paid = sum(int(inv.get("paid") or 0) for inv in invoices)
  outstanding = sum(int(inv.get("outstanding") or 0) for inv in invoices)
  overdue_count = sum(1 for inv in invoices if (inv.get("status") == "OVERDUE"))
  return {
    "total_billed": total_billed,
    "total_paid": total_paid,
    "outstanding": outstanding,
    "overdue_count": overdue_count,
    "currency": currency or "USD",
  }


def _empty_summary() -> Dict[str, Any]:
  return {
    "total_billed": 0,
    "total_paid": 0,
    "outstanding": 0,
    "overdue_count": 0,
    "currency": "USD",
  }
