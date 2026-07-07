from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import MatterStaffAssignment
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.billing.db_core import _actual_table_name, get_db, row_to_dict, safe_json_parse
from app.services.billing.utils import to_minor
from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES
from app.utils.workflow_semantics import workflow_primary_owner_user_id

OWNER_BASIS_LABELS = {
    "primary": " Responsible",
    "manager": "Manager",
    "attorney": "Responsible attorney",
    "handler": "Handler",
}
SORT_LABELS = {
    "activity": "Task ",
    "invoice": "Revenue  ",
    "risk": " ",
    "collection": " Open ",
}
_ROLE_PRIORITY = {"admin": 0, "mgmt_director": 1, "lead_attorney": 2, "mgmt_staff": 3}
_ZERO_DECIMAL_CURRENCIES = {"USD", "JPY"}
_MGMT_CATEGORIES_UPPER = {str(value or "").upper() for value in MGMT_CATEGORIES}
_WORK_CATEGORIES_UPPER = {str(value or "").upper() for value in WORK_CATEGORIES}
_OWNER_ROLE_CODES = {
    "primary": ("handler", "attorney", "manager"),
    "manager": ("manager",),
    "attorney": ("attorney",),
    "handler": ("handler",),
}
_COMPLETED_STATUS = "completed"
_ABANDONED_STATUS = "abandoned"


@dataclass
class _InvoiceAttribution:
    invoice_id: int
    number: str
    matter_ids: list[str]
    service_amount: float
    collected_amount: float
    collection_ratio: float
    currency: str
    issue_date: str


def normalize_owner_basis(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in _OWNER_ROLE_CODES:
        return normalized
    return "primary"


def normalize_sort_key(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in SORT_LABELS:
        return normalized
    return "activity"


def default_period(*, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today - timedelta(days=89), today


def clamp_period(
    start: date | None,
    end: date | None,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    default_start, default_end = default_period(today=today)
    start = start or default_start
    end = end or default_end
    if start > end:
        start, end = end, start
    return start, end


def build_user_kpi_dashboard(
    *,
    start: date,
    end: date,
    owner_basis: str = "primary",
    sort_key: str = "activity",
    today: date | None = None,
) -> dict[str, Any]:
    owner_basis = normalize_owner_basis(owner_basis)
    sort_key = normalize_sort_key(sort_key)
    today = today or date.today()

    start, end = clamp_period(start, end, today=today)
    prev_start, prev_end = _previous_period(start=start, end=end)

    users = User.query.all()
    user_map: dict[int, dict[str, Any]] = {}
    user_id_by_staff_party_id: dict[str, int] = {}
    for user in users:
        user_id = _safe_int(getattr(user, "id", None))
        if user_id is None:
            continue
        display_name = (
            (getattr(user, "display_name", None) or "").strip()
            or (getattr(user, "username", None) or "").strip()
            or (getattr(user, "email", None) or "").strip()
            or f"User {user_id}"
        )
        role_name = _primary_role_name(user)
        staff_party_id = (getattr(user, "staff_party_id", None) or "").strip() or None
        user_map[user_id] = {
            "user_id": user_id,
            "display_name": display_name,
            "role": role_name,
            "staff_party_id": staff_party_id,
            "is_active": bool(getattr(user, "is_active", False)),
        }
        if staff_party_id:
            user_id_by_staff_party_id[staff_party_id] = user_id

    workflow_current = _workflow_completed_metrics(start=start, end=end)
    workflow_prev = _workflow_completed_metrics(start=prev_start, end=prev_end)
    workflow_backlog = _workflow_backlog_metrics(today=today)
    worklog_current = _worklog_metrics(
        start=start,
        end=end,
        user_id_by_staff_party_id=user_id_by_staff_party_id,
    )
    worklog_prev = _worklog_metrics(
        start=prev_start,
        end=prev_end,
        user_id_by_staff_party_id=user_id_by_staff_party_id,
    )
    activity_anchor = min(end, today)
    worklog_last_activity = _worklog_last_activity(
        anchor=activity_anchor,
        user_id_by_staff_party_id=user_id_by_staff_party_id,
    )
    docket_metrics = _docket_backlog_metrics(
        today=today, user_id_by_staff_party_id=user_id_by_staff_party_id
    )

    invoice_current = _invoice_attribution_metrics(
        start=start,
        end=end,
        owner_basis=owner_basis,
        user_id_by_staff_party_id=user_id_by_staff_party_id,
    )

    active_user_ids = {user_id for user_id, meta in user_map.items() if meta.get("is_active")}
    row_user_ids = {
        uid
        for uid in (
            list(active_user_ids)
            + list(workflow_current.keys())
            + list(workflow_prev.keys())
            + list(workflow_backlog.keys())
            + list(worklog_current.keys())
            + list(worklog_prev.keys())
            + list(docket_metrics.keys())
            + list(invoice_current["by_user"].keys())
        )
        if uid
    }

    rows: list[dict[str, Any]] = []
    for user_id in row_user_ids:
        user_meta = user_map.get(user_id) or {
            "user_id": user_id,
            "display_name": f"User {user_id}",
            "role": "-",
            "staff_party_id": None,
            "is_active": False,
        }

        workflow_now = workflow_current.get(user_id) or {}
        workflow_then = workflow_prev.get(user_id) or {}
        workflow_pending = workflow_backlog.get(user_id) or {}
        worklog_now = worklog_current.get(user_id) or {}
        worklog_then = worklog_prev.get(user_id) or {}
        docket_now = docket_metrics.get(user_id) or {}
        revenue_now = invoice_current["by_user"].get(user_id) or {}

        workflow_completed = int(workflow_now.get("completed", 0))
        workflow_prev_completed = int(workflow_then.get("completed", 0))
        worklog_completed = int(worklog_now.get("completed", 0))
        worklog_prev_completed = int(worklog_then.get("completed", 0))
        activity_total = workflow_completed + worklog_completed
        prev_activity_total = workflow_prev_completed + worklog_prev_completed
        trend_pct = _trend_pct(activity_total, prev_activity_total)
        worklog_active_days = int(worklog_now.get("active_days", 0))
        worklog_due_tracked_completed = int(worklog_now.get("due_tracked_completed", 0))
        worklog_on_time_completed = int(worklog_now.get("on_time_completed", 0))
        worklog_delayed_completed = int(worklog_now.get("delayed_completed", 0))
        worklog_avg_delay_days = _safe_divide(
            worklog_now.get("delay_days_total", 0.0),
            worklog_delayed_completed,
        )
        worklog_daily_throughput = _safe_divide(worklog_completed, worklog_active_days)
        worklog_on_time_rate = _safe_ratio_pct(
            worklog_on_time_completed,
            worklog_due_tracked_completed,
        )
        worklog_due_coverage = _safe_ratio_pct(worklog_due_tracked_completed, worklog_completed)
        worklog_mgmt_completed = int(worklog_now.get("mgmt_completed", 0))
        worklog_work_completed = int(worklog_now.get("work_completed", 0))
        worklog_hybrid_completed = int(worklog_now.get("hybrid_completed", 0))
        worklog_other_completed = int(worklog_now.get("other_completed", 0))
        worklog_focus_label = _worklog_focus_label(
            mgmt_completed=worklog_mgmt_completed,
            work_completed=worklog_work_completed,
            hybrid_completed=worklog_hybrid_completed,
            other_completed=worklog_other_completed,
        )
        last_activity_date = worklog_last_activity.get(user_id)
        days_since_last_activity = (
            (activity_anchor - last_activity_date).days if last_activity_date else None
        )

        open_workflow = int(workflow_pending.get("open_workflow", 0))
        overdue_workflow = int(workflow_pending.get("overdue_workflow", 0))
        open_docket = int(docket_now.get("open_docket", 0))
        overdue_docket = int(docket_now.get("overdue_docket", 0))

        invoice_count = float(revenue_now.get("invoice_count", 0.0) or 0.0)
        collection_progress = _safe_ratio_pct(
            revenue_now.get("collection_progress_numerator", 0.0),
            revenue_now.get("collection_progress_denominator", 0.0),
        )
        paid_invoice_share = _safe_ratio_pct(
            revenue_now.get("paid_invoice_share_numerator", 0.0),
            revenue_now.get("invoice_count", 0.0),
        )

        row = {
            "user_id": user_id,
            "assignee": user_meta["display_name"],
            "role": user_meta["role"],
            "staff_party_id": user_meta["staff_party_id"],
            "is_active": user_meta["is_active"],
            "workflow_completed": workflow_completed,
            "workflow_prev_completed": workflow_prev_completed,
            "worklog_completed": worklog_completed,
            "worklog_prev_completed": worklog_prev_completed,
            "activity_total": activity_total,
            "prev_activity_total": prev_activity_total,
            "trend_pct": trend_pct,
            "tc_hours": round(float(workflow_now.get("tc_hours", 0.0) or 0.0), 1),
            "worklog_active_days": worklog_active_days,
            "worklog_daily_throughput": worklog_daily_throughput,
            "worklog_due_tracked_completed": worklog_due_tracked_completed,
            "worklog_due_coverage": worklog_due_coverage,
            "worklog_on_time_completed": worklog_on_time_completed,
            "worklog_delayed_completed": worklog_delayed_completed,
            "worklog_on_time_rate": worklog_on_time_rate,
            "worklog_avg_delay_days": worklog_avg_delay_days,
            "worklog_mgmt_completed": worklog_mgmt_completed,
            "worklog_work_completed": worklog_work_completed,
            "worklog_hybrid_completed": worklog_hybrid_completed,
            "worklog_other_completed": worklog_other_completed,
            "worklog_focus_label": worklog_focus_label,
            "worklog_mix_display": _build_worklog_mix_display(
                mgmt_completed=worklog_mgmt_completed,
                work_completed=worklog_work_completed,
                hybrid_completed=worklog_hybrid_completed,
                other_completed=worklog_other_completed,
            ),
            "last_activity_date": last_activity_date,
            "last_activity_display": last_activity_date.isoformat() if last_activity_date else "-",
            "days_since_last_activity": days_since_last_activity,
            "open_workflow": open_workflow,
            "overdue_workflow": overdue_workflow,
            "open_docket": open_docket,
            "overdue_docket": overdue_docket,
            "backlog_total": open_workflow + open_docket,
            "overdue_total": overdue_workflow + overdue_docket,
            "invoice_count": invoice_count,
            "invoice_count_display": _format_count(invoice_count),
            "billed_by_currency": dict(revenue_now.get("billed_by_currency") or {}),
            "billed_display": format_currency_totals(revenue_now.get("billed_by_currency") or {}),
            "collected_by_currency": dict(revenue_now.get("collected_by_currency") or {}),
            "collected_display": format_currency_totals(
                revenue_now.get("collected_by_currency") or {}
            ),
            "collection_progress": collection_progress,
            "paid_invoice_share": paid_invoice_share,
            "insight_tags": [],
        }
        rows.append(row)

    _attach_row_insights(rows)
    rows = _sort_rows(rows, sort_key=sort_key)

    summary = _build_summary(
        rows=rows,
        unattributed=invoice_current["unattributed"],
        start=start,
        end=end,
        owner_basis=owner_basis,
    )
    highlights = _build_highlights(rows=rows, summary=summary)
    charts = _build_charts(rows=rows)

    return {
        "filters": {
            "start": start,
            "end": end,
            "owner_basis": owner_basis,
            "owner_basis_label": OWNER_BASIS_LABELS[owner_basis],
            "sort_key": sort_key,
            "sort_label": SORT_LABELS[sort_key],
        },
        "summary": summary,
        "rows": rows,
        "highlights": highlights,
        "charts": charts,
        "owner_basis_options": [
            {"value": value, "label": OWNER_BASIS_LABELS[value]}
            for value in ("primary", "manager", "attorney", "handler")
        ],
        "sort_options": [
            {"value": value, "label": SORT_LABELS[value]}
            for value in ("activity", "invoice", "risk", "collection")
        ],
        "unattributed_examples": invoice_current["unattributed_examples"],
    }


def format_currency_totals(values: dict[str, float] | None) -> str:
    values = values or {}
    parts: list[str] = []
    for currency, amount in sorted(values.items()):
        cur = (currency or "USD").strip().upper() or "USD"
        try:
            numeric = float(amount or 0.0)
        except Exception:
            numeric = 0.0
        if abs(numeric) < 0.0001:
            continue
        digits = 0 if cur in _ZERO_DECIMAL_CURRENCIES else 2
        parts.append(f"{cur} {numeric:,.{digits}f}")
    return " / ".join(parts) if parts else "-"


def _workflow_completed_metrics(*, start: date, end: date) -> dict[int, dict[str, float]]:
    rows: dict[int, dict[str, float]] = defaultdict(lambda: {"completed": 0, "tc_hours": 0.0})
    completed_filter = (
        func.lower(func.coalesce(func.trim(Workflow.status), "")) == _COMPLETED_STATUS
    )
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end + timedelta(days=1), time.min)
    window_filter = or_(
        (
            Workflow.completed_date.isnot(None)
            & (Workflow.completed_date >= start)
            & (Workflow.completed_date <= end)
        ),
        (
            Workflow.completed_date.is_(None)
            & (Workflow.created_at >= start_dt)
            & (Workflow.created_at < end_dt)
        ),
    )
    q = (
        db.session.query(
            Workflow.category,
            Workflow.assignee_id,
            Workflow.attorney_assignee_id,
            Workflow.inspector_id,
            Workflow.work_hours,
        )
        .filter(completed_filter)
        .filter(window_filter)
    )
    for category, handler_id, attorney_id, manager_id, work_hours in q.all():
        owner_id = _workflow_owner_user_id(
            category=category,
            handler_id=handler_id,
            attorney_id=attorney_id,
            manager_id=manager_id,
        )
        if owner_id is None:
            continue
        rows[owner_id]["completed"] += 1
        rows[owner_id]["tc_hours"] += float(work_hours or 0.0)
    return rows


def _workflow_backlog_metrics(*, today: date) -> dict[int, dict[str, int]]:
    rows: dict[int, dict[str, int]] = defaultdict(
        lambda: {"open_workflow": 0, "overdue_workflow": 0}
    )
    open_filter = func.lower(func.coalesce(func.trim(Workflow.status), "")) != _COMPLETED_STATUS
    q = db.session.query(
        Workflow.category,
        Workflow.assignee_id,
        Workflow.attorney_assignee_id,
        Workflow.inspector_id,
        Workflow.due_date,
    ).filter(open_filter)
    for category, handler_id, attorney_id, manager_id, due_date in q.all():
        owner_id = _workflow_owner_user_id(
            category=category,
            handler_id=handler_id,
            attorney_id=attorney_id,
            manager_id=manager_id,
        )
        if owner_id is None:
            continue
        rows[owner_id]["open_workflow"] += 1
        if due_date and due_date < today:
            rows[owner_id]["overdue_workflow"] += 1
    return rows


def _worklog_metrics(
    *,
    start: date,
    end: date,
    user_id_by_staff_party_id: dict[str, int],
) -> dict[int, dict[str, float | int]]:
    rows: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "completed": 0,
            "due_tracked_completed": 0,
            "on_time_completed": 0,
            "delayed_completed": 0,
            "delay_days_total": 0.0,
            "mgmt_completed": 0,
            "work_completed": 0,
            "hybrid_completed": 0,
            "other_completed": 0,
            "_active_dates": set(),
        }
    )
    completed_expr = _worklog_completed_filter()
    ts_expr = func.coalesce(WorkLog.completed_at, WorkLog.updated_at, WorkLog.created_at)
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end + timedelta(days=1), time.min)
    q = (
        db.session.query(
            WorkLog.completed_by_id,
            WorkLog.owner_staff_party_id,
            WorkLog.task_category,
            WorkLog.due_date,
            ts_expr,
        )
        .filter(completed_expr)
        .filter(ts_expr >= start_dt)
        .filter(ts_expr < end_dt)
    )
    for completed_by_id, owner_staff_party_id, task_category, due_date, completed_ts in q.all():
        user_id = _resolve_worklog_user_id(
            completed_by_id=completed_by_id,
            owner_staff_party_id=owner_staff_party_id,
            user_id_by_staff_party_id=user_id_by_staff_party_id,
        )
        if user_id is None:
            continue
        completed_date = _parse_date_like(completed_ts)
        if completed_date is None:
            continue
        bucket = _worklog_bucket(task_category)
        metrics = rows[user_id]
        metrics["completed"] += 1
        metrics[f"{bucket}_completed"] += 1
        cast_active_dates = metrics.get("_active_dates")
        if isinstance(cast_active_dates, set):
            cast_active_dates.add(completed_date)
        parsed_due = _parse_date_like(due_date)
        if parsed_due is None:
            continue
        metrics["due_tracked_completed"] += 1
        if completed_date <= parsed_due:
            metrics["on_time_completed"] += 1
        else:
            metrics["delayed_completed"] += 1
            metrics["delay_days_total"] += float((completed_date - parsed_due).days)

    finalized: dict[int, dict[str, float | int]] = {}
    for user_id, metrics in rows.items():
        active_dates = metrics.pop("_active_dates", set())
        finalized[user_id] = {
            **metrics,
            "active_days": len(active_dates) if isinstance(active_dates, set) else 0,
        }
    return finalized


def _worklog_last_activity(
    *,
    anchor: date,
    user_id_by_staff_party_id: dict[str, int],
) -> dict[int, date]:
    rows: dict[int, date] = {}
    completed_expr = _worklog_completed_filter()
    ts_expr = func.coalesce(WorkLog.completed_at, WorkLog.updated_at, WorkLog.created_at)
    anchor_dt = datetime.combine(anchor + timedelta(days=1), time.min)
    q = (
        db.session.query(WorkLog.completed_by_id, WorkLog.owner_staff_party_id, ts_expr)
        .filter(completed_expr)
        .filter(ts_expr < anchor_dt)
    )
    for completed_by_id, owner_staff_party_id, completed_ts in q.all():
        user_id = _resolve_worklog_user_id(
            completed_by_id=completed_by_id,
            owner_staff_party_id=owner_staff_party_id,
            user_id_by_staff_party_id=user_id_by_staff_party_id,
        )
        if user_id is None:
            continue
        completed_date = _parse_date_like(completed_ts)
        if completed_date is None:
            continue
        previous = rows.get(user_id)
        if previous is None or completed_date > previous:
            rows[user_id] = completed_date
    return rows


def _docket_backlog_metrics(
    *,
    today: date,
    user_id_by_staff_party_id: dict[str, int],
) -> dict[int, dict[str, int]]:
    rows: dict[int, dict[str, int]] = defaultdict(lambda: {"open_docket": 0, "overdue_docket": 0})
    effective_due = func.coalesce(
        func.nullif(DocketItem.extended_due_date, ""),
        func.nullif(DocketItem.due_date, ""),
    )
    open_filter = func.trim(func.coalesce(DocketItem.done_date, "")) == ""
    q = (
        db.session.query(DocketItem.owner_staff_party_id, effective_due)
        .filter(DocketItem.owner_staff_party_id.isnot(None))
        .filter(open_filter)
    )
    if hasattr(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))

    for owner_staff_party_id, effective_due_value in q.all():
        owner_key = (owner_staff_party_id or "").strip()
        user_id = user_id_by_staff_party_id.get(owner_key)
        if not owner_key or user_id is None:
            continue
        rows[user_id]["open_docket"] += 1
        parsed_due = _parse_date_like(effective_due_value)
        if parsed_due and parsed_due < today:
            rows[user_id]["overdue_docket"] += 1
    return rows


def _invoice_attribution_metrics(
    *,
    start: date,
    end: date,
    owner_basis: str,
    user_id_by_staff_party_id: dict[str, int],
) -> dict[str, Any]:
    invoices = _fetch_invoice_rows(start=start, end=end)
    by_user: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "invoice_count": 0.0,
            "paid_invoice_share_numerator": 0.0,
            "collection_progress_numerator": 0.0,
            "collection_progress_denominator": 0.0,
            "billed_by_currency": defaultdict(float),
            "collected_by_currency": defaultdict(float),
        }
    )
    unattributed = {
        "invoice_count": 0,
        "billed_by_currency": defaultdict(float),
    }
    unattributed_examples: list[dict[str, str]] = []

    if not invoices:
        return {
            "by_user": {},
            "unattributed": unattributed,
            "unattributed_examples": [],
        }

    matter_owner_by_id = _resolve_matter_owners(
        matter_ids={matter_id for invoice in invoices for matter_id in invoice.matter_ids},
        owner_basis=owner_basis,
        user_id_by_staff_party_id=user_id_by_staff_party_id,
    )

    for invoice in invoices:
        user_ids = sorted(
            {
                matter_owner_by_id.get(matter_id)
                for matter_id in invoice.matter_ids
                if matter_owner_by_id.get(matter_id)
            }
        )
        share_factor = 1.0 / len(user_ids) if user_ids else 0.0
        if not user_ids:
            unattributed["invoice_count"] += 1
            unattributed["billed_by_currency"][invoice.currency] += invoice.service_amount
            if len(unattributed_examples) < 8:
                unattributed_examples.append(
                    {
                        "number": invoice.number,
                        "date": invoice.issue_date,
                        "currency": invoice.currency,
                    }
                )
            continue

        paid_share = 1.0 if invoice.collection_ratio >= 0.999 else 0.0
        for user_id in user_ids:
            metrics = by_user[user_id]
            metrics["invoice_count"] += share_factor
            metrics["paid_invoice_share_numerator"] += paid_share * share_factor
            metrics["collection_progress_numerator"] += invoice.collection_ratio * share_factor
            metrics["collection_progress_denominator"] += share_factor
            metrics["billed_by_currency"][invoice.currency] += invoice.service_amount * share_factor
            metrics["collected_by_currency"][invoice.currency] += (
                invoice.collected_amount * share_factor
            )

    return {
        "by_user": by_user,
        "unattributed": unattributed,
        "unattributed_examples": unattributed_examples,
    }


def _fetch_invoice_rows(*, start: date, end: date) -> list[_InvoiceAttribution]:
    conn = get_db()
    try:
        invoices_tbl = _actual_table_name("invoices")
        line_items_tbl = _actual_table_name("line_items")
        payments_tbl = _actual_table_name("invoice_payments")
        invoice_case_map_tbl = _actual_table_name("invoice_case_map")
        external_case_map_tbl = _actual_table_name("external_invoice_case_map")

        foreign_total_sql = f"""
            COALESCE((
                SELECT SUM(
                    CASE
                        WHEN COALESCE(li.fx_rate_used, 0) > 0 THEN
                            (COALESCE(li.fx_fee, 0) + COALESCE(li.fx_gov, 0))
                            * COALESCE(li.fx_rate_used, 0)
                            * (1 + COALESCE(li.fx_markup, 0) / 100.0)
                        ELSE
                            li.qty * li.unit_price * (1 - COALESCE(li.discount, 0) / 100.0)
                    END
                )
                FROM {line_items_tbl} li
                WHERE li.invoice_id = i.id
                  AND li.item_type = 'foreign'
                  AND (li.is_estimated IS NULL OR li.is_estimated = 0)
            ), 0.0)
        """
        admin_total_sql = f"""
            COALESCE((
                SELECT SUM(li.qty * li.unit_price * (1 - COALESCE(li.discount, 0) / 100.0))
                FROM {line_items_tbl} li
                WHERE li.invoice_id = i.id
                  AND li.item_type = 'admin'
                  AND (li.is_estimated IS NULL OR li.is_estimated = 0)
            ), 0.0)
        """

        invoice_rows = conn.execute(
            f"""
            SELECT
                i.id,
                i.number,
                i.issue_date,
                COALESCE(NULLIF(i.currency, ''), 'USD') AS currency,
                i.billing_status,
                i.payment_status,
                i.payment_meta,
                i.total,
                i.subtotal,
                i.ipm_case_id,
                {admin_total_sql} AS admin_total,
                {foreign_total_sql} AS foreign_total,
                COALESCE((
                    SELECT SUM(p.amount_minor)
                    FROM {payments_tbl} p
                    WHERE p.invoice_id = i.id
                      AND COALESCE(p.is_deleted, 0) = 0
                ), 0) AS paid_minor
            FROM {invoices_tbl} i
            WHERE i.issue_date >= ? AND i.issue_date <= ?
              AND COALESCE(NULLIF(i.billing_status, ''), COALESCE(NULLIF(i.status, ''), 'sent')) NOT IN ('draft', 'void')
            ORDER BY i.issue_date DESC, i.id DESC
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()

        invoice_ids = [int(row["id"]) for row in invoice_rows if _safe_int(row["id"]) is not None]
        linked_matter_ids = _fetch_invoice_matter_links(
            conn=conn,
            invoice_ids=invoice_ids,
            invoice_case_map_tbl=invoice_case_map_tbl,
            external_case_map_tbl=external_case_map_tbl,
        )

        out: list[_InvoiceAttribution] = []
        for row in invoice_rows:
            data = row_to_dict(row)
            invoice_id = int(data["id"])
            currency = (data.get("currency") or "USD").strip().upper() or "USD"
            subtotal = _safe_float(data.get("subtotal"))
            admin_total = _safe_float(data.get("admin_total"))
            foreign_total = _safe_float(data.get("foreign_total"))
            service_amount = max(subtotal - admin_total - foreign_total, 0.0)
            total_amount = _safe_float(data.get("total"))
            total_minor = _amount_to_minor(total_amount, currency)
            paid_minor = _safe_int(data.get("paid_minor")) or 0
            legacy_paid_minor = _parse_payment_meta_minor(data.get("payment_meta"), currency)
            if paid_minor <= 0 and legacy_paid_minor > 0:
                paid_minor = legacy_paid_minor
            elif legacy_paid_minor > paid_minor:
                paid_minor = legacy_paid_minor
            payment_status = (data.get("payment_status") or "").strip().lower()
            if payment_status in {"paid", "overpaid"} and total_minor > 0:
                paid_minor = max(paid_minor, total_minor)
            collection_ratio = 0.0
            if total_minor > 0:
                collection_ratio = max(0.0, min(float(paid_minor) / float(total_minor), 1.0))
            elif payment_status in {"paid", "overpaid"}:
                collection_ratio = 1.0

            matter_ids = list(linked_matter_ids.get(invoice_id) or [])
            ipm_case_id = (data.get("ipm_case_id") or "").strip()
            if ipm_case_id and ipm_case_id not in matter_ids:
                matter_ids.append(ipm_case_id)

            out.append(
                _InvoiceAttribution(
                    invoice_id=invoice_id,
                    number=(data.get("number") or f"INV-{invoice_id}").strip()
                    or f"INV-{invoice_id}",
                    matter_ids=matter_ids,
                    service_amount=service_amount,
                    collected_amount=service_amount * collection_ratio,
                    collection_ratio=collection_ratio,
                    currency=currency,
                    issue_date=_stringify_date(data.get("issue_date")),
                )
            )
        return out
    finally:
        conn.close()


def _fetch_invoice_matter_links(
    *,
    conn,
    invoice_ids: list[int],
    invoice_case_map_tbl: str,
    external_case_map_tbl: str,
) -> dict[int, list[str]]:
    links: dict[int, list[str]] = defaultdict(list)
    if not invoice_ids:
        return links

    qmarks = ", ".join("?" for _ in invoice_ids)
    for sql in (
        f"""
        SELECT invoice_id AS linked_invoice_id, matter_id
        FROM {invoice_case_map_tbl}
        WHERE invoice_id IN ({qmarks})
          AND matter_id IS NOT NULL
          AND TRIM(COALESCE(matter_id, '')) <> ''
        """,
        f"""
        SELECT external_invoice_id AS linked_invoice_id, matter_id
        FROM {external_case_map_tbl}
        WHERE external_invoice_id IN ({qmarks})
          AND matter_id IS NOT NULL
          AND TRIM(COALESCE(matter_id, '')) <> ''
        """,
    ):
        rows = conn.execute(sql, invoice_ids).fetchall()
        for row in rows:
            invoice_id = _safe_int(row["linked_invoice_id"])
            matter_id = (row["matter_id"] or "").strip()
            if invoice_id is None or not matter_id:
                continue
            bucket = links[invoice_id]
            if matter_id not in bucket:
                bucket.append(matter_id)
    return links


def _resolve_matter_owners(
    *,
    matter_ids: set[str],
    owner_basis: str,
    user_id_by_staff_party_id: dict[str, int],
) -> dict[str, int]:
    if not matter_ids:
        return {}

    role_codes = (
        _OWNER_ROLE_CODES["primary"] if owner_basis == "primary" else _OWNER_ROLE_CODES[owner_basis]
    )
    q = (
        db.session.query(
            MatterStaffAssignment.matter_id,
            MatterStaffAssignment.staff_role_code,
            MatterStaffAssignment.staff_party_id,
            MatterStaffAssignment.seq,
        )
        .filter(MatterStaffAssignment.matter_id.in_(sorted(matter_ids)))
        .filter(MatterStaffAssignment.staff_role_code.in_(list(_OWNER_ROLE_CODES["primary"])))
        .order_by(
            MatterStaffAssignment.matter_id.asc(),
            MatterStaffAssignment.seq.asc(),
            MatterStaffAssignment.msa_id.asc(),
        )
    )

    by_matter: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for matter_id, role_code, staff_party_id, _seq in q.all():
        mid = (matter_id or "").strip()
        role = (role_code or "").strip().lower()
        staff_id = (staff_party_id or "").strip()
        user_id = user_id_by_staff_party_id.get(staff_id)
        if not mid or not role or user_id is None:
            continue
        existing = by_matter[mid][role]
        if user_id not in existing:
            existing.append(user_id)

    resolved: dict[str, int] = {}
    for matter_id in matter_ids:
        candidates = by_matter.get(matter_id) or {}
        for role_code in role_codes:
            user_ids = candidates.get(role_code) or []
            if user_ids:
                resolved[matter_id] = user_ids[0]
                break
    return resolved


def _build_summary(
    *,
    rows: list[dict[str, Any]],
    unattributed: dict[str, Any],
    start: date,
    end: date,
    owner_basis: str,
) -> dict[str, Any]:
    active_rows = [row for row in rows if row.get("is_active")]
    activity_total = sum(int(row.get("activity_total") or 0) for row in rows)
    prev_activity_total = sum(int(row.get("prev_activity_total") or 0) for row in rows)
    attributed_invoice_count = sum(float(row.get("invoice_count") or 0.0) for row in rows)
    billed_by_currency = _sum_currency_dicts([row.get("billed_by_currency") or {} for row in rows])
    collected_by_currency = _sum_currency_dicts(
        [row.get("collected_by_currency") or {} for row in rows]
    )
    worklog_completed_total = sum(int(row.get("worklog_completed") or 0) for row in rows)
    worklog_due_tracked_completed = sum(
        int(row.get("worklog_due_tracked_completed") or 0) for row in rows
    )
    worklog_on_time_completed = sum(int(row.get("worklog_on_time_completed") or 0) for row in rows)
    worklog_delayed_completed = sum(int(row.get("worklog_delayed_completed") or 0) for row in rows)
    worklog_delay_days_total = sum(
        float(row.get("worklog_avg_delay_days") or 0.0)
        * int(row.get("worklog_delayed_completed") or 0)
        for row in rows
    )
    cadence_rows = [row for row in rows if int(row.get("worklog_active_days") or 0) > 0]
    active_rows_with_history = [
        row for row in active_rows if row.get("days_since_last_activity") is not None
    ]
    backlog_total = sum(int(row.get("backlog_total") or 0) for row in rows)
    overdue_total = sum(int(row.get("overdue_total") or 0) for row in rows)
    unattributed_invoice_count = int(unattributed.get("invoice_count") or 0)
    total_invoice_count = attributed_invoice_count + float(unattributed_invoice_count)

    return {
        "start": start,
        "end": end,
        "period_days": (end - start).days + 1,
        "owner_basis_label": OWNER_BASIS_LABELS[owner_basis],
        "active_staff_count": len(active_rows),
        "visible_user_count": len(rows),
        "activity_total": activity_total,
        "prev_activity_total": prev_activity_total,
        "activity_trend_pct": _trend_pct(activity_total, prev_activity_total),
        "workflow_completed_total": sum(int(row.get("workflow_completed") or 0) for row in rows),
        "worklog_completed_total": worklog_completed_total,
        "worklog_due_tracked_completed": worklog_due_tracked_completed,
        "worklog_due_coverage": _safe_ratio_pct(
            worklog_due_tracked_completed,
            worklog_completed_total,
        ),
        "worklog_on_time_rate": _safe_ratio_pct(
            worklog_on_time_completed,
            worklog_due_tracked_completed,
        ),
        "worklog_delayed_completed": worklog_delayed_completed,
        "worklog_avg_delay_days": _safe_divide(
            worklog_delay_days_total,
            worklog_delayed_completed,
        ),
        "worklog_active_days_avg": _safe_average(
            [float(row.get("worklog_active_days") or 0) for row in cadence_rows]
        ),
        "worklog_daily_throughput_avg": _safe_average(
            [float(row.get("worklog_daily_throughput") or 0.0) for row in cadence_rows]
        ),
        "recent_active_staff_count": sum(
            1
            for row in active_rows_with_history
            if int(row.get("days_since_last_activity") or 9999) <= 7
        ),
        "stale_staff_count": sum(
            1
            for row in active_rows_with_history
            if int(row.get("days_since_last_activity") or 0) >= 14
        ),
        "backlog_total": backlog_total,
        "overdue_total": overdue_total,
        "attributed_invoice_count": attributed_invoice_count,
        "attributed_invoice_count_display": _format_count(attributed_invoice_count),
        "unattributed_invoice_count": unattributed_invoice_count,
        "invoice_capture_rate": _safe_ratio_pct(attributed_invoice_count, total_invoice_count),
        "billed_by_currency": billed_by_currency,
        "billed_display": format_currency_totals(billed_by_currency),
        "collected_by_currency": collected_by_currency,
        "collected_display": format_currency_totals(collected_by_currency),
        "collection_progress_avg": _safe_average(
            [
                float(row.get("collection_progress") or 0.0)
                for row in rows
                if row.get("invoice_count")
            ]
        ),
        "unattributed_billed_display": format_currency_totals(
            unattributed.get("billed_by_currency") or {}
        ),
    }


def _build_highlights(
    *, rows: list[dict[str, Any]], summary: dict[str, Any]
) -> list[dict[str, str]]:
    highlights: list[dict[str, str]] = []
    with_invoices = [row for row in rows if float(row.get("invoice_count") or 0.0) > 0]
    if rows:
        top_activity = max(
            rows, key=lambda row: (row.get("activity_total") or 0, -(row.get("overdue_total") or 0))
        )
        highlights.append(
            {
                "title": "Task Process ",
                "body": f"{top_activity['assignee']} · Done {top_activity['activity_total']}items / TC {top_activity['tc_hours']}h",
            }
        )
    disciplined_rows = [
        row for row in rows if int(row.get("worklog_due_tracked_completed") or 0) >= 2
    ]
    if disciplined_rows:
        top_discipline = max(
            disciplined_rows,
            key=lambda row: (
                float(row.get("worklog_on_time_rate") or 0.0),
                int(row.get("worklog_due_tracked_completed") or 0),
                int(row.get("worklog_active_days") or 0),
            ),
        )
        highlights.append(
            {
                "title": "Deadline  ",
                "body": (
                    f"{top_discipline['assignee']} ·  {top_discipline['worklog_on_time_rate']:.1f}%"
                    f" / Deadline  {top_discipline['worklog_due_tracked_completed']}items"
                ),
            }
        )
    if with_invoices:
        top_invoice = max(
            with_invoices,
            key=lambda row: (
                float(row.get("invoice_count") or 0.0),
                float(row.get("collection_progress") or 0.0),
            ),
        )
        highlights.append(
            {
                "title": "Revenue  ",
                "body": f"{top_invoice['assignee']} ·  {top_invoice['invoice_count_display']}items / {top_invoice['billed_display']}",
            }
        )
    risky_rows = [row for row in rows if int(row.get("overdue_total") or 0) > 0]
    gap_rows = [
        row
        for row in rows
        if row.get("is_active") and row.get("days_since_last_activity") is not None
    ]
    if risky_rows:
        top_risk = max(
            risky_rows,
            key=lambda row: (row.get("overdue_total") or 0, row.get("backlog_total") or 0),
        )
        highlights.append(
            {
                "title": "  Required",
                "body": f"{top_risk['assignee']} ·  {top_risk['overdue_total']}items / backlog {top_risk['backlog_total']}items",
            }
        )
    if gap_rows:
        top_gap = max(
            gap_rows,
            key=lambda row: (
                row.get("days_since_last_activity") or 0,
                row.get("backlog_total") or 0,
            ),
        )
        if int(top_gap.get("days_since_last_activity") or 0) >= 10:
            highlights.append(
                {
                    "title": "Recent  ",
                    "body": (
                        f"{top_gap['assignee']} · Recent Done {top_gap['days_since_last_activity']} "
                        f" / Task Log {top_gap['worklog_completed']}items"
                    ),
                }
            )
    highlights.append(
        {
            "title": " ",
            "body": f"Invoice {summary['invoice_capture_rate']:.1f}%  ·  {summary['unattributed_invoice_count']}items",
        }
    )
    return highlights


def _build_charts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    top_activity_rows = [row for row in rows if int(row.get("activity_total") or 0) > 0][:12]
    activity_chart = {
        "labels": [row["assignee"] for row in top_activity_rows],
        "activity": [int(row.get("activity_total") or 0) for row in top_activity_rows],
        "overdue": [int(row.get("overdue_total") or 0) for row in top_activity_rows],
    }
    quality_rows = sorted(
        [row for row in rows if int(row.get("worklog_completed") or 0) > 0],
        key=lambda row: (
            -(row.get("worklog_completed") or 0),
            -(row.get("worklog_active_days") or 0),
            row.get("assignee") or "",
        ),
    )[:10]
    worklog_quality_chart = {
        "labels": [row["assignee"] for row in quality_rows],
        "on_time_rate": [float(row.get("worklog_on_time_rate") or 0.0) for row in quality_rows],
        "daily_throughput": [
            float(row.get("worklog_daily_throughput") or 0.0) for row in quality_rows
        ],
    }
    scatter_points = []
    for row in rows[:18]:
        activity = int(row.get("activity_total") or 0)
        invoices = float(row.get("invoice_count") or 0.0)
        overdue = int(row.get("overdue_total") or 0)
        if activity <= 0 and invoices <= 0:
            continue
        scatter_points.append(
            {
                "x": activity,
                "y": round(invoices, 2),
                "r": min(18, 7 + overdue * 2),
                "label": row["assignee"],
                "overdue": overdue,
            }
        )
    return {
        "activity": activity_chart,
        "worklog_quality": worklog_quality_chart,
        "scatter": scatter_points,
    }


def _attach_row_insights(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    avg_activity = _safe_average([row.get("activity_total", 0) or 0 for row in rows])
    avg_invoice_count = _safe_average([float(row.get("invoice_count") or 0.0) for row in rows])
    avg_overdue = _safe_average([row.get("overdue_total", 0) or 0 for row in rows])
    avg_cadence = _safe_average(
        [
            float(row.get("worklog_daily_throughput") or 0.0)
            for row in rows
            if row.get("worklog_active_days")
        ]
    )

    for row in rows:
        tags: list[str] = []
        if row.get("is_active") and int(row.get("days_since_last_activity") or 0) >= 14:
            tags.append(" ")
        if (row.get("overdue_total") or 0) >= max(2, avg_overdue * 1.5):
            tags.append(" ")
        if (
            float(row.get("worklog_on_time_rate") or 0.0) >= 90.0
            and int(row.get("worklog_due_tracked_completed") or 0) >= 3
        ):
            tags.append("Deadline ")
        if (row.get("activity_total") or 0) >= max(3, avg_activity * 1.2):
            tags.append("Task ")
        if (
            float(row.get("worklog_daily_throughput") or 0.0) >= max(1.0, avg_cadence * 1.2)
            and int(row.get("worklog_active_days") or 0) >= 3
        ):
            tags.append(" ")
        if float(row.get("invoice_count") or 0.0) >= max(1.0, avg_invoice_count * 1.2):
            tags.append("Revenue ")
        if (
            float(row.get("collection_progress") or 0.0) >= 90.0
            and float(row.get("invoice_count") or 0.0) >= 1.0
        ):
            tags.append(" ")
        if not tags:
            tags.append("")
        row["insight_tags"] = tags[:3]


def _sort_rows(rows: list[dict[str, Any]], *, sort_key: str) -> list[dict[str, Any]]:
    if sort_key == "risk":
        return sorted(
            rows,
            key=lambda row: (
                -(row.get("overdue_total") or 0),
                float(
                    row.get("worklog_on_time_rate")
                    if row.get("worklog_due_tracked_completed")
                    else 100.0
                ),
                -(row.get("days_since_last_activity") or 0),
                -(row.get("backlog_total") or 0),
                -(row.get("activity_total") or 0),
                row.get("assignee") or "",
            ),
        )
    if sort_key == "invoice":
        return sorted(
            rows,
            key=lambda row: (
                -float(row.get("invoice_count") or 0.0),
                -(row.get("activity_total") or 0),
                -float(row.get("collection_progress") or 0.0),
                row.get("assignee") or "",
            ),
        )
    if sort_key == "collection":
        return sorted(
            rows,
            key=lambda row: (
                -float(row.get("collection_progress") or 0.0),
                -float(row.get("invoice_count") or 0.0),
                -(row.get("activity_total") or 0),
                row.get("assignee") or "",
            ),
        )
    return sorted(
        rows,
        key=lambda row: (
            -(row.get("activity_total") or 0),
            -(row.get("worklog_active_days") or 0),
            -float(row.get("worklog_daily_throughput") or 0.0),
            -float(row.get("invoice_count") or 0.0),
            -(row.get("overdue_total") or 0),
            row.get("assignee") or "",
        ),
    )


def _sum_currency_dicts(values: list[dict[str, float]]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for value in values:
        for currency, amount in (value or {}).items():
            out[(currency or "USD").strip().upper() or "USD"] += _safe_float(amount)
    return dict(out)


def _workflow_owner_user_id(
    *,
    category: str | None,
    handler_id: Any,
    attorney_id: Any,
    manager_id: Any,
) -> int | None:
    owner_id = workflow_primary_owner_user_id(
        category=category,
        handler_id=_safe_int(handler_id),
        attorney_id=_safe_int(attorney_id),
        manager_id=_safe_int(manager_id),
    )
    return _safe_int(owner_id)


def _worklog_completed_filter():
    status_expr = func.lower(func.coalesce(WorkLog.status, ""))
    return or_(
        status_expr == _COMPLETED_STATUS,
        and_(WorkLog.completed_at.isnot(None), status_expr != _ABANDONED_STATUS),
    )


def _resolve_worklog_user_id(
    *,
    completed_by_id: Any,
    owner_staff_party_id: Any,
    user_id_by_staff_party_id: dict[str, int],
) -> int | None:
    direct_user_id = _safe_int(completed_by_id)
    if direct_user_id is not None:
        return direct_user_id
    owner_key = str(owner_staff_party_id or "").strip()
    if not owner_key:
        return None
    return user_id_by_staff_party_id.get(owner_key)


def _worklog_bucket(task_category: Any) -> str:
    normalized = str(task_category or "").strip().upper()
    if normalized in _MGMT_CATEGORIES_UPPER and normalized in _WORK_CATEGORIES_UPPER:
        return "hybrid"
    if normalized in _MGMT_CATEGORIES_UPPER:
        return "mgmt"
    if normalized in _WORK_CATEGORIES_UPPER:
        return "work"
    return "other"


def _build_worklog_mix_display(
    *,
    mgmt_completed: int,
    work_completed: int,
    hybrid_completed: int,
    other_completed: int,
) -> str:
    parts: list[str] = []
    if work_completed > 0:
        parts.append(f"WORK {work_completed}")
    if mgmt_completed > 0:
        parts.append(f"MGMT {mgmt_completed}")
    if hybrid_completed > 0:
        parts.append(f"MIX {hybrid_completed}")
    if other_completed > 0:
        parts.append(f"Other {other_completed}")
    return " / ".join(parts) if parts else "-"


def _worklog_focus_label(
    *,
    mgmt_completed: int,
    work_completed: int,
    hybrid_completed: int,
    other_completed: int,
) -> str:
    counts = {
        "MGMT": mgmt_completed,
        "WORK": work_completed,
        "MIX": hybrid_completed,
        "Other": other_completed,
    }
    total = sum(counts.values())
    if total <= 0:
        return "-"
    label, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    if count * 100 < total * 60:
        return ""
    return label


def _parse_payment_meta_minor(meta_val: Any, currency: str) -> int:
    meta = safe_json_parse(meta_val, {})
    if not isinstance(meta, dict):
        return 0
    deposit = meta.get("deposit")
    if deposit is None and isinstance(meta.get("deposits"), list):
        total = 0
        for record in meta.get("deposits") or []:
            if not isinstance(record, dict):
                continue
            try:
                total += int(str(record.get("deposit") or "0").replace(",", "").strip())
            except Exception:
                continue
        deposit = total
    if deposit is None:
        return 0
    cur = (meta.get("currency") or currency or "USD").strip().upper() or "USD"
    if cur == "USD":
        try:
            return int(str(deposit or "0").replace(",", "").replace(" ", ""))
        except Exception:
            return 0
    try:
        return int(to_minor(Decimal(str(deposit).replace(",", "")), cur))
    except Exception:
        return 0


def _amount_to_minor(amount: float, currency: str) -> int:
    try:
        return int(to_minor(Decimal(str(amount or 0.0)), currency or "USD"))
    except Exception:
        return 0


def _previous_period(*, start: date, end: date) -> tuple[date, date]:
    period_days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)
    return prev_start, prev_end


def _primary_role_name(user: User) -> str:
    try:
        role_names = sorted(
            [name for name in getattr(user, "role_names", set()) if name],
            key=lambda value: (_ROLE_PRIORITY.get(value, 99), value),
        )
    except Exception:
        role_names = []
    if role_names:
        return role_names[0]
    return (getattr(user, "role", None) or "user").strip().lower() or "user"


def _format_count(value: float) -> str:
    try:
        numeric = float(value or 0.0)
    except Exception:
        numeric = 0.0
    if abs(numeric - round(numeric)) < 0.001:
        return f"{int(round(numeric)):,}"
    return f"{numeric:,.1f}"


def _stringify_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _parse_date_like(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def _safe_average(values: list[float]) -> float:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return 0.0
    return round(sum(cleaned) / len(cleaned), 1)


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except Exception:
        return None
    return normalized if normalized > 0 else None


def _safe_divide(numerator: Any, denominator: Any) -> float:
    den = _safe_float(denominator)
    if den <= 0:
        return 0.0
    return round(_safe_float(numerator) / den, 1)


def _safe_ratio_pct(numerator: Any, denominator: Any) -> float:
    num = _safe_float(numerator)
    den = _safe_float(denominator)
    if den <= 0:
        return 0.0
    return round((num / den) * 100.0, 1)


def _trend_pct(current: int | float, previous: int | float) -> float | None:
    prev = _safe_float(previous)
    now = _safe_float(current)
    if prev <= 0:
        return None if now <= 0 else 100.0
    return round(((now - prev) / prev) * 100.0, 1)
