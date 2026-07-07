from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from flask import current_app
from sqlalchemy import func, or_

from app.extensions import db
from app.models.billing_guardrail import BillingGuardrailFinding
from app.models.legacy_finance import CaseExpenseInvoiceMap, LegacyExpense
from app.models.ip_records import Matter, MatterEvent
from app.models.workflow import Workflow
from app.services.billing.db_core import _actual_table_name, get_db, row_get, row_to_dict
from app.services.billing.invoice_services import PaymentService
from app.services.billing.utils import to_minor

OPEN_STATUSES = {
  BillingGuardrailFinding.STATUS_OPEN,
  BillingGuardrailFinding.STATUS_REVIEWING,
}

DEFAULT_EVENT_KEYWORDS = (
  "filing",
  "application",
  "office_action",
  "oa",
  "registration",
  "allowance",
  "appeal",
  "Filing",
  "",
  "Examination",
  "Registration",
  "",
  "",
)


@dataclass(frozen=True)
class GuardrailSyncResult:
  scanned: int
  created: int
  updated: int


def _table_name(base: str) -> str:
  return _actual_table_name(base)


def _not_deleted_sql(alias: str) -> str:
  return (
    f"COALESCE(LOWER(CAST({alias}.is_deleted AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def _safe_int(value: Any, default: int = 0) -> int:
  try:
    return int(value or 0)
  except Exception:
    try:
      return int(Decimal(str(value or "0")))
    except Exception:
      return default


def _amount_to_minor(value: Any, currency: str | None) -> int:
  cur = (currency or "USD").strip().upper() or "USD"
  raw = str(value or "").replace(",", "").strip()
  if not raw:
    return 0
  if cur in {"USD", "JPY"}:
    try:
      return int(Decimal(raw).quantize(Decimal("1")))
    except Exception:
      return 0
  try:
    return int(to_minor(Decimal(raw), cur))
  except Exception:
    return 0


def _invoice_total_minor(row: dict[str, Any]) -> int:
  currency = (row.get("currency") or "USD").upper()
  total_minor = _safe_int(row.get("total_minor"))
  if total_minor <= 0:
    total_minor = _amount_to_minor(row.get("total"), currency)
  return total_minor


def _parse_ymd(value: Any) -> date | None:
  if not value:
    return None
  if isinstance(value, date):
    return value
  try:
    return date.fromisoformat(str(value)[:10])
  except Exception:
    return None


def _matter_map(matter_ids: Iterable[str]) -> dict[str, Matter]:
  ids = sorted({str(mid).strip() for mid in matter_ids if str(mid or "").strip()})
  if not ids:
    return {}
  rows = Matter.query.filter(Matter.matter_id.in_(ids)).all()
  return {str(row.matter_id): row for row in rows}


def _findings_base() -> list[dict[str, Any]]:
  return []


def _finding(
  *,
  finding_key: str,
  finding_type: str,
  severity: str,
  matter_id: str | None,
  our_ref: str | None,
  source_type: str,
  source_id: str,
  title: str,
  detail: str,
  expected_amount_minor: int | None = None,
  actual_amount_minor: int | None = None,
  gap_amount_minor: int | None = None,
  currency: str = "USD",
  confidence: int = 70,
  evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
  return {
    "finding_key": finding_key,
    "finding_type": finding_type,
    "severity": severity,
    "matter_id": matter_id,
    "our_ref": our_ref,
    "source_type": source_type,
    "source_id": source_id,
    "currency": (currency or "USD").upper(),
    "expected_amount_minor": expected_amount_minor,
    "actual_amount_minor": actual_amount_minor,
    "gap_amount_minor": gap_amount_minor,
    "confidence": max(0, min(int(confidence or 0), 100)),
    "title": title,
    "detail": detail,
    "evidence_json": evidence or {},
  }


def _billable_event_keywords() -> tuple[str, ...]:
  configured = current_app.config.get("BILLING_GUARDRAIL_EVENT_KEYWORDS")
  if isinstance(configured, str):
    values = [x.strip() for x in configured.split(",")]
  elif isinstance(configured, (list, tuple, set)):
    values = [str(x).strip() for x in configured]
  else:
    values = list(DEFAULT_EVENT_KEYWORDS)
  return tuple(x.lower() for x in values if x)


def _event_is_billable(event_key: Any, raw_text: Any = "") -> bool:
  text = f"{event_key or ''} {raw_text or ''}".strip().lower()
  if not text:
    return False
  return any(keyword in text for keyword in _billable_event_keywords())


def _workflow_is_billable(wf: Workflow) -> bool:
  code = str(getattr(wf, "business_code", "") or "").strip().upper()
  if code.startswith("ANNUITY:") or code.startswith("MGMT:"):
    return False
  category = str(getattr(wf, "category", "") or "").strip().upper()
  if category == "MGMT":
    return False
  if getattr(wf, "work_hours", None):
    return True
  name = str(getattr(wf, "name", "") or "")
  return _event_is_billable(code, name)


def _invoice_rows_for_matters(matter_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
  ids = sorted({str(mid).strip() for mid in matter_ids if str(mid or "").strip()})
  if not ids:
    return {}
  placeholders = ",".join("?" for _ in ids)
  conn = get_db()
  try:
    invoices = _table_name("invoices")
    eicm = _table_name("external_invoice_case_map")
    icm = _table_name("invoice_case_map")
    sql_parts = [
      f"""
      SELECT i.*, e.matter_id AS link_matter_id, e.our_ref AS link_our_ref
      FROM {invoices} i
      JOIN {eicm} e ON e.external_invoice_id = i.id
      WHERE e.matter_id IN ({placeholders})
       AND {_not_deleted_sql("i")}
       AND {_not_deleted_sql("e")}
      """,
      f"""
      SELECT i.*, m.matter_id AS link_matter_id, m.our_ref AS link_our_ref
      FROM {invoices} i
      JOIN {icm} m ON m.invoice_id = i.id
      WHERE m.matter_id IN ({placeholders})
       AND {_not_deleted_sql("i")}
       AND {_not_deleted_sql("m")}
      """,
      f"""
      SELECT i.*, i.ipm_case_id AS link_matter_id, i.ipm_case_ref AS link_our_ref
      FROM {invoices} i
      WHERE i.ipm_case_id IN ({placeholders})
       AND {_not_deleted_sql("i")}
      """,
    ]
    rows: list[dict[str, Any]] = []
    for sql in sql_parts:
      rows.extend(row_to_dict(r) for r in conn.execute(sql, ids).fetchall())
  finally:
    conn.close()

  by_matter: dict[str, list[dict[str, Any]]] = {}
  seen: set[tuple[str, int]] = set()
  for row in rows:
    mid = str(row.get("link_matter_id") or "").strip()
    inv_id = _safe_int(row.get("id"))
    if not mid or inv_id <= 0:
      continue
    key = (mid, inv_id)
    if key in seen:
      continue
    seen.add(key)
    by_matter.setdefault(mid, []).append(row)
  return by_matter


def _invoice_dates_by_matter(matter_ids: Iterable[str]) -> dict[str, list[date]]:
  by_matter = _invoice_rows_for_matters(matter_ids)
  out: dict[str, list[date]] = {}
  for mid, rows in by_matter.items():
    dates = []
    for row in rows:
      issued = _parse_ymd(row.get("issue_date"))
      if issued:
        dates.append(issued)
    out[mid] = sorted(dates)
  return out


def _has_invoice_after(
  invoice_dates_by_matter: dict[str, list[date]],
  *,
  matter_id: str,
  event_date: date | None,
  grace_days: int = 7,
) -> bool:
  dates = invoice_dates_by_matter.get(str(matter_id), [])
  if not dates:
    return False
  if not event_date:
    return True
  cutoff = event_date - timedelta(days=max(0, grace_days))
  return any(issued >= cutoff for issued in dates)


def _collect_expense_findings(limit: int) -> list[dict[str, Any]]:
  active = or_(LegacyExpense.is_deleted == False, LegacyExpense.is_deleted.is_(None)) # noqa: E712
  rows = (
    LegacyExpense.query.filter(active)
    .filter(func.coalesce(LegacyExpense.requested_total, 0) > 0)
    .order_by(
      LegacyExpense.dn_date.desc(),
      LegacyExpense.expense_date.desc(),
      LegacyExpense.expense_id.desc(),
    )
    .limit(max(1, int(limit)))
    .all()
  )
  matter_ids = [str(row.matter_id) for row in rows if row.matter_id]
  matters = _matter_map(matter_ids)

  mapped_rows = (
    db.session.query(
      CaseExpenseInvoiceMap.expense_id,
      func.coalesce(func.sum(CaseExpenseInvoiceMap.amount_minor), 0),
    )
    .filter(CaseExpenseInvoiceMap.expense_id.in_([row.expense_id for row in rows]))
    .filter(
      or_(
        CaseExpenseInvoiceMap.is_deleted == 0,
        CaseExpenseInvoiceMap.is_deleted.is_(None),
      )
    )
    .group_by(CaseExpenseInvoiceMap.expense_id)
    .all()
    if rows
    else []
  )
  mapped_minor = {str(expense_id): _safe_int(amount) for expense_id, amount in mapped_rows}

  findings = _findings_base()
  for exp in rows:
    currency = (exp.currency or "USD").upper()
    expected = _amount_to_minor(exp.requested_total, currency)
    actual = mapped_minor.get(str(exp.expense_id), 0)
    gap = max(0, expected - actual)
    if expected <= 0 or gap <= 0:
      continue

    matter = matters.get(str(exp.matter_id))
    our_ref = getattr(matter, "our_ref", None) or None
    type_code = "unbilled_expense" if actual <= 0 else "underbilled_expense"
    severity = "high" if gap >= max(100000, expected * 0.5) else "medium"
    label = "Billing Advanced cost/" if actual <= 0 else "Billing Advanced cost/"
    title = f"{label}: {exp.expense_ref or exp.dn_no or exp.expense_id}"
    detail = (exp.description or exp.vendor_name or "").strip()
    findings.append(
      _finding(
        finding_key=f"{type_code}:{exp.expense_id}",
        finding_type=type_code,
        severity=severity,
        matter_id=str(exp.matter_id) if exp.matter_id else None,
        our_ref=our_ref,
        source_type="expense",
        source_id=str(exp.expense_id),
        title=title,
        detail=detail or "Matter Amount Invoice  .",
        expected_amount_minor=expected,
        actual_amount_minor=actual,
        gap_amount_minor=gap,
        currency=currency,
        confidence=92 if actual <= 0 else 86,
        evidence={
          "expense_ref": exp.expense_ref,
          "dn_no": exp.dn_no,
          "dn_date": exp.dn_date,
          "requested_total": exp.requested_total,
          "mapped_amount_minor": actual,
        },
      )
    )
  return findings


def _collect_workflow_findings(limit: int) -> list[dict[str, Any]]:
  since = date.today() - timedelta(
    days=int(current_app.config.get("BILLING_GUARDRAIL_EVENT_LOOKBACK_DAYS", 365))
  )
  rows = (
    Workflow.query.filter(Workflow.status == Workflow.STATUS_COMPLETED)
    .filter(Workflow.completed_date.isnot(None), Workflow.completed_date >= since)
    .order_by(Workflow.completed_date.desc(), Workflow.id.desc())
    .limit(max(1, int(limit)))
    .all()
  )
  rows = [wf for wf in rows if wf.case_id and _workflow_is_billable(wf)]
  invoice_dates = _invoice_dates_by_matter([str(wf.case_id) for wf in rows])
  matters = _matter_map([str(wf.case_id) for wf in rows])

  findings = _findings_base()
  for wf in rows:
    event_date = getattr(wf, "completed_date", None)
    mid = str(wf.case_id)
    if _has_invoice_after(invoice_dates, matter_id=mid, event_date=event_date):
      continue
    matter = matters.get(mid)
    findings.append(
      _finding(
        finding_key=f"billable-workflow:{wf.id}",
        finding_type="billable_workflow_without_invoice",
        severity="medium",
        matter_id=mid,
        our_ref=getattr(matter, "our_ref", None) or None,
        source_type="workflow",
        source_id=str(wf.id),
        title=f"Done Task Billing Confirm required: {wf.name}",
        detail="Done  Task Link Invoice Confirm .",
        confidence=65,
        evidence={
          "workflow_id": wf.id,
          "completed_date": event_date.isoformat() if event_date else None,
          "work_hours": wf.work_hours,
          "business_code": wf.business_code,
          "category": wf.category,
        },
      )
    )
  return findings


def _collect_event_findings(limit: int) -> list[dict[str, Any]]:
  since = date.today() - timedelta(
    days=int(current_app.config.get("BILLING_GUARDRAIL_EVENT_LOOKBACK_DAYS", 365))
  )
  rows = (
    MatterEvent.query.filter(
      MatterEvent.event_date.isnot(None), MatterEvent.event_date >= since
    )
    .order_by(MatterEvent.event_date.desc(), MatterEvent.mevent_id.desc())
    .limit(max(1, int(limit)))
    .all()
  )
  rows = [event for event in rows if _event_is_billable(event.event_key, event.raw_text)]
  invoice_dates = _invoice_dates_by_matter([str(event.matter_id) for event in rows])
  matters = _matter_map([str(event.matter_id) for event in rows])

  findings = _findings_base()
  for event in rows:
    mid = str(event.matter_id)
    if _has_invoice_after(invoice_dates, matter_id=mid, event_date=event.event_date):
      continue
    matter = matters.get(mid)
    findings.append(
      _finding(
        finding_key=f"billable-event:{event.mevent_id}",
        finding_type="billable_event_without_invoice",
        severity="low",
        matter_id=mid,
        our_ref=getattr(matter, "our_ref", None) or None,
        source_type="matter_event",
        source_id=str(event.mevent_id),
        title=f"Matter Billing Confirm required: {event.event_key}",
        detail=event.raw_text
        or "Billing  Matter  Link Invoice Confirm .",
        confidence=50,
        evidence={
          "event_key": event.event_key,
          "event_date": event.event_date.isoformat() if event.event_date else None,
          "source_column": event.source_column,
        },
      )
    )
  return findings


def _fetch_due_invoice_link_rows(limit: int) -> list[dict[str, Any]]:
  today = date.today().isoformat()
  conn = get_db()
  try:
    invoices = _table_name("invoices")
    eicm = _table_name("external_invoice_case_map")
    icm = _table_name("invoice_case_map")
    base_where = (
      "i.due_date IS NOT NULL AND i.due_date < ? "
      "AND COALESCE(LOWER(CAST(i.billing_status AS TEXT)), '') NOT IN ('draft','void') "
      "AND COALESCE(LOWER(CAST(i.payment_status AS TEXT)), '') NOT IN ('paid','overpaid','none') "
      f"AND {_not_deleted_sql('i')}"
    )
    sql_parts = [
      f"""
      SELECT i.*, e.matter_id AS link_matter_id, e.our_ref AS link_our_ref
      FROM {invoices} i
      JOIN {eicm} e ON e.external_invoice_id = i.id
      WHERE {base_where} AND {_not_deleted_sql("e")}
      ORDER BY i.due_date ASC, i.id DESC
      LIMIT ?
      """,
      f"""
      SELECT i.*, m.matter_id AS link_matter_id, m.our_ref AS link_our_ref
      FROM {invoices} i
      JOIN {icm} m ON m.invoice_id = i.id
      WHERE {base_where} AND {_not_deleted_sql("m")}
      ORDER BY i.due_date ASC, i.id DESC
      LIMIT ?
      """,
      f"""
      SELECT i.*, i.ipm_case_id AS link_matter_id, i.ipm_case_ref AS link_our_ref
      FROM {invoices} i
      WHERE {base_where}
       AND COALESCE(i.ipm_case_id, '') <> ''
      ORDER BY i.due_date ASC, i.id DESC
      LIMIT ?
      """,
    ]
    rows: list[dict[str, Any]] = []
    for sql in sql_parts:
      rows.extend(row_to_dict(r) for r in conn.execute(sql, [today, int(limit)]).fetchall())
  finally:
    conn.close()
  seen: set[tuple[str, int]] = set()
  deduped = []
  for row in rows:
    mid = str(row.get("link_matter_id") or "").strip()
    inv_id = _safe_int(row.get("id"))
    if not mid or inv_id <= 0:
      continue
    key = (mid, inv_id)
    if key in seen:
      continue
    seen.add(key)
    deduped.append(row)
  return deduped[: max(1, int(limit))]


def _collect_uncollected_findings(limit: int) -> list[dict[str, Any]]:
  rows = _fetch_due_invoice_link_rows(limit)
  matter_ids = [str(row.get("link_matter_id")) for row in rows if row.get("link_matter_id")]
  matters = _matter_map(matter_ids)
  findings = _findings_base()
  today = date.today()
  for row in rows:
    invoice_id = _safe_int(row.get("id"))
    if invoice_id <= 0:
      continue
    currency = (row.get("currency") or "USD").upper()
    total = _invoice_total_minor(row)
    paid = _safe_int(PaymentService.get_total_paid(invoice_id))
    if str(row.get("payment_status") or "").strip().lower() in {"paid", "overpaid"}:
      paid = max(paid, total)
    outstanding = max(0, total - paid)
    if outstanding <= 0:
      continue
    due = _parse_ymd(row.get("due_date"))
    overdue_days = (today - due).days if due else 0
    severity = "high" if overdue_days >= 30 or outstanding >= 1000000 else "medium"
    mid = str(row.get("link_matter_id") or "").strip()
    matter = matters.get(mid)
    our_ref = (
      row.get("link_our_ref") or getattr(matter, "our_ref", None) or row.get("ipm_case_ref")
    )
    findings.append(
      _finding(
        finding_key=f"uncollected-invoice:{invoice_id}:{mid}",
        finding_type="uncollected_invoice",
        severity=severity,
        matter_id=mid,
        our_ref=our_ref,
        source_type="invoice",
        source_id=str(invoice_id),
        title=f"Collection Invoice: {row.get('number') or '#' + str(invoice_id)}",
        detail=f"Due date {max(0, overdue_days)}days , Collection  exists.",
        expected_amount_minor=total,
        actual_amount_minor=paid,
        gap_amount_minor=outstanding,
        currency=currency,
        confidence=95,
        evidence={
          "invoice_id": invoice_id,
          "invoice_number": row.get("number"),
          "due_date": row.get("due_date"),
          "client_name": row.get("client_name"),
          "overdue_days": overdue_days,
        },
      )
    )
  return findings


def build_current_findings(*, limit_per_source: int = 300) -> list[dict[str, Any]]:
  limit = max(1, min(int(limit_per_source or 300), 1000))
  findings: list[dict[str, Any]] = []
  findings.extend(_collect_expense_findings(limit))
  findings.extend(_collect_workflow_findings(limit))
  findings.extend(_collect_event_findings(limit))
  findings.extend(_collect_uncollected_findings(limit))
  return findings


def sync_guardrail_findings(*, limit_per_source: int = 300) -> GuardrailSyncResult:
  now = datetime.utcnow()
  current = build_current_findings(limit_per_source=limit_per_source)
  created = 0
  updated = 0
  for payload in current:
    row = BillingGuardrailFinding.query.filter_by(
      finding_key=payload["finding_key"]
    ).one_or_none()
    if row is None:
      row = BillingGuardrailFinding(
        **payload,
        first_detected_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
      )
      db.session.add(row)
      created += 1
      continue

    row.last_seen_at = now
    row.updated_at = now
    if row.status in OPEN_STATUSES:
      for key, value in payload.items():
        setattr(row, key, value)
      updated += 1
  db.session.commit()
  return GuardrailSyncResult(scanned=len(current), created=created, updated=updated)


def list_guardrail_findings(
  *,
  status: str = "open",
  finding_type: str = "",
  severity: str = "",
  q: str = "",
  limit: int = 200,
) -> list[BillingGuardrailFinding]:
  query = BillingGuardrailFinding.query
  status = (status or "").strip().lower()
  if status and status != "all":
    query = query.filter(BillingGuardrailFinding.status == status)
  finding_type = (finding_type or "").strip()
  if finding_type:
    query = query.filter(BillingGuardrailFinding.finding_type == finding_type)
  severity = (severity or "").strip().lower()
  if severity:
    query = query.filter(BillingGuardrailFinding.severity == severity)
  q = (q or "").strip()
  if q:
    like = f"%{q}%"
    query = query.filter(
      or_(
        BillingGuardrailFinding.our_ref.ilike(like),
        BillingGuardrailFinding.title.ilike(like),
        BillingGuardrailFinding.detail.ilike(like),
        BillingGuardrailFinding.source_id.ilike(like),
      )
    )
  return (
    query.order_by(
      BillingGuardrailFinding.severity.desc(),
      BillingGuardrailFinding.last_seen_at.desc(),
      BillingGuardrailFinding.id.desc(),
    )
    .limit(max(1, min(int(limit or 200), 1000)))
    .all()
  )


def summarize_guardrail_findings() -> dict[str, Any]:
  rows = (
    db.session.query(
      BillingGuardrailFinding.status,
      BillingGuardrailFinding.finding_type,
      BillingGuardrailFinding.severity,
      func.count(BillingGuardrailFinding.id),
      func.coalesce(func.sum(BillingGuardrailFinding.gap_amount_minor), 0),
    )
    .group_by(
      BillingGuardrailFinding.status,
      BillingGuardrailFinding.finding_type,
      BillingGuardrailFinding.severity,
    )
    .all()
  )
  out: dict[str, Any] = {
    "open_count": 0,
    "reviewing_count": 0,
    "resolved_count": 0,
    "dismissed_count": 0,
    "open_gap_minor": 0,
    "by_type": {},
    "by_severity": {},
  }
  for status, finding_type, severity, count, gap in rows:
    count_i = _safe_int(count)
    gap_i = _safe_int(gap)
    if status == BillingGuardrailFinding.STATUS_OPEN:
      out["open_count"] += count_i
      out["open_gap_minor"] += gap_i
    elif status == BillingGuardrailFinding.STATUS_REVIEWING:
      out["reviewing_count"] += count_i
      out["open_gap_minor"] += gap_i
    elif status == BillingGuardrailFinding.STATUS_RESOLVED:
      out["resolved_count"] += count_i
    elif status == BillingGuardrailFinding.STATUS_DISMISSED:
      out["dismissed_count"] += count_i
    if status in OPEN_STATUSES:
      out["by_type"][finding_type] = out["by_type"].get(finding_type, 0) + count_i
      out["by_severity"][severity] = out["by_severity"].get(severity, 0) + count_i
  return out
