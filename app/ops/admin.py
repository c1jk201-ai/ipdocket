from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import and_, case, func, or_

from app.extensions import db
from app.models.annuity_workflow_sync_dead_letter import AnnuityWorkflowSyncDeadLetter
from app.models.annuity_workflow_sync_queue import AnnuityWorkflowSyncQueue
from app.models.error_report import ErrorReport
from app.models.job_run import JobRun
from app.models.notification import NotificationLog
from app.models.ip_records import EmailIngestionLog
from app.models.user_access_log import UserAccessLog
from app.ops.durable_queue import build_queue_from_app, durable_job_retry_diagnostics
from app.ops.models import DiskSample, DurableJob
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception

try:
    from flask_login import current_user, login_required
except Exception:  # pragma: no cover

    def login_required(f):  # type: ignore[no-redef]
        return f

    current_user = None


ops_admin_bp = Blueprint("ops_admin", __name__, url_prefix="/admin/ops")


def _require_admin() -> None:
    if current_user is None:
        return
    # Our User model uses `role` ("admin") rather than an `is_admin` attribute.
    role = (getattr(current_user, "role", "") or "").strip().lower()
    if role != "admin":
        abort(403)


def _parse_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _age_minutes(dt: datetime | None, *, now: datetime) -> int | None:
    if dt is None:
        return None
    try:
        return max(0, int((now - dt).total_seconds() // 60))
    except Exception:
        return None


def _percentile_from_sorted(values: list[int], p: float) -> float | None:
    if not values:
        return None
    if p <= 0:
        return float(values[0])
    if p >= 1:
        return float(values[-1])
    idx = int(round((len(values) - 1) * p))
    idx = max(0, min(idx, len(values) - 1))
    return float(values[idx])


def _safe_count(builder, *, context: str, log_key: str) -> int:
    try:
        return int(builder() or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=log_key,
            log_window_seconds=300,
        )
        return 0


def _safe_rows(builder, *, context: str, log_key: str) -> list[Any]:
    try:
        rows = builder()
        return list(rows or [])
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=log_key,
            log_window_seconds=300,
        )
        return []


def _first_aggregate_row(rows: list[Any], default: tuple[Any, ...]) -> tuple[Any, ...]:
    if not rows:
        return default

    if len(rows) == len(default):
        first = rows[0]
        if not isinstance(first, (tuple, list)) and not hasattr(first, "_mapping"):
            return tuple(rows)

    try:
        row = tuple(rows[0])
    except TypeError:
        return default

    if len(row) != len(default):
        return default
    return row


def _last_job_run(job_name: str, status: str | None = None) -> JobRun | None:
    q = JobRun.query.filter(JobRun.job_name == job_name)
    if status:
        q = q.filter(JobRun.status == status)
    return q.order_by(JobRun.started_at.desc()).first()


def _queue_snapshot() -> dict[str, Any]:
    status_expr = func.lower(func.trim(func.coalesce(DurableJob.status, "")))
    queued_count_expr = func.sum(case((status_expr == "queued", 1), else_=0)).label("queued_count")
    running_count_expr = func.sum(case((status_expr == "running", 1), else_=0)).label(
        "running_count"
    )
    failed_count_expr = func.sum(case((status_expr == "failed", 1), else_=0)).label("failed_count")
    open_count_expr = func.sum(case((status_expr.in_(["queued", "running"]), 1), else_=0)).label(
        "open_count"
    )

    status_rows = _safe_rows(
        lambda: db.session.query(status_expr.label("status"), func.count(DurableJob.id))
        .group_by(status_expr)
        .all(),
        context="ops_admin.queue_snapshot.status_rows",
        log_key="ops_admin.queue_snapshot.status_rows",
    )
    status_counts = {
        str(status or "unknown"): int(count or 0)
        for status, count in status_rows
        if status or count
    }

    queue_rows = _safe_rows(
        lambda: db.session.query(
            DurableJob.queue,
            queued_count_expr,
            running_count_expr,
            failed_count_expr,
            open_count_expr,
        )
        .group_by(DurableJob.queue)
        .order_by(open_count_expr.desc(), failed_count_expr.desc(), DurableJob.queue.asc())
        .limit(8)
        .all(),
        context="ops_admin.queue_snapshot.queue_rows",
        log_key="ops_admin.queue_snapshot.queue_rows",
    )
    top_queues = [
        {
            "queue": str(queue or "-"),
            "queued": int(queued or 0),
            "running": int(running or 0),
            "failed": int(failed or 0),
            "open": int(open_count or 0),
        }
        for queue, queued, running, failed, open_count in queue_rows
    ]

    recent_failed_jobs = _safe_rows(
        lambda: DurableJob.query.filter(status_expr == "failed")
        .order_by(DurableJob.updated_at.desc())
        .limit(8)
        .all(),
        context="ops_admin.queue_snapshot.recent_failed_jobs",
        log_key="ops_admin.queue_snapshot.recent_failed_jobs",
    )
    oldest_open_job = (
        _safe_rows(
            lambda: DurableJob.query.filter(status_expr.in_(["queued", "running"]))
            .order_by(DurableJob.run_at.asc(), DurableJob.created_at.asc())
            .limit(1)
            .all(),
            context="ops_admin.queue_snapshot.oldest_open_job",
            log_key="ops_admin.queue_snapshot.oldest_open_job",
        )
        or [None]
    )[0]

    return {
        "status_counts": status_counts,
        "top_queues": top_queues,
        "recent_failed_jobs": recent_failed_jobs,
        "oldest_open_job": oldest_open_job,
        "failed_count": int(status_counts.get("failed", 0)),
        "queued_count": int(status_counts.get("queued", 0)),
        "running_count": int(status_counts.get("running", 0)),
        "open_count": int(status_counts.get("queued", 0)) + int(status_counts.get("running", 0)),
    }


def _retry_due_label(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds <= 0:
        return " "
    minutes = max(1, (int(seconds) + 59) // 60)
    if minutes < 60:
        return f"{minutes} "
    hours = minutes // 60
    remainder = minutes % 60
    if remainder:
        return f"{hours} {remainder} "
    return f"{hours} "


def _durable_job_display_row(job: DurableJob, *, now: datetime) -> dict[str, Any]:
    diagnostics = durable_job_retry_diagnostics(job, now=now)
    retry_due_seconds = diagnostics.get("retry_due_in_seconds")
    try:
        retry_due_seconds_int = int(retry_due_seconds) if retry_due_seconds is not None else None
    except Exception:
        retry_due_seconds_int = None

    return {
        "id": job.id,
        "queue": job.queue,
        "task": job.task,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_at": job.run_at,
        "updated_at": job.updated_at,
        "last_error": job.last_error,
        "retry_state": diagnostics.get("retry_state") or "",
        "retry_state_label": diagnostics.get("retry_state_label") or "",
        "retry_cause": diagnostics.get("retry_cause") or "",
        "next_retry_at": diagnostics.get("next_retry_at"),
        "retry_due_label": _retry_due_label(retry_due_seconds_int),
        "retries_remaining": diagnostics.get("retries_remaining"),
    }


def _queue_dashboard_row(
    *,
    name: str,
    kind: str,
    queued: int,
    running: int,
    failed: int,
    retry: int,
    oldest_at: datetime | None,
    now: datetime,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "queued": int(queued or 0),
        "running": int(running or 0),
        "failed": int(failed or 0),
        "retry": int(retry or 0),
        "oldest_at": oldest_at,
        "oldest_age_minutes": _age_minutes(oldest_at, now=now),
        "detail": detail,
    }


def _durable_queue_dashboard_rows(*, now: datetime) -> list[dict[str, Any]]:
    status_expr = func.lower(func.trim(func.coalesce(DurableJob.status, "")))
    oldest_open_expr = func.min(
        case(
            (
                status_expr.in_(["queued", "running"]),
                func.coalesce(DurableJob.run_at, DurableJob.created_at),
            ),
            else_=None,
        )
    ).label("oldest_open_at")

    rows = _safe_rows(
        lambda: db.session.query(
            DurableJob.queue,
            func.sum(case((status_expr == "queued", 1), else_=0)),
            func.sum(case((status_expr == "running", 1), else_=0)),
            func.sum(case((status_expr == "failed", 1), else_=0)),
            func.sum(
                case(
                    (
                        and_(
                            status_expr == "queued",
                            DurableJob.attempts > 0,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            oldest_open_expr,
        )
        .group_by(DurableJob.queue)
        .order_by(DurableJob.queue.asc())
        .all(),
        context="ops_admin.queue_dashboard.durable",
        log_key="ops_admin.queue_dashboard.durable",
    )
    return [
        _queue_dashboard_row(
            name=str(queue or "default"),
            kind="durable_jobs",
            queued=queued,
            running=running,
            failed=failed,
            retry=retry,
            oldest_at=oldest_at,
            now=now,
        )
        for queue, queued, running, failed, retry, oldest_at in rows
    ]


def _annuity_queue_dashboard_row(*, now: datetime) -> dict[str, Any]:
    lock_timeout = ConfigService.get_int("ANNUITY_SYNC_QUEUE_LOCK_TIMEOUT_SECONDS", 300) or 300
    expired_before = now - timedelta(seconds=max(1, int(lock_timeout)))
    ready_expr = and_(
        or_(
            AnnuityWorkflowSyncQueue.locked_at.is_(None),
            AnnuityWorkflowSyncQueue.locked_at < expired_before,
        ),
        or_(
            AnnuityWorkflowSyncQueue.next_run_at.is_(None),
            AnnuityWorkflowSyncQueue.next_run_at <= now,
        ),
    )
    running_expr = and_(
        AnnuityWorkflowSyncQueue.locked_at.isnot(None),
        AnnuityWorkflowSyncQueue.locked_at >= expired_before,
    )
    retry_expr = or_(
        AnnuityWorkflowSyncQueue.last_error.isnot(None),
        AnnuityWorkflowSyncQueue.next_run_at > now,
    )
    row = _first_aggregate_row(
        _safe_rows(
            lambda: db.session.query(
                func.sum(case((ready_expr, 1), else_=0)),
                func.sum(case((running_expr, 1), else_=0)),
                func.sum(case((retry_expr, 1), else_=0)),
                func.min(
                    func.coalesce(
                        AnnuityWorkflowSyncQueue.next_run_at,
                        AnnuityWorkflowSyncQueue.created_at,
                    )
                ),
            ).all(),
            context="ops_admin.queue_dashboard.annuity",
            log_key="ops_admin.queue_dashboard.annuity",
        ),
        (0, 0, 0, None),
    )
    dead_letters = _safe_count(
        lambda: db.session.query(func.count(AnnuityWorkflowSyncDeadLetter.id)).scalar(),
        context="ops_admin.queue_dashboard.annuity.dead_letter",
        log_key="ops_admin.queue_dashboard.annuity.dead_letter",
    )
    queued, running, retry, oldest_at = row
    return _queue_dashboard_row(
        name="annuity_workflow_sync_queue",
        kind="legacy_lock_queue",
        queued=queued,
        running=running,
        failed=dead_letters,
        retry=retry,
        oldest_at=oldest_at,
        now=now,
        detail="drained by annuity.workflow_sync durable adapter",
    )


def _queue_dashboard_rows() -> list[dict[str, Any]]:
    now = datetime.utcnow()
    rows: list[dict[str, Any]] = []
    rows.extend(_durable_queue_dashboard_rows(now=now))
    rows.append(_annuity_queue_dashboard_row(now=now))
    return sorted(
        rows,
        key=lambda item: (
            -(
                _as_int(item.get("queued"))
                + _as_int(item.get("running"))
                + _as_int(item.get("failed"))
                + _as_int(item.get("retry"))
            ),
            str(item.get("name") or ""),
        ),
    )


def _scheduler_snapshot() -> dict[str, Any]:
    from flask import current_app

    now = datetime.utcnow()
    try:
        hb_seconds = int(current_app.config.get("SCHEDULER_HEARTBEAT_INTERVAL_SECONDS", 300) or 300)
    except Exception:
        hb_seconds = 300
    hb_seconds = max(60, hb_seconds)
    stale_threshold = max(hb_seconds * 2, 600)

    heartbeat_success = _last_job_run("scheduler_heartbeat", "success")
    heartbeat_time = getattr(heartbeat_success, "finished_at", None) or getattr(
        heartbeat_success, "started_at", None
    )
    alive = bool(heartbeat_time and (now - heartbeat_time) <= timedelta(seconds=stale_threshold))

    jobs = [
        ("daily_annuity_generation", "Daily Annuity Auto-Generation"),
        ("annuity_sync_queue_drain", "Annuity Sync Queue Drain"),
        ("daily_housekeeping", "Daily Housekeeping"),
        ("disk_monitor", "Disk Monitor"),
        ("matter_status_recalc_queue_drain", "Matter Status Recalc Queue Drain"),
        ("matter_status_cache_audit", "Matter Status Cache Audit"),
        ("matter_status_cache_reconcile", "Matter Status Cache Reconcile"),
        ("scheduler_heartbeat", "Scheduler Heartbeat"),
    ]

    items = []
    for job_name, label in jobs:
        last = _last_job_run(job_name)
        last_success = _last_job_run(job_name, "success")
        last_failed = _last_job_run(job_name, "failed")
        items.append(
            {
                "job_name": job_name,
                "label": label,
                "last": last,
                "last_success": last_success,
                "last_failed": last_failed,
            }
        )

    recent_failures = sorted(
        [item for item in items if item.get("last_failed")],
        key=lambda item: getattr(item["last_failed"], "finished_at", None)
        or getattr(item["last_failed"], "started_at", None)
        or datetime.min,
        reverse=True,
    )

    return {
        "alive": alive,
        "heartbeat_time": heartbeat_time,
        "stale_threshold": stale_threshold,
        "now": now,
        "items": items,
        "recent_failures": recent_failures[:3],
    }


def _disk_snapshot(*, labels: tuple[str, ...] = ("uploads", "backups")) -> dict[str, Any]:
    now = datetime.utcnow()
    since = now - timedelta(days=7)
    rows = _safe_rows(
        lambda: DiskSample.query.filter(
            DiskSample.mount_label.in_(labels), DiskSample.sampled_at >= since
        )
        .order_by(DiskSample.mount_label.asc(), DiskSample.sampled_at.asc())
        .all(),
        context="ops_admin.disk_snapshot.rows",
        log_key="ops_admin.disk_snapshot.rows",
    )

    grouped: dict[str, list[DiskSample]] = {label: [] for label in labels}
    for row in rows:
        grouped.setdefault(str(row.mount_label or ""), []).append(row)

    summaries: list[dict[str, Any]] = []
    for label in labels:
        points = grouped.get(label, [])
        latest = points[-1] if points else None
        earliest = points[0] if points else None
        used_delta = None
        if latest is not None and earliest is not None:
            used_delta = round(float(latest.used_pct or 0) - float(earliest.used_pct or 0), 2)
        summaries.append(
            {
                "label": label,
                "latest": latest,
                "points": points,
                "point_count": len(points),
                "used_delta_pct": used_delta,
            }
        )

    return {
        "generated_at": now,
        "labels": summaries,
        "has_points": any(int(summary.get("point_count") or 0) > 0 for summary in summaries),
        "critical_labels": [
            summary
            for summary in summaries
            if isinstance(summary.get("latest"), DiskSample)
            and float(cast(DiskSample, summary["latest"]).used_pct or 0) >= 85.0
        ],
    }


def _normalize_retention_stats(job_name: str, stats: dict[str, Any]) -> dict[str, Any]:
    if job_name == "daily_housekeeping":
        inbox = cast(dict[str, Any], stats.get("inbox_ignored_retention") or {})
        file_gc = cast(dict[str, Any], stats.get("file_asset_gc") or {})
        delete_queue = cast(dict[str, Any], stats.get("file_delete_queue") or {})
        staging_gc = cast(dict[str, Any], stats.get("file_asset_staging_gc") or {})
        pdf_gc = cast(dict[str, Any], stats.get("pdf_text_cache_gc") or {})
        durable_jobs = cast(dict[str, Any], stats.get("durable_jobs_deleted") or {})
        backup_cleanup_ok = bool(stats.get("backup_cleanup_ran", False))

        sections = [
            {
                "label": "Upload Sessions",
                "removed": _as_int(stats.get("upload_sessions_deleted")),
                "errors": 0,
            },
            {
                "label": "Job Runs",
                "removed": _as_int(stats.get("job_runs_deleted"))
                + _as_int(stats.get("job_runs_success_deleted")),
                "errors": 0,
            },
            {
                "label": "Durable Jobs",
                "removed": _as_int(durable_jobs.get("finished_deleted"))
                + _as_int(durable_jobs.get("failed_deleted")),
                "errors": 0,
            },
            {
                "label": "Access/Disk Samples",
                "removed": _as_int(stats.get("user_access_logs_deleted"))
                + _as_int(stats.get("disk_samples_deleted")),
                "errors": 0,
            },
            {
                "label": "Ignored Inbox",
                "removed": _as_int(inbox.get("emails_deleted"))
                + _as_int(inbox.get("file_assets_deleted")),
                "errors": 0,
            },
            {
                "label": "File Asset GC",
                "removed": _as_int(file_gc.get("deleted")),
                "errors": 0,
            },
            {
                "label": "Staging GC",
                "removed": _as_int(staging_gc.get("deleted")),
                "errors": 0,
            },
            {
                "label": "PDF Text Cache",
                "removed": _as_int(pdf_gc.get("deleted")),
                "errors": 0,
            },
            {
                "label": "File Delete Queue",
                "removed": _as_int(delete_queue.get("deleted")),
                "errors": _as_int(delete_queue.get("failed")),
            },
            {
                "label": "Backup Cleanup",
                "removed": 0,
                "errors": 0 if backup_cleanup_ok else 1,
            },
        ]

        sample_texts: list[str] = []
        if _as_int(delete_queue.get("failed")) > 0:
            sample_texts.append(f"file_delete_queue failed={_as_int(delete_queue.get('failed'))}")
        if not backup_cleanup_ok:
            sample_texts.append("backup_cleanup_ran=false")

        normalized = dict(stats)
        normalized["display_sections"] = sections
        normalized["sample_texts"] = sample_texts
        normalized["total_removed"] = sum(_as_int(section.get("removed")) for section in sections)
        normalized["total_errors"] = sum(_as_int(section.get("errors")) for section in sections)
        normalized["source_job_name"] = job_name
        return normalized

    uploads = cast(dict[str, Any], stats.get("uploads") or {})
    backups = cast(dict[str, Any], stats.get("backups") or {})
    sample_texts = []
    for sample in list(uploads.get("error_samples") or []) + list(
        backups.get("error_samples") or []
    ):
        if sample:
            sample_texts.append(str(sample))

    normalized = dict(stats)
    normalized.setdefault(
        "display_sections",
        [
            {
                "label": "Uploads",
                "removed": _as_int(uploads.get("removed")),
                "errors": _as_int(uploads.get("errors")),
            },
            {
                "label": "Backups",
                "removed": _as_int(backups.get("removed")),
                "errors": _as_int(backups.get("errors")),
            },
        ],
    )
    normalized.setdefault(
        "total_removed",
        _as_int(uploads.get("removed")) + _as_int(backups.get("removed")),
    )
    normalized.setdefault(
        "total_errors",
        _as_int(uploads.get("errors")) + _as_int(backups.get("errors")),
    )
    normalized["sample_texts"] = sample_texts
    normalized["source_job_name"] = job_name
    return normalized


def _retention_snapshot() -> dict[str, Any]:
    rows = _safe_rows(
        lambda: JobRun.query.filter(
            JobRun.job_name.in_(["daily_housekeeping", "retention.cleanup"])
        )
        .order_by(JobRun.started_at.desc())
        .limit(30)
        .all(),
        context="ops_admin.retention_snapshot.rows",
        log_key="ops_admin.retention_snapshot.rows",
    )
    items = [
        {
            "run": row,
            "stats": _normalize_retention_stats(
                str(getattr(row, "job_name", "") or ""), _parse_json(row.output_ref)
            ),
        }
        for row in rows
    ]
    last_item = items[0] if items else None
    problem_runs = [
        item
        for item in items
        if str(getattr(item["run"], "status", "") or "").lower() != "success"
        or int((item.get("stats") or {}).get("total_errors") or 0) > 0
    ]
    return {
        "items": items,
        "last_item": last_item,
        "problem_runs": problem_runs[:5],
        "table_growth": _table_growth_snapshot(),
    }


def _table_growth_snapshot() -> dict[str, Any]:
    try:
        from app.services.ops.operational_metrics import collect_high_volume_table_metrics

        return collect_high_volume_table_metrics()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="ops_admin.table_growth_snapshot",
            log_key="ops_admin.table_growth_snapshot",
            log_window_seconds=300,
        )
        return {"tables": [], "alerts": []}


def _annuity_snapshot() -> dict[str, Any]:
    backlog = _safe_count(
        lambda: db.session.query(func.count(AnnuityWorkflowSyncQueue.matter_id)).scalar(),
        context="ops_admin.annuity_snapshot.backlog",
        log_key="ops_admin.annuity_snapshot.backlog",
    )
    queued_items = _safe_rows(
        lambda: AnnuityWorkflowSyncQueue.query.order_by(
            case((AnnuityWorkflowSyncQueue.next_run_at.is_(None), 1), else_=0).asc(),
            AnnuityWorkflowSyncQueue.next_run_at.asc(),
            AnnuityWorkflowSyncQueue.updated_at.asc(),
        )
        .limit(5)
        .all(),
        context="ops_admin.annuity_snapshot.queued_items",
        log_key="ops_admin.annuity_snapshot.queued_items",
    )
    return {
        "backlog": backlog,
        "queued_items": queued_items,
        "last_generation": _last_job_run("daily_annuity_generation"),
        "last_sync_drain": _last_job_run("annuity_sync_queue_drain"),
    }


def _queue_health_snapshot(*, now: datetime) -> dict[str, Any]:
    backlog_warn_minutes = ConfigService.get_int("ADMIN_QUEUE_BACKLOG_WARN_MINUTES", 60)
    if backlog_warn_minutes is None:
        backlog_warn_minutes = 60
    try:
        durable_lock_ttl = int(current_app.config.get("DURABLE_QUEUE_LOCK_TTL_SECONDS", 600) or 600)
    except Exception:
        durable_lock_ttl = 600
    durable_status = func.lower(func.trim(func.coalesce(DurableJob.status, "")))

    durable_oldest = (
        _safe_rows(
            lambda: db.session.query(DurableJob)
            .filter(durable_status.in_(["queued", "running"]))
            .order_by(
                func.coalesce(DurableJob.run_at, DurableJob.created_at).asc(),
                DurableJob.created_at.asc(),
            )
            .limit(1)
            .all(),
            context="ops_admin.service_health.queue_health.durable_oldest",
            log_key="ops_admin.service_health.queue_health.durable_oldest",
        )
        or [None]
    )[0]

    durable_failed = _safe_count(
        lambda: db.session.query(func.count(DurableJob.id))
        .filter(durable_status == "failed")
        .scalar(),
        context="ops_admin.service_health.queue_health.durable_failed",
        log_key="ops_admin.service_health.queue_health.durable_failed",
    )
    durable_stale_running = _safe_count(
        lambda: db.session.query(func.count(DurableJob.id))
        .filter(durable_status == "running")
        .filter(DurableJob.locked_at.isnot(None))
        .filter(DurableJob.locked_at < now - timedelta(seconds=max(1, int(durable_lock_ttl))))
        .scalar(),
        context="ops_admin.service_health.queue_health.durable_stale",
        log_key="ops_admin.service_health.queue_health.durable_stale",
    )

    durable_oldest_at = None
    if durable_oldest is not None:
        durable_oldest_at = getattr(durable_oldest, "run_at", None) or getattr(
            durable_oldest, "created_at", None
        )

    durable_age = _age_minutes(durable_oldest_at, now=now)
    warn_minutes = max(1, int(backlog_warn_minutes))

    return {
        "thresholds": {
            "backlog_warn_minutes": warn_minutes,
            "durable_lock_ttl_seconds": max(1, int(durable_lock_ttl)),
        },
        "durable_jobs": {
            "failed": durable_failed,
            "stale_running": durable_stale_running,
            "oldest_age_minutes": durable_age,
            "oldest_id": getattr(durable_oldest, "id", None) if durable_oldest else None,
        },
    }


def _queue_alerts(
    *,
    queue_backlog: dict[str, int],
    queue_health: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    warn_minutes = _as_int((queue_health.get("thresholds") or {}).get("backlog_warn_minutes")) or 60

    durable = cast(dict[str, Any], queue_health.get("durable_jobs") or {})

    if _as_int(durable.get("failed")) > 0:
        alerts.append(
            {
                "level": "warning",
                "code": "durable_jobs_failed",
                "message": f"durable_jobs failed rows={_as_int(durable.get('failed'))}",
            }
        )
    if _as_int(durable.get("stale_running")) > 0:
        alerts.append(
            {
                "level": "danger",
                "code": "durable_jobs_stale_running",
                "message": f"durable_jobs stale running rows={_as_int(durable.get('stale_running'))}",
            }
        )
    if (durable.get("oldest_age_minutes") or 0) >= warn_minutes:
        alerts.append(
            {
                "level": "warning",
                "code": "durable_jobs_backlog_age",
                "message": (
                    "durable_jobs oldest backlog age="
                    f"{_as_int(durable.get('oldest_age_minutes'))}m"
                ),
            }
        )

    if _as_int(queue_backlog.get("annuity_workflow_sync_queue")) > 0:
        alerts.append(
            {
                "level": "info",
                "code": "annuity_workflow_sync_queue_backlog",
                "message": (
                    "annuity_workflow_sync_queue backlog="
                    f"{_as_int(queue_backlog.get('annuity_workflow_sync_queue'))}"
                ),
            }
        )
    return alerts


def _service_health_snapshot() -> dict[str, Any]:
    now = datetime.utcnow()
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)

    p50_ms = None
    p95_ms = None
    p99_ms = None
    latency_sample_count = 0

    try:
        row = (
            db.session.query(
                func.percentile_cont(0.5).within_group(UserAccessLog.duration_ms),
                func.percentile_cont(0.95).within_group(UserAccessLog.duration_ms),
                func.percentile_cont(0.99).within_group(UserAccessLog.duration_ms),
                func.count(UserAccessLog.id),
            )
            .filter(UserAccessLog.created_at >= since_24h)
            .filter(UserAccessLog.duration_ms.isnot(None))
            .one()
        )
        p50_ms = float(row[0]) if row[0] is not None else None
        p95_ms = float(row[1]) if row[1] is not None else None
        p99_ms = float(row[2]) if row[2] is not None else None
        latency_sample_count = int(row[3] or 0)
    except Exception as exc:
        # Fallback for DBs without percentile_cont (e.g., SQLite tests).
        report_swallowed_exception(
            exc,
            context="ops_admin.service_health.latency.percentile_sql",
            log_key="ops_admin.service_health.latency.percentile_sql",
            log_window_seconds=300,
        )
        try:
            raw = (
                db.session.query(UserAccessLog.duration_ms)
                .filter(UserAccessLog.created_at >= since_24h)
                .filter(UserAccessLog.duration_ms.isnot(None))
                .all()
            )
            durations = sorted(int(r[0]) for r in raw if r and r[0] is not None)
            latency_sample_count = len(durations)
            p50_ms = _percentile_from_sorted(durations, 0.50)
            p95_ms = _percentile_from_sorted(durations, 0.95)
            p99_ms = _percentile_from_sorted(durations, 0.99)
        except Exception as fallback_exc:
            report_swallowed_exception(
                fallback_exc,
                context="ops_admin.service_health.latency.percentile_fallback",
                log_key="ops_admin.service_health.latency.percentile_fallback",
                log_window_seconds=300,
            )

    error_reports_24h = _safe_count(
        lambda: db.session.query(func.count(ErrorReport.id))
        .filter(ErrorReport.created_at >= since_24h)
        .scalar(),
        context="ops_admin.service_health.error_reports_24h",
        log_key="ops_admin.service_health.error_reports_24h",
    )

    email_ingestion_total_7d = _safe_count(
        lambda: db.session.query(func.count(EmailIngestionLog.id))
        .filter(EmailIngestionLog.created_at >= since_7d)
        .scalar(),
        context="ops_admin.service_health.email_ingestion.total_7d",
        log_key="ops_admin.service_health.email_ingestion.total_7d",
    )
    email_ingestion_failed_7d = _safe_count(
        lambda: db.session.query(func.count(EmailIngestionLog.id))
        .filter(EmailIngestionLog.created_at >= since_7d)
        .filter(EmailIngestionLog.error_code.isnot(None))
        .filter(func.trim(EmailIngestionLog.error_code) != "")
        .filter(func.lower(func.trim(EmailIngestionLog.error_code)) != "lock_timeout")
        .scalar(),
        context="ops_admin.service_health.email_ingestion.failed_7d",
        log_key="ops_admin.service_health.email_ingestion.failed_7d",
    )
    email_ingestion_lock_timeout_7d = _safe_count(
        lambda: db.session.query(func.count(EmailIngestionLog.id))
        .filter(EmailIngestionLog.created_at >= since_7d)
        .filter(
            func.lower(func.trim(func.coalesce(EmailIngestionLog.error_code, ""))) == "lock_timeout"
        )
        .scalar(),
        context="ops_admin.service_health.email_ingestion.lock_timeout_7d",
        log_key="ops_admin.service_health.email_ingestion.lock_timeout_7d",
    )
    email_ingestion_failure_rate_7d = (
        (float(email_ingestion_failed_7d) / float(email_ingestion_total_7d) * 100.0)
        if email_ingestion_total_7d > 0
        else 0.0
    )

    notification_failed_24h = _safe_count(
        lambda: db.session.query(func.count(NotificationLog.id))
        .filter(NotificationLog.sent_at >= since_24h)
        .filter(func.lower(func.trim(func.coalesce(NotificationLog.status, ""))) == "failed")
        .scalar(),
        context="ops_admin.service_health.notification_failed_24h",
        log_key="ops_admin.service_health.notification_failed_24h",
    )

    durable_jobs_backlog = _safe_count(
        lambda: db.session.query(func.count(DurableJob.id))
        .filter(
            func.lower(func.trim(func.coalesce(DurableJob.status, ""))).in_(["queued", "running"])
        )
        .scalar(),
        context="ops_admin.service_health.queue_backlog.durable_jobs",
        log_key="ops_admin.service_health.queue_backlog.durable_jobs",
    )
    annuity_workflow_sync_queue_backlog = _safe_count(
        lambda: db.session.query(func.count(AnnuityWorkflowSyncQueue.matter_id)).scalar(),
        context="ops_admin.service_health.queue_backlog.annuity_workflow_sync_queue",
        log_key="ops_admin.service_health.queue_backlog.annuity_workflow_sync_queue",
    )
    queue_backlog = {
        "durable_jobs": durable_jobs_backlog,
        "annuity_workflow_sync_queue": annuity_workflow_sync_queue_backlog,
    }
    queue_health = _queue_health_snapshot(now=now)
    operational_metrics: dict[str, Any] = {}
    try:
        from app.services.ops.operational_metrics import collect_operational_metrics

        operational_metrics = collect_operational_metrics()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="ops_admin.service_health.operational_metrics",
            log_key="ops_admin.service_health.operational_metrics",
            log_window_seconds=300,
        )
    alerts = _queue_alerts(queue_backlog=queue_backlog, queue_health=queue_health)
    table_growth = cast(dict[str, Any], operational_metrics.get("table_growth") or {})
    alerts.extend(list(table_growth.get("alerts") or []))

    return {
        "generated_at": now.isoformat() + "Z",
        "request_latency_24h": {
            "sample_count": latency_sample_count,
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
            "p99_ms": p99_ms,
        },
        "error_reports_24h": error_reports_24h,
        "email_ingestion_7d": {
            "total_runs": email_ingestion_total_7d,
            "failed_runs": email_ingestion_failed_7d,
            "lock_timeout_runs": email_ingestion_lock_timeout_7d,
            "failure_rate_pct": round(email_ingestion_failure_rate_7d, 2),
        },
        "notification_failures_24h": notification_failed_24h,
        "queue_backlog": queue_backlog,
        "queue_health": queue_health,
        "operational_metrics": operational_metrics,
        "alerts": alerts,
    }


@ops_admin_bp.route("/", methods=["GET"], strict_slashes=False)
@login_required
def ops_overview():
    _require_admin()
    return render_template(
        "admin/ops_overview.html",
        active_page="ops_overview",
        health=_service_health_snapshot(),
        queue_snapshot=_queue_snapshot(),
        scheduler_snapshot=_scheduler_snapshot(),
        disk_snapshot=_disk_snapshot(),
        retention_snapshot=_retention_snapshot(),
        annuity_snapshot=_annuity_snapshot(),
    )


@ops_admin_bp.get("/queue")
@login_required
def queue_home():
    _require_admin()

    status = request.args.get("status", "failed")
    queue = request.args.get("queue", "")

    q = DurableJob.query
    if status:
        q = q.filter(DurableJob.status == status)
    if queue:
        q = q.filter(DurableJob.queue == queue)

    now = datetime.utcnow()
    jobs = q.order_by(DurableJob.updated_at.desc()).limit(200).all()
    return render_template(
        "admin/ops_queue.html",
        jobs=[_durable_job_display_row(job, now=now) for job in jobs],
        queue_summary=_queue_snapshot(),
        queue_dashboard_rows=_queue_dashboard_rows(),
        status=status,
        queue=queue,
        active_page="ops_queue",
    )


@ops_admin_bp.post("/queue/<int:job_id>/retry")
@login_required
def queue_retry(job_id: int):
    _require_admin()
    from flask import current_app

    dq = build_queue_from_app(current_app)
    ok = dq.retry(job_id)
    return redirect(url_for("ops_admin.queue_home", status=request.args.get("status", "failed")))


@ops_admin_bp.get("/disk")
@login_required
def disk_home():
    _require_admin()
    from app.ops.retention import build_retention_preview

    return render_template(
        "admin/ops_disk.html",
        active_page="ops_disk",
        disk_snapshot=_disk_snapshot(),
        retention_preview=build_retention_preview(current_app),
    )


@ops_admin_bp.get("/disk/data")
@login_required
def disk_data():
    _require_admin()
    now = datetime.utcnow()
    since = now - timedelta(days=7)

    label = request.args.get("label", "uploads")
    rows = (
        DiskSample.query.filter(DiskSample.mount_label == label, DiskSample.sampled_at >= since)
        .order_by(DiskSample.sampled_at.asc())
        .all()
    )

    return jsonify(
        {
            "label": label,
            "points": [
                {
                    "t": r.sampled_at.isoformat() + "Z",
                    "used_pct": r.used_pct,
                    "used_bytes": r.used_bytes,
                    "free_bytes": r.free_bytes,
                    "total_bytes": r.total_bytes,
                }
                for r in rows
            ],
        }
    )


@ops_admin_bp.get("/scheduler")
@login_required
def scheduler_home():
    _require_admin()
    snapshot = _scheduler_snapshot()
    return render_template("admin/ops_scheduler.html", active_page="ops_scheduler", **snapshot)


@ops_admin_bp.get("/retention")
@login_required
def retention_home():
    _require_admin()
    snapshot = _retention_snapshot()
    return render_template("admin/ops_retention.html", active_page="ops_retention", **snapshot)


@ops_admin_bp.get("/annuity")
@login_required
def annuity_home():
    _require_admin()

    q = (request.args.get("q") or "").strip()
    refresh = (request.args.get("refresh") or "").strip() in ("1", "true", "yes", "on")

    matches = []
    selected = None
    diag = None

    if q:
        try:
            from app.models.matter import Matter

            selected = Matter.query.get(q)
            if not selected:
                matches = (
                    Matter.query.filter(Matter.our_ref.ilike(f"%{q}%"))
                    .order_by(Matter.our_ref.asc())
                    .limit(20)
                    .all()
                )
                if len(matches) == 1:
                    selected = matches[0]
        except Exception:
            selected = None
            matches = []

    if selected:
        from app.services.annuity.annuity_service import diagnose_annuity_autogen_for_matter

        diag = diagnose_annuity_autogen_for_matter(
            str(getattr(selected, "matter_id", "")),
            refresh_registration_date=bool(refresh),
        )

    return render_template(
        "admin/ops_annuity.html",
        q=q,
        refresh=refresh,
        matches=matches,
        selected=selected,
        diag=diag,
        active_page="ops_annuity",
    )


@ops_admin_bp.get("/service-health")
@login_required
def service_health():
    _require_admin()
    payload = _service_health_snapshot()
    return render_template(
        "admin/ops_service_health.html",
        health=payload,
        active_page="ops_service_health",
    )


@ops_admin_bp.get("/service-health.json")
@login_required
def service_health_json():
    _require_admin()
    return jsonify(_service_health_snapshot())


@ops_admin_bp.post("/annuity/<matter_id>/ensure")
@login_required
def annuity_ensure(matter_id: str):
    _require_admin()
    from app.services.annuity.annuity_service import ensure_annuities_for_matter

    try:
        created = ensure_annuities_for_matter(
            str(matter_id),
            refresh_registration_date=True,
            commit=True,
        )
        flash(f"Renewal rows created or updated: {created} change(s).", "success")
    except Exception as exc:
        flash(f"Renewal auto-create failed: {exc}", "error")

    return redirect(url_for("ops_admin.annuity_home", q=str(matter_id), refresh="1"))


@ops_admin_bp.post("/annuity/<matter_id>/workflow-sync")
@login_required
def annuity_workflow_sync(matter_id: str):
    """Force a rebuild of annuity workflows for the matter (applies "next annuity" + N window)."""
    _require_admin()
    from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

    try:
        sync_annuity_workflows_for_matter(str(matter_id))
        db.session.commit()
        flash("Renewal Task  Done", "success")
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="ops_admin.annuity_workflow_sync.rollback",
                log_key="ops_admin.annuity_workflow_sync.rollback",
                log_window_seconds=300,
            )
        flash(f"Renewal Task  Failed: {exc}", "error")

    return redirect(url_for("ops_admin.annuity_home", q=str(matter_id), refresh="1"))
