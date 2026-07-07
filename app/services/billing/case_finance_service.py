from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, or_

from app.extensions import db
from app.models.legacy_finance import CaseExpenseInvoiceMap, LegacyExpense, LegacyExpensePayment
from app.services.billing.case_invoice_service import fetch_case_invoices


class CaseFinanceService:
  """Aggregate case finance data from canonical invoice + LegacyExpense sources."""

  @staticmethod
  def get_summary(
    matter_id: str,
    *,
    filters: Optional[dict] = None,
    include_ledger: bool = True,
  ) -> Dict[str, Any]:
    mid = (matter_id or "").strip()
    if not mid:
      return {
        "summary": {
          "ar": _empty_ar_summary(),
          "ap": _empty_ap_summary(),
          "links": {"unbilled_expense_count": 0},
        },
        "invoices": [],
        "payables": [],
        "ledger": [],
      }

    invoice_payload = fetch_case_invoices(mid)
    invoices = _normalize_invoices(invoice_payload.get("invoices") or [])
    ar_summary = _normalize_ar_summary(invoice_payload.get("summary") or {})

    payables, ap_summary, unbilled_count = _fetch_payables(mid)

    ledger: list[dict[str, Any]] = []
    if include_ledger:
      ledger = CaseFinanceService.list_ledger(
        mid,
        filters=filters or {},
        invoices=invoices,
        payables=payables,
      )

    return {
      "summary": {
        "ar": ar_summary,
        "ap": ap_summary,
        "links": {"unbilled_expense_count": unbilled_count},
      },
      "invoices": invoices,
      "payables": payables,
      "ledger": ledger,
    }

  @staticmethod
  def list_ledger(
    matter_id: str,
    *,
    filters: Optional[dict] = None,
    invoices: Optional[List[Dict[str, Any]]] = None,
    payables: Optional[List[Dict[str, Any]]] = None,
  ) -> List[Dict[str, Any]]:
    mid = (matter_id or "").strip()
    if not mid:
      return []

    filters = filters or {}
    type_filter = (filters.get("type") or "ALL").strip().upper()
    query = (filters.get("q") or "").strip().lower()
    from_date = _parse_date_only(filters.get("from"))
    to_date = _parse_date_only(filters.get("to"))

    if invoices is None:
      invoice_payload = fetch_case_invoices(mid)
      invoices = _normalize_invoices(invoice_payload.get("invoices") or [])
    if payables is None:
      payables, _ap_summary, _unbilled = _fetch_payables(mid)

    items: list[dict[str, Any]] = []

    if type_filter in ("ALL", "INVOICE"):
      for inv in invoices:
        invoice_no = (inv.get("invoice_no") or "").strip()
        title = _invoice_ledger_title(inv, invoice_no=invoice_no)
        items.append(
          {
            "type": "INVOICE",
            "date": inv.get("issue_date") or inv.get("due_date") or "",
            "title": title,
            "amount_minor": int(inv.get("total_minor") or 0),
            "paid_minor": int(inv.get("paid_minor") or 0),
            "outstanding_minor": int(inv.get("outstanding_minor") or 0),
            "currency": (inv.get("currency") or "USD").upper(),
            "status": inv.get("status") or "",
            "billing_status": inv.get("billing_status"),
            "billing_status_label": inv.get("billing_status_label"),
            "billing_status_pill": inv.get("billing_status_pill"),
            "payment_status": inv.get("payment_status"),
            "payment_status_label": inv.get("payment_status_label"),
            "payment_status_pill": inv.get("payment_status_pill"),
            "is_overdue": bool(inv.get("is_overdue")),
            "invoice_id": inv.get("invoice_id"),
            "invoice_no": invoice_no,
          }
        )

    if type_filter in ("ALL", "PAYABLE"):
      for exp in payables or []:
        title = (exp.get("description") or "").strip() or (
          exp.get("expense_ref") or ""
        ).strip()
        if not title:
          title = f"Payable {exp.get('expense_id') or ''}".strip()
        items.append(
          {
            "type": "PAYABLE",
            "date": exp.get("expense_date") or exp.get("dn_date") or "",
            "title": title,
            "amount": float(exp.get("requested_total") or 0),
            "paid": float(exp.get("paid_total") or 0),
            "outstanding": float(exp.get("outstanding") or 0),
            "status": exp.get("status") or "",
            "expense_id": exp.get("expense_id"),
          }
        )

    items = _filter_ledger_items(items, query=query, from_date=from_date, to_date=to_date)
    items.sort(key=_ledger_sort_key, reverse=True)
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date_only(value: Any) -> Optional[date]:
  if not value:
    return None
  try:
    return date.fromisoformat(str(value)[:10])
  except Exception:
    return None


def _safe_float(value: Any) -> float:
  try:
    return float(value or 0)
  except Exception:
    return 0.0


def _safe_int(value: Any) -> int:
  try:
    return int(value or 0)
  except Exception:
    return 0


def _invoice_ledger_title(inv: Dict[str, Any], *, invoice_no: str) -> str:
  # Case ledger "Item" should prioritize line-item descriptions over invoice memo/title.
  line_items_summary = str(inv.get("line_items_summary") or "").strip()
  if line_items_summary:
    return line_items_summary
  base_title = str(inv.get("title") or "").strip()
  if base_title:
    return base_title
  return f"Invoice {invoice_no or '#' + str(inv.get('invoice_id') or '')}".strip()


def _active_filter(model):
  # Deprecated for mixed usage. Use _active_filter_bool or _active_filter_int directly.
  return or_(model.is_deleted == False, model.is_deleted.is_(None)) # noqa: E712


def _active_filter_bool(model):
  return or_(model.is_deleted == False, model.is_deleted.is_(None)) # noqa: E712


def _active_filter_int(model):
  # Compare against False instead of 0 to stay compatible with Boolean columns on Postgres.
  return or_(model.is_deleted == False, model.is_deleted.is_(None)) # noqa: E712


def _normalize_invoices(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
  invoices = []
  for row in rows:
    invoices.append(
      {
        "invoice_id": row.get("invoice_id"),
        "invoice_no": row.get("invoice_no"),
        "title": row.get("title"),
        "line_items_summary": row.get("line_items_summary"),
        "issue_date": row.get("issued_at") or row.get("issue_date"),
        "due_date": row.get("due_at") or row.get("due_date"),
        "status": row.get("status"),
        "billing_status": row.get("billing_status"),
        "billing_status_label": row.get("billing_status_label"),
        "billing_status_pill": row.get("billing_status_pill"),
        "payment_status": row.get("payment_status"),
        "payment_status_label": row.get("payment_status_label"),
        "payment_status_pill": row.get("payment_status_pill"),
        "is_overdue": bool(row.get("is_overdue")),
        "total_minor": _safe_int(row.get("total")),
        "paid_minor": _safe_int(row.get("paid")),
        "outstanding_minor": _safe_int(row.get("outstanding")),
        "currency": (row.get("currency") or "USD").upper(),
        "open_url": row.get("open_url"),
        "pdf_url": row.get("pdf_url"),
      }
    )
  return invoices


def _normalize_ar_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
  return {
    "billed_minor": _safe_int(summary.get("total_billed")),
    "paid_minor": _safe_int(summary.get("total_paid")),
    "outstanding_minor": _safe_int(summary.get("outstanding")),
    "overdue_count": _safe_int(summary.get("overdue_count")),
    "currency": (summary.get("currency") or "USD").upper(),
  }


def _empty_ar_summary() -> Dict[str, Any]:
  return {
    "billed_minor": 0,
    "paid_minor": 0,
    "outstanding_minor": 0,
    "overdue_count": 0,
    "currency": "USD",
  }


def _empty_ap_summary() -> Dict[str, Any]:
  return {
    "requested": 0.0,
    "paid": 0.0,
    "outstanding": 0.0,
    "unpaid_count": 0,
    "currency": "USD",
  }


def _fetch_payables(matter_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], int]:
  expenses = (
    LegacyExpense.query.filter(LegacyExpense.matter_id == matter_id)
    .filter(_active_filter_bool(LegacyExpense))
    .order_by(LegacyExpense.dn_date.desc(), LegacyExpense.expense_id.desc())
    .all()
  )
  if not expenses:
    return [], _empty_ap_summary(), 0

  expense_ids = [e.expense_id for e in expenses if e.expense_id]
  payment_meta: Dict[str, Dict[str, Any]] = {}
  if expense_ids:
    rows = (
      db.session.query(
        LegacyExpensePayment.expense_id.label("expense_id"),
        func.sum(LegacyExpensePayment.sent_amount).label("paid_total"),
        func.max(LegacyExpensePayment.sent_date).label("last_sent_date"),
      )
      .filter(LegacyExpensePayment.expense_id.in_(expense_ids))
      .filter(_active_filter_bool(LegacyExpensePayment))
      .group_by(LegacyExpensePayment.expense_id)
      .all()
    )
    payment_meta = {
      r.expense_id: {
        "paid_total": _safe_float(r.paid_total),
        "last_sent_date": r.last_sent_date,
      }
      for r in rows
    }

  link_map: Dict[str, List[Dict[str, Any]]] = {}
  if expense_ids:
    links = (
      CaseExpenseInvoiceMap.query.filter(CaseExpenseInvoiceMap.expense_id.in_(expense_ids))
      .filter(_active_filter_int(CaseExpenseInvoiceMap))
      .order_by(CaseExpenseInvoiceMap.id.asc())
      .all()
    )
    for link in links:
      link_map.setdefault(link.expense_id, []).append(
        {
          "billing_invoice_id": link.billing_invoice_id,
          "billing_line_item_id": link.billing_line_item_id,
          "amount_minor": link.amount_minor,
          "currency": link.currency,
          "linked_id": link.id,
        }
      )

  total_requested = 0.0
  total_paid = 0.0
  total_outstanding = 0.0
  unpaid_count = 0
  currency = "USD"

  payables: List[Dict[str, Any]] = []
  for exp in expenses:
    requested = _safe_float(exp.requested_total)
    paid = _safe_float(payment_meta.get(exp.expense_id, {}).get("paid_total"))
    outstanding = max(0.0, requested - paid)
    status = "PAID" if outstanding <= 0 else "UNPAID"
    if outstanding > 0:
      unpaid_count += 1

    exp_currency = (exp.currency or "").strip()
    if exp_currency and currency == "USD":
      currency = exp_currency

    total_requested += requested
    total_paid += paid
    total_outstanding += outstanding

    payables.append(
      {
        "expense_id": exp.expense_id,
        "dn_date": exp.dn_date,
        "expense_date": exp.expense_date,
        "due_date": exp.due_date,
        "vendor_name": exp.vendor_name,
        "category_code": exp.category_code,
        "expense_ref": exp.expense_ref,
        "requested_total": requested,
        "paid_total": paid,
        "outstanding": outstanding,
        "status": status,
        "description": exp.description,
        "currency": (exp.currency or "USD").upper(),
        "last_paid_date": payment_meta.get(exp.expense_id, {}).get("last_sent_date"),
        "linked_invoices": link_map.get(exp.expense_id, []),
      }
    )

  unbilled_count = sum(1 for exp in expenses if not link_map.get(exp.expense_id))

  return (
    payables,
    {
      "requested": total_requested,
      "paid": total_paid,
      "outstanding": total_outstanding,
      "unpaid_count": unpaid_count,
      "currency": (currency or "USD").upper(),
    },
    unbilled_count,
  )


def _filter_ledger_items(
  items: List[Dict[str, Any]],
  *,
  query: str,
  from_date: Optional[date],
  to_date: Optional[date],
) -> List[Dict[str, Any]]:
  if not items:
    return items

  filtered = []
  for item in items:
    item_date = _parse_date_only(item.get("date"))
    if from_date and (not item_date or item_date < from_date):
      continue
    if to_date and (not item_date or item_date > to_date):
      continue
    if query:
      haystack = " ".join(
        [
          str(item.get("title") or ""),
          str(item.get("invoice_no") or ""),
          str(item.get("invoice_id") or ""),
          str(item.get("expense_id") or ""),
        ]
      ).lower()
      if query not in haystack:
        continue
    filtered.append(item)
  return filtered


def _ledger_sort_key(item: Dict[str, Any]) -> Tuple[str, str]:
  return (str(item.get("date") or ""), str(item.get("type") or ""))
