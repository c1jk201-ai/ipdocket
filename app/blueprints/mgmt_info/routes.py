from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import render_template, request
from flask_login import login_required
from sqlalchemy import func, or_

from app.blueprints.billing_invoices.auth import role_required
from app.blueprints.mgmt_info import bp
from app.extensions import db
from app.models.ip_records import DocketItem, EmailMessage
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.ops.models import DurableJob
from app.services.admin.user_kpi_service import (
    build_user_kpi_dashboard,
    clamp_period,
    default_period,
    normalize_owner_basis,
    normalize_sort_key,
)
from app.services.automation.automation_monitoring import (
    check_automation_drift,
    collect_automation_metrics,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.workflow_roles import workflow_assignee_columns


def _effective_due_expr():
    return func.coalesce(
        func.nullif(DocketItem.extended_due_date, ""),
        func.nullif(DocketItem.due_date, ""),
    )


def _count_by_user(
    *,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[int, int]:
    completed_expr = or_(
        func.lower(func.coalesce(WorkLog.status, "")) == "completed",
        WorkLog.completed_at.isnot(None),
    )
    ts_expr = func.coalesce(WorkLog.completed_at, WorkLog.updated_at, WorkLog.created_at)
    q = (
        db.session.query(WorkLog.completed_by_id, func.count())
        .filter(WorkLog.completed_by_id.isnot(None))
        .filter(completed_expr)
    )
    if start_dt is not None:
        q = q.filter(ts_expr >= start_dt)
    if end_dt is not None:
        q = q.filter(ts_expr < end_dt)
    rows = q.group_by(WorkLog.completed_by_id).all()
    return {int(user_id): int(cnt or 0) for user_id, cnt in rows if user_id is not None}


def _workflow_completed_by_user(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[int, int]:
    completed_expr = func.lower(func.coalesce(func.trim(Workflow.status), "")) == "completed"
    filters = [completed_expr]
    if start_date is not None:
        filters.extend([Workflow.completed_date.isnot(None), Workflow.completed_date >= start_date])
    if end_date is not None:
        filters.extend([Workflow.completed_date.isnot(None), Workflow.completed_date < end_date])
    return _workflow_counts_by_user(filters=filters)


def _workflow_open_counts(*, today: date) -> tuple[dict[int, int], dict[int, int]]:
    open_expr = func.lower(func.coalesce(func.trim(Workflow.status), "")) != "completed"
    open_by_user = _workflow_counts_by_user(filters=[open_expr])
    overdue_by_user = _workflow_counts_by_user(
        filters=[open_expr, Workflow.due_date.isnot(None), Workflow.due_date < today]
    )
    return open_by_user, overdue_by_user


def _workflow_counts_by_user(*, filters: list | tuple) -> dict[int, int]:
    """Count workflows per user across all assignment roles without double-counting same workflow."""
    cols = workflow_assignee_columns()
    q = db.session.query(Workflow.id, *cols)
    for cond in filters or ():
        q = q.filter(cond)

    counts: dict[int, int] = {}
    for row in q.all():
        assigned_ids: set[int] = set()
        for raw_uid in row[1:]:
            if raw_uid is None:
                continue
            try:
                uid = int(raw_uid)
            except Exception:
                continue
            if uid > 0:
                assigned_ids.add(uid)
        for uid in assigned_ids:
            counts[uid] = counts.get(uid, 0) + 1
    return counts


def _docket_counts_by_owner(*, today: date) -> tuple[dict[str, int], dict[str, int], int]:
    effective_due = _effective_due_expr()
    open_filter = func.trim(func.coalesce(DocketItem.done_date, "")) == ""
    q_by_owner = (
        db.session.query(DocketItem.owner_staff_party_id, func.count())
        .filter(DocketItem.owner_staff_party_id.isnot(None))
        .filter(open_filter)
    )
    q_total = db.session.query(func.count()).select_from(DocketItem).filter(open_filter)
    if hasattr(DocketItem, "is_deleted"):
        active_filter = or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
        q_by_owner = q_by_owner.filter(active_filter)
        q_total = q_total.filter(active_filter)

    open_rows = q_by_owner.group_by(DocketItem.owner_staff_party_id).all()
    overdue_rows = (
        q_by_owner.filter(effective_due.isnot(None), effective_due < today.isoformat())
        .group_by(DocketItem.owner_staff_party_id)
        .all()
    )
    due_next_7 = (
        q_total.filter(
            effective_due.isnot(None),
            effective_due >= today.isoformat(),
            effective_due <= (today + timedelta(days=7)).isoformat(),
        ).scalar()
        or 0
    )

    open_by_owner = {
        str(owner or "").strip(): int(cnt or 0)
        for owner, cnt in open_rows
        if str(owner or "").strip()
    }
    overdue_by_owner = {
        str(owner or "").strip(): int(cnt or 0)
        for owner, cnt in overdue_rows
        if str(owner or "").strip()
    }
    return open_by_owner, overdue_by_owner, int(due_next_7)


def _collect_staff_metrics(*, today: date) -> tuple[list[dict], dict]:
    users = User.query.filter(User.is_active.is_(True)).all()
    user_key_by_staff_pid: dict[str, str] = {}
    rows_by_key: dict[str, dict] = {}

    def ensure_row(*, key: str, name: str, role: str, staff_party_id: str | None) -> dict:
        if key in rows_by_key:
            return rows_by_key[key]
        row = {
            "key": key,
            "assignee": name,
            "role": role or "-",
            "staff_party_id": staff_party_id,
            "worklog_7d": 0,
            "worklog_prev_7d": 0,
            "worklog_30d": 0,
            "workflow_7d": 0,
            "workflow_prev_7d": 0,
            "workflow_30d": 0,
            "open_workflow": 0,
            "overdue_workflow": 0,
            "open_docket": 0,
            "overdue_docket": 0,
        }
        rows_by_key[key] = row
        return row

    for user in users:
        role = (user.role or "").strip().lower()
        if role == "user":
            continue
        key = f"user:{int(user.id)}"
        name = (
            (user.display_name or "").strip()
            or (user.username or "").strip()
            or (user.email or "").strip()
            or f"user-{user.id}"
        )
        staff_pid = str(user.staff_party_id or "").strip() or None
        ensure_row(key=key, name=name, role=role, staff_party_id=staff_pid)
        if staff_pid:
            user_key_by_staff_pid[staff_pid] = key

    start_7_dt = datetime.combine(today - timedelta(days=7), datetime.min.time())
    start_14_dt = datetime.combine(today - timedelta(days=14), datetime.min.time())
    start_30_dt = datetime.combine(today - timedelta(days=30), datetime.min.time())
    start_7_date = today - timedelta(days=7)
    start_14_date = today - timedelta(days=14)
    start_30_date = today - timedelta(days=30)

    worklog_7d = _count_by_user(start_dt=start_7_dt)
    worklog_prev_7d = _count_by_user(start_dt=start_14_dt, end_dt=start_7_dt)
    worklog_30d = _count_by_user(start_dt=start_30_dt)

    workflow_7d = _workflow_completed_by_user(start_date=start_7_date)
    workflow_prev_7d = _workflow_completed_by_user(start_date=start_14_date, end_date=start_7_date)
    workflow_30d = _workflow_completed_by_user(start_date=start_30_date)
    workflow_open, workflow_overdue = _workflow_open_counts(today=today)

    for user_id, count in worklog_7d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["worklog_7d"] = int(count)
    for user_id, count in worklog_prev_7d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["worklog_prev_7d"] = int(count)
    for user_id, count in worklog_30d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["worklog_30d"] = int(count)

    for user_id, count in workflow_7d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["workflow_7d"] = int(count)
    for user_id, count in workflow_prev_7d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["workflow_prev_7d"] = int(count)
    for user_id, count in workflow_30d.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["workflow_30d"] = int(count)
    for user_id, count in workflow_open.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["open_workflow"] = int(count)
    for user_id, count in workflow_overdue.items():
        row = ensure_row(
            key=f"user:{user_id}", name=f"user-{user_id}", role="-", staff_party_id=None
        )
        row["overdue_workflow"] = int(count)

    docket_open, docket_overdue, due_next_7_total = _docket_counts_by_owner(today=today)
    for owner, count in docket_open.items():
        key = user_key_by_staff_pid.get(owner) or f"staff:{owner}"
        row = ensure_row(key=key, name=f"staff:{owner[:8]}", role="external", staff_party_id=owner)
        row["open_docket"] = int(count)
    for owner, count in docket_overdue.items():
        key = user_key_by_staff_pid.get(owner) or f"staff:{owner}"
        row = ensure_row(key=key, name=f"staff:{owner[:8]}", role="external", staff_party_id=owner)
        row["overdue_docket"] = int(count)

    rows: list[dict] = []
    for row in rows_by_key.values():
        processed_7d = int(row["worklog_7d"] + row["workflow_7d"])
        processed_prev_7d = int(row["worklog_prev_7d"] + row["workflow_prev_7d"])
        processed_30d = int(row["worklog_30d"] + row["workflow_30d"])
        pending_total = int(row["open_workflow"] + row["open_docket"])
        overdue_total = int(row["overdue_workflow"] + row["overdue_docket"])
        if processed_prev_7d > 0:
            trend_pct = round(((processed_7d - processed_prev_7d) / processed_prev_7d) * 100.0, 1)
        else:
            trend_pct = None
        row["processed_7d"] = processed_7d
        row["processed_prev_7d"] = processed_prev_7d
        row["processed_30d"] = processed_30d
        row["pending_total"] = pending_total
        row["overdue_total"] = overdue_total
        row["trend_pct"] = trend_pct

        if processed_30d > 0 or pending_total > 0 or overdue_total > 0:
            rows.append(row)

    if not rows:
        rows = [
            row
            for row in rows_by_key.values()
            if str(row.get("role") or "").lower() not in {"-", "user"}
        ]

    rows.sort(
        key=lambda item: (
            -(item.get("overdue_total") or 0),
            -(item.get("pending_total") or 0),
            -(item.get("processed_30d") or 0),
            str(item.get("assignee") or ""),
        )
    )

    summary = {
        "staff_count": len(rows),
        "processed_7d_total": sum(int(row.get("processed_7d") or 0) for row in rows),
        "processed_prev_7d_total": sum(int(row.get("processed_prev_7d") or 0) for row in rows),
        "processed_30d_total": sum(int(row.get("processed_30d") or 0) for row in rows),
        "pending_total": sum(int(row.get("pending_total") or 0) for row in rows),
        "overdue_total": sum(int(row.get("overdue_total") or 0) for row in rows),
        "workflow_overdue_total": sum(int(row.get("overdue_workflow") or 0) for row in rows),
        "docket_overdue_total": sum(int(row.get("overdue_docket") or 0) for row in rows),
        "due_next_7_total": due_next_7_total,
    }
    if summary["processed_prev_7d_total"] > 0:
        summary["processed_trend_pct"] = round(
            (
                (summary["processed_7d_total"] - summary["processed_prev_7d_total"])
                / summary["processed_prev_7d_total"]
            )
            * 100.0,
            1,
        )
    else:
        summary["processed_trend_pct"] = None

    return rows, summary


def _collect_queue_metrics(*, summary: dict) -> tuple[dict, list[dict]]:
    automation_rows = (
        db.session.query(EmailMessage.processing_status, func.count())
        .group_by(EmailMessage.processing_status)
        .all()
    )
    status_counts = {
        str(status or "").upper(): int(cnt or 0)
        for status, cnt in automation_rows
        if str(status or "").strip()
    }
    automation_backlog = (
        status_counts.get("REVIEW", 0)
        + status_counts.get("READY", 0)
        + status_counts.get("EXTRACTED", 0)
        + status_counts.get("BLOCKED", 0)
    )

    durable_counts = {"queued": 0, "running": 0, "failed": 0}
    try:
        durable_rows = (
            db.session.query(DurableJob.status, func.count())
            .filter(DurableJob.status.in_(["queued", "running", "failed"]))
            .group_by(DurableJob.status)
            .all()
        )
        for status, cnt in durable_rows:
            durable_counts[str(status or "").lower()] = int(cnt or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="mgmt_info.routes._collect_queue_metrics.durable_jobs",
            log_key="mgmt_info.routes._collect_queue_metrics.durable_jobs",
            log_window_seconds=300,
        )

    queue_metrics = {
        "status_counts": status_counts,
        "automation_backlog": automation_backlog,
        "automation_review_ready": status_counts.get("REVIEW", 0) + status_counts.get("READY", 0),
        "durable_counts": durable_counts,
        "durable_queued_running": durable_counts.get("queued", 0)
        + durable_counts.get("running", 0),
        "workflow_overdue_total": int(summary.get("workflow_overdue_total") or 0),
        "docket_overdue_total": int(summary.get("docket_overdue_total") or 0),
    }

    bottlenecks = [
        {
            "label": "Foreign Auto ",
            "count": queue_metrics["automation_backlog"],
            "meta": f"REVIEW {status_counts.get('REVIEW', 0)} / READY {status_counts.get('READY', 0)} / BLOCKED {status_counts.get('BLOCKED', 0)}",
        },
        {
            "label": "Task ",
            "count": queue_metrics["workflow_overdue_total"],
            "meta": "Completed , due_date ",
        },
        {
            "label": "Deadline ",
            "count": queue_metrics["docket_overdue_total"],
            "meta": "done_date   Deadline ",
        },
        {
            "label": "Durable Job Waiting",
            "count": queue_metrics["durable_queued_running"],
            "meta": f"queued {durable_counts.get('queued', 0)} / running {durable_counts.get('running', 0)}",
        },
    ]
    bottlenecks.sort(key=lambda row: -(row.get("count") or 0))
    return queue_metrics, bottlenecks


def _humanize_drift_alert(alert: str) -> str:
    key = str(alert or "").split(":", 1)[0]
    mapping = {
        "error_rate_high": "Foreign  Error  .",
        "review_rate_high": "REVIEW   Auto Process  .",
        "missing_evidence_rate_high": "   .",
        "parsing_fail_rate_high": " Failed  .",
        "missing_match_rate_high": "Matter Matching   .",
    }
    return mapping.get(key, alert)


def _build_predictive_alerts(
    *,
    summary: dict,
    queue_metrics: dict,
    drift_alerts: list[str],
) -> list[dict]:
    alerts: list[dict] = []
    processed_7d = int(summary.get("processed_7d_total") or 0)
    processed_prev_7d = int(summary.get("processed_prev_7d_total") or 0)
    due_next_7 = int(summary.get("due_next_7_total") or 0)
    overdue_total = int(summary.get("overdue_total") or 0)
    backlog_total = int(summary.get("pending_total") or 0)
    automation_backlog = int(queue_metrics.get("automation_backlog") or 0)
    durable_backlog = int(queue_metrics.get("durable_queued_running") or 0)

    if due_next_7 >= max(10, int(processed_7d * 1.2)):
        alerts.append(
            {
                "level": "warning",
                "title": "7  Deadline  Estimated",
                "detail": f"{due_next_7} deadline(s) due in the next 7 days; {processed_7d} processed in the last 7 days.",
            }
        )

    if overdue_total >= 15 and overdue_total > max(processed_7d, 1):
        alerts.append(
            {
                "level": "critical",
                "title": "   ",
                "detail": f"{overdue_total} overdue item(s); {processed_7d} processed in the last 7 days.",
            }
        )

    if processed_prev_7d > 0 and processed_7d < int(processed_prev_7d * 0.8):
        alerts.append(
            {
                "level": "warning",
                "title": "Process  ",
                "detail": f"Processed {processed_7d - processed_prev_7d} more item(s) than the prior 7-day period.",
            }
        )

    if automation_backlog >= 20:
        alerts.append(
            {
                "level": "warning",
                "title": "Auto   ",
                "detail": f"Foreign automation backlog: {automation_backlog} item(s).",
            }
        )

    if durable_backlog >= 80:
        alerts.append(
            {
                "level": "warning",
                "title": "  ",
                "detail": f"Durable tasks queued or running: {durable_backlog} item(s).",
            }
        )

    for item in drift_alerts:
        alerts.append(
            {
                "level": "critical",
                "title": "Auto  ",
                "detail": _humanize_drift_alert(item),
            }
        )

    if not alerts:
        alerts.append(
            {
                "level": "ok",
                "title": "  None",
                "detail": f"Current backlog: {backlog_total} item(s); overdue: {overdue_total} item(s).",
            }
        )
    return alerts


def _parse_optional_date(raw_value: str | None) -> date | None:
    raw = (raw_value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@bp.route("/")
@login_required
@role_required("admin", "staff")
def index():
    today = date.today()

    staff_rows, summary = _collect_staff_metrics(today=today)
    queue_metrics, bottlenecks = _collect_queue_metrics(summary=summary)
    summary["automation_backlog"] = int(queue_metrics.get("automation_backlog") or 0)

    automation_metrics = {}
    drift_alerts: list[str] = []
    try:
        automation_metrics = collect_automation_metrics(window_days=7)
        drift_alerts = check_automation_drift(automation_metrics)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="mgmt_info.routes.index.collect_automation_metrics",
            log_key="mgmt_info.routes.index.collect_automation_metrics",
            log_window_seconds=300,
        )

    predictive_alerts = _build_predictive_alerts(
        summary=summary,
        queue_metrics=queue_metrics,
        drift_alerts=drift_alerts,
    )

    return render_template(
        "mgmt_info/index.html",
        snapshot_at=datetime.utcnow(),
        today=today,
        staff_rows=staff_rows,
        summary=summary,
        queue_metrics=queue_metrics,
        bottlenecks=bottlenecks,
        predictive_alerts=predictive_alerts,
        automation_metrics=automation_metrics,
        drift_alerts=drift_alerts,
    )


@bp.route("/kpi")
@login_required
@role_required("admin", "staff")
def user_kpi():
    today = date.today()
    requested_start = _parse_optional_date(request.args.get("start"))
    requested_end = _parse_optional_date(request.args.get("end"))

    if requested_start is None and requested_end is None:
        start, end = default_period(today=today)
    else:
        start, end = clamp_period(requested_start, requested_end, today=today)

    dashboard = build_user_kpi_dashboard(
        start=start,
        end=end,
        owner_basis=normalize_owner_basis(request.args.get("owner_basis")),
        sort_key=normalize_sort_key(request.args.get("sort")),
        today=today,
    )
    return render_template(
        "mgmt_info/user_kpi.html",
        snapshot_at=datetime.utcnow(),
        dashboard=dashboard,
    )
