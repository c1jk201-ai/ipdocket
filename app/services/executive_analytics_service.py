from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_

from app.extensions import db
from app.models.billing_guardrail import BillingGuardrailFinding
from app.models.crm import CRMLead, CRMOpportunity
from app.models.workflow import Workflow
from app.services.billing.db_core import _actual_table_name, get_db, row_to_dict
from app.services.billing.guardrail_service import OPEN_STATUSES, summarize_guardrail_findings
from app.services.billing.invoice_services import PaymentService
from app.services.billing.utils import to_minor


def _table_name(base: str) -> str:
    return _actual_table_name(base)


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


def _add_amount(target: dict[str, int], currency: str | None, amount_minor: int) -> None:
    cur = (currency or "USD").strip().upper() or "USD"
    target[cur] = int(target.get(cur, 0)) + int(amount_minor or 0)


def _invoice_total_minor(row: dict[str, Any]) -> int:
    total_minor = _safe_int(row.get("total_minor"))
    if total_minor <= 0:
        total_minor = _amount_to_minor(row.get("total"), row.get("currency"))
    return total_minor


def _not_deleted_sql(alias: str) -> str:
    return (
        f"COALESCE(LOWER(CAST({alias}.is_deleted AS TEXT)), 'false') "
        "NOT IN ('1', 'true', 't', 'yes', 'y')"
    )


def _invoice_finance(start: date, end: date) -> dict[str, Any]:
    conn = get_db()
    try:
        invoices = _table_name("invoices")
        rows = conn.execute(
            f"""
            SELECT id, number, issue_date, due_date, billing_status, payment_status,
                   currency, total, total_minor
            FROM {invoices} i
            WHERE i.issue_date >= ? AND i.issue_date <= ?
              AND COALESCE(LOWER(CAST(i.billing_status AS TEXT)), '') NOT IN ('draft','void')
              AND {_not_deleted_sql("i")}
            ORDER BY i.issue_date DESC, i.id DESC
            LIMIT 5000
            """,
            [start.isoformat(), end.isoformat()],
        ).fetchall()
        expense_rows = conn.execute(
            f"""
            SELECT COALESCE(UPPER(currency), 'USD') AS currency,
                   COALESCE(SUM(total_amount), 0) AS total_amount
            FROM {_table_name("expenses")} e
            WHERE e.expense_date >= ? AND e.expense_date <= ?
              AND {_not_deleted_sql("e")}
            GROUP BY COALESCE(UPPER(currency), 'USD')
            """,
            [start.isoformat(), end.isoformat()],
        ).fetchall()
    finally:
        conn.close()

    billed_by_currency: dict[str, int] = {}
    paid_by_currency: dict[str, int] = {}
    outstanding_by_currency: dict[str, int] = {}
    invoice_count = 0
    overdue_count = 0
    today = date.today()
    for raw in rows:
        row = row_to_dict(raw)
        invoice_id = _safe_int(row.get("id"))
        currency = (row.get("currency") or "USD").upper()
        total = _invoice_total_minor(row)
        paid = _safe_int(PaymentService.get_total_paid(invoice_id)) if invoice_id > 0 else 0
        if str(row.get("payment_status") or "").strip().lower() in {"paid", "overpaid"}:
            paid = max(paid, total)
        outstanding = max(0, total - paid)
        _add_amount(billed_by_currency, currency, total)
        _add_amount(paid_by_currency, currency, paid)
        _add_amount(outstanding_by_currency, currency, outstanding)
        invoice_count += 1
        due_raw = row.get("due_date")
        try:
            due = date.fromisoformat(str(due_raw)[:10]) if due_raw else None
        except Exception:
            due = None
        if due and due < today and outstanding > 0:
            overdue_count += 1

    expenses_by_currency: dict[str, int] = {}
    for raw in expense_rows:
        row = row_to_dict(raw)
        currency = (row.get("currency") or "USD").upper()
        _add_amount(
            expenses_by_currency, currency, _amount_to_minor(row.get("total_amount"), currency)
        )

    gross_margin_by_currency = {
        cur: int(billed_by_currency.get(cur, 0)) - int(expenses_by_currency.get(cur, 0))
        for cur in sorted(set(billed_by_currency) | set(expenses_by_currency))
    }

    return {
        "invoice_count": invoice_count,
        "overdue_invoice_count": overdue_count,
        "billed_by_currency": billed_by_currency,
        "paid_by_currency": paid_by_currency,
        "outstanding_by_currency": outstanding_by_currency,
        "expenses_by_currency": expenses_by_currency,
        "gross_margin_by_currency": gross_margin_by_currency,
    }


def _workflow_quality(start: date, end: date) -> dict[str, Any]:
    completed_q = Workflow.query.filter(
        Workflow.status == Workflow.STATUS_COMPLETED,
        Workflow.completed_date.isnot(None),
        Workflow.completed_date >= start,
        Workflow.completed_date <= end,
    )
    completed = completed_q.all()
    completed_count = len(completed)
    total_hours = sum(float(wf.work_hours or 0) for wf in completed)

    lead_days: list[int] = []
    on_time = 0
    due_measured = 0
    automation_count = 0
    for wf in completed:
        completed_date = wf.completed_date
        start_date = wf.request_start_date
        if not start_date and wf.created_at:
            start_date = wf.created_at.date() if isinstance(wf.created_at, datetime) else None
        if start_date and completed_date:
            lead_days.append(max(0, (completed_date - start_date).days))
        if wf.due_date and completed_date:
            due_measured += 1
            if completed_date <= wf.due_date:
                on_time += 1
        code = str(wf.business_code or "").upper()
        if code.startswith(("DOCKET:", "USPTO:", "USPTO_OA:")) or "AUTO" in code:
            automation_count += 1

    today = date.today()
    open_q = Workflow.query.filter(Workflow.status.notin_(list(Workflow.TERMINAL_STATUSES)))
    overdue_open = open_q.filter(Workflow.due_date.isnot(None), Workflow.due_date < today).count()
    urgent_open = open_q.filter(
        Workflow.due_date.isnot(None),
        Workflow.due_date >= today,
        Workflow.due_date <= today + timedelta(days=7),
    ).count()

    saved_hours = round(automation_count * 0.25, 2)
    return {
        "completed_count": completed_count,
        "total_hours": round(total_hours, 2),
        "avg_lead_days": round(sum(lead_days) / len(lead_days), 1) if lead_days else 0,
        "on_time_rate": round((on_time / due_measured) * 100, 1) if due_measured else 0,
        "due_measured_count": due_measured,
        "overdue_open_count": int(overdue_open or 0),
        "urgent_open_count": int(urgent_open or 0),
        "automation_count": automation_count,
        "automation_saved_hours": saved_hours,
        "automation_roi_rate": round((saved_hours / total_hours) * 100, 1) if total_hours else 0,
    }


def _crm_snapshot(start: date, end: date) -> dict[str, Any]:
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())
    try:
        new_leads = CRMLead.query.filter(
            CRMLead.created_at >= start_dt,
            CRMLead.created_at <= end_dt,
        ).count()
    except Exception:
        new_leads = 0
    open_opportunities = CRMOpportunity.query.filter(
        CRMOpportunity.stage.notin_(["closed_won", "closed_lost"])
    ).count()
    forecast = (
        db.session.query(
            func.coalesce(
                func.sum(
                    (
                        func.coalesce(CRMOpportunity.amount, 0)
                        * func.coalesce(CRMOpportunity.probability, 0)
                    )
                    / 100.0
                ),
                0,
            )
        )
        .filter(CRMOpportunity.stage.notin_(["closed_won", "closed_lost"]))
        .scalar()
        or 0
    )
    return {
        "new_leads": int(new_leads or 0),
        "open_opportunities": int(open_opportunities or 0),
        "forecast_amount": float(forecast or 0),
    }


def _top_open_guardrails(limit: int = 8) -> list[BillingGuardrailFinding]:
    return (
        BillingGuardrailFinding.query.filter(BillingGuardrailFinding.status.in_(OPEN_STATUSES))
        .order_by(
            BillingGuardrailFinding.severity.desc(),
            func.coalesce(BillingGuardrailFinding.gap_amount_minor, 0).desc(),
            BillingGuardrailFinding.last_seen_at.desc(),
        )
        .limit(max(1, min(limit, 50)))
        .all()
    )


def build_executive_analytics(start: date, end: date) -> dict[str, Any]:
    finance = _invoice_finance(start, end)
    workflow = _workflow_quality(start, end)
    crm = _crm_snapshot(start, end)
    guardrail = summarize_guardrail_findings()
    open_guardrail_gap = _safe_int(guardrail.get("open_gap_minor"))
    outstanding_total = sum(int(v or 0) for v in finance["outstanding_by_currency"].values())
    billed_total = sum(int(v or 0) for v in finance["billed_by_currency"].values())
    paid_total = sum(int(v or 0) for v in finance["paid_by_currency"].values())

    risk_score = 0
    if billed_total > 0:
        risk_score += min(40, int((outstanding_total / billed_total) * 100))
    risk_score += min(30, int(finance["overdue_invoice_count"] or 0) * 3)
    risk_score += min(20, int(guardrail.get("open_count") or 0) * 2)
    risk_score += min(10, int(workflow["overdue_open_count"] or 0))

    return {
        "start": start,
        "end": end,
        "finance": finance,
        "workflow": workflow,
        "crm": crm,
        "guardrail": guardrail,
        "top_guardrails": _top_open_guardrails(),
        "collection_rate": round((paid_total / billed_total) * 100, 1) if billed_total else 0,
        "ar_risk_score": min(100, risk_score),
        "open_guardrail_gap_minor": open_guardrail_gap,
    }
