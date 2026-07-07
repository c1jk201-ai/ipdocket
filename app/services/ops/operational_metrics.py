"""Operational metrics used by readiness and internal dashboards."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app
from sqlalchemy import case, func, or_

from app.extensions import db
from app.models.error_report import ErrorReport
from app.models.job_run import JobRun
from app.models.parse_failure import ParseFailure
from app.models.system_config import SystemConfig
from app.models.user_access_log import UserAccessLog
from app.ops.models import DiskSample, DurableJob
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

_TABLE_SIZE_MB = 1024 * 1024
_HIGH_VOLUME_TABLE_DEFAULTS: dict[str, dict[str, int]] = {
    "job_runs": {
        "warn_mb": 200,
        "warn_rows": 200_000,
        "warn_rows_24h": 25_000,
    },
    "user_access_log": {
        "warn_mb": 128,
        "warn_rows": 250_000,
        "warn_rows_24h": 50_000,
    },
    "durable_jobs": {
        "warn_mb": 128,
        "warn_rows": 100_000,
        "warn_rows_24h": 20_000,
    },
}


def _safe(fn, default, *, context: str):
    try:
        return fn()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=300,
        )
        return default


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if isinstance(value, datetime) else None


def _age_seconds(value: datetime | None, *, now: datetime) -> int | None:
    if not isinstance(value, datetime):
        return None
    return max(0, int((now - value).total_seconds()))


def _config_int(name: str, default: int) -> int:
    try:
        return int(current_app.config.get(name, default) or 0)
    except Exception:
        return int(default)


def _dt_for_json(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    return value.isoformat() + "Z"


def _coerce_naive_utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    return _coerce_naive_utc(parsed)


def _percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = int(round((len(values) - 1) * p))
    idx = max(0, min(idx, len(values) - 1))
    return float(values[idx])


def _request_metrics(*, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)

    def _latency() -> dict[str, Any]:
        raw = (
            db.session.query(UserAccessLog.duration_ms)
            .filter(UserAccessLog.created_at >= since)
            .filter(UserAccessLog.duration_ms.isnot(None))
            .all()
        )
        values = [int(r[0]) for r in raw if r and r[0] is not None]
        return {
            "sample_count": len(values),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
        }

    def _five_xx_by_endpoint() -> list[dict[str, Any]]:
        rows = (
            db.session.query(
                func.coalesce(UserAccessLog.endpoint, UserAccessLog.path).label("endpoint"),
                func.count(UserAccessLog.id),
            )
            .filter(UserAccessLog.created_at >= since)
            .filter(UserAccessLog.status_code >= 500)
            .group_by(func.coalesce(UserAccessLog.endpoint, UserAccessLog.path))
            .order_by(func.count(UserAccessLog.id).desc())
            .limit(25)
            .all()
        )
        return [
            {"endpoint": str(endpoint or "-"), "count": int(count or 0)} for endpoint, count in rows
        ]

    total = _safe(
        lambda: int(
            db.session.query(func.count(UserAccessLog.id))
            .filter(UserAccessLog.created_at >= since)
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.requests.total",
    )
    five_xx = _safe(
        lambda: int(
            db.session.query(func.count(UserAccessLog.id))
            .filter(UserAccessLog.created_at >= since)
            .filter(UserAccessLog.status_code >= 500)
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.requests.5xx",
    )
    denied = _safe(
        lambda: int(
            db.session.query(func.count(UserAccessLog.id))
            .filter(UserAccessLog.created_at >= since)
            .filter(UserAccessLog.status_code == 403)
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.requests.denied",
    )
    latency = _safe(
        _latency,
        {"sample_count": 0, "p95_ms": None, "p99_ms": None},
        context="ops.operational_metrics.requests.latency",
    )
    return {
        "window_hours": 24,
        "total": total,
        "5xx_total": five_xx,
        "5xx_rate": round((five_xx / total), 6) if total else 0.0,
        "5xx_by_endpoint": _safe(
            _five_xx_by_endpoint,
            [],
            context="ops.operational_metrics.requests.5xx_by_endpoint",
        ),
        "policy_denied_count": denied,
        "latency": latency,
    }


def _db_pool_metrics() -> dict[str, Any]:
    pool = db.engine.pool

    def _call(name: str) -> int | None:
        attr = getattr(pool, name, None)
        if attr is None:
            return None
        try:
            return int(attr() if callable(attr) else attr)
        except Exception:
            return None

    size = _call("size")
    checked_out = _call("checkedout")
    checked_in = _call("checkedin")
    overflow = _call("overflow")
    total_capacity = None
    max_overflow = current_app.config.get("DB_MAX_OVERFLOW")
    try:
        if size is not None:
            total_capacity = int(size) + max(0, int(max_overflow or 0))
    except Exception:
        total_capacity = size
    utilization = (
        round(float(checked_out or 0) / float(total_capacity), 4)
        if total_capacity and total_capacity > 0
        else None
    )
    return {
        "pool_class": type(pool).__name__,
        "size": size,
        "checked_out": checked_out,
        "checked_in": checked_in,
        "overflow": overflow,
        "total_capacity": total_capacity,
        "utilization": utilization,
        "status": getattr(pool, "status", lambda: "")(),
    }


def _durable_queue_metrics(*, now: datetime) -> dict[str, Any]:
    status_expr = func.lower(func.trim(func.coalesce(DurableJob.status, "")))
    total_case = func.count(DurableJob.id)
    queued_case = func.sum(case((status_expr == "queued", 1), else_=0))
    running_case = func.sum(case((status_expr == "running", 1), else_=0))
    failed_case = func.sum(case((status_expr == "failed", 1), else_=0))
    retry_case = func.sum(case((DurableJob.attempts > 1, 1), else_=0))
    active_oldest_case = func.min(
        case((status_expr.in_(("queued", "running", "failed")), DurableJob.run_at), else_=None)
    )
    oldest_queued_case = func.min(case((status_expr == "queued", DurableJob.run_at), else_=None))
    rows = _safe(
        lambda: db.session.query(
            DurableJob.queue,
            DurableJob.task,
            total_case,
            queued_case,
            running_case,
            failed_case,
            retry_case,
            active_oldest_case,
            oldest_queued_case,
        )
        .group_by(DurableJob.queue, DurableJob.task)
        .order_by(DurableJob.queue.asc(), DurableJob.task.asc())
        .limit(100)
        .all(),
        [],
        context="ops.operational_metrics.durable.by_queue_task",
    )
    items: list[dict[str, Any]] = []
    max_lag = 0
    oldest_queued_age = 0
    oldest_active_age = 0
    for (
        queue,
        task,
        total_count,
        queued,
        running,
        failed,
        retries,
        oldest_active_run_at,
        oldest_queued,
    ) in rows:
        lag = _age_seconds(oldest_queued, now=now) if oldest_queued and oldest_queued <= now else 0
        queued_age = (
            _age_seconds(oldest_queued, now=now) if oldest_queued and oldest_queued <= now else 0
        )
        active_age = (
            _age_seconds(oldest_active_run_at, now=now)
            if oldest_active_run_at and oldest_active_run_at <= now
            else 0
        )
        max_lag = max(max_lag, int(lag or 0))
        oldest_queued_age = max(oldest_queued_age, int(queued_age or 0))
        oldest_active_age = max(oldest_active_age, int(active_age or 0))
        items.append(
            {
                "queue": str(queue or "-"),
                "task": str(task or "-"),
                "total": int(total_count or 0),
                "queued": int(queued or 0),
                "running": int(running or 0),
                "failed": int(failed or 0),
                "retry_count": int(retries or 0),
                "oldest_run_at": _dt_iso(oldest_active_run_at),
                "oldest_active_run_at": _dt_iso(oldest_active_run_at),
                "oldest_queued_run_at": _dt_iso(oldest_queued),
                "queue_lag_seconds": int(lag or 0),
                "oldest_queued_age_seconds": int(queued_age or 0),
                "oldest_active_age_seconds": int(active_age or 0),
            }
        )
    since = now - timedelta(hours=24)
    stale_lock_recovery_count = _safe(
        lambda: int(
            db.session.query(func.count(DurableJob.id))
            .filter(DurableJob.updated_at >= since)
            .filter(DurableJob.last_error.ilike("%[recovered stale lock]%"))
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.durable.stale_lock_recovery_count",
    )
    total_jobs = sum(item["total"] for item in items)
    retry_total = sum(item["retry_count"] for item in items)
    totals = {
        "total": total_jobs,
        "queued": sum(item["queued"] for item in items),
        "running": sum(item["running"] for item in items),
        "failed": sum(item["failed"] for item in items),
        "retry_count": retry_total,
        "retry_rate": round(float(retry_total) / float(total_jobs), 6) if total_jobs else 0.0,
        "max_queue_lag_seconds": max_lag,
        "oldest_queued_age_seconds": oldest_queued_age,
        "oldest_active_age_seconds": oldest_active_age,
        "stale_lock_recovery_count_24h": int(stale_lock_recovery_count or 0),
    }
    return {"totals": totals, "by_queue_task": items}


def _postgres_table_size_bytes(table_name: str) -> int | None:
    try:
        bind = db.session.get_bind()
        dialect = (getattr(bind.dialect, "name", "") or "").lower() if bind else ""
    except Exception:
        dialect = ""
    if not dialect.startswith("postgres"):
        return None

    value = _safe(
        lambda: db.session.execute(
            text(
                """
                SELECT pg_total_relation_size(to_regclass(:table_name))::bigint
                """
            ),
            {"table_name": table_name},
        ).scalar(),
        None,
        context=f"ops.operational_metrics.table_growth.{table_name}.size",
    )
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _table_thresholds(table_name: str) -> dict[str, int]:
    defaults = _HIGH_VOLUME_TABLE_DEFAULTS.get(table_name, {})
    prefix = table_name.upper()
    default_warn_mb = int(defaults.get("warn_mb") or 256)
    default_warn_rows = int(defaults.get("warn_rows") or 500_000)
    default_warn_rows_24h = int(defaults.get("warn_rows_24h") or 50_000)

    def _threshold(*, per_table_key: str, generic_key: str, default: int) -> int:
        per_table_value = _config_int(per_table_key, 0)
        if per_table_value > 0:
            return per_table_value
        generic_value = _config_int(generic_key, 0)
        if generic_value > 0:
            return generic_value
        return default

    warn_mb = _threshold(
        per_table_key=f"OPERATIONAL_TABLE_{prefix}_WARN_MB",
        generic_key="OPERATIONAL_TABLE_WARN_MB",
        default=default_warn_mb,
    )
    warn_rows = _threshold(
        per_table_key=f"OPERATIONAL_TABLE_{prefix}_WARN_ROWS",
        generic_key="OPERATIONAL_TABLE_WARN_ROWS",
        default=default_warn_rows,
    )
    warn_rows_24h = _threshold(
        per_table_key=f"OPERATIONAL_TABLE_{prefix}_WARN_ROWS_24H",
        generic_key="OPERATIONAL_TABLE_WARN_ROWS_24H",
        default=default_warn_rows_24h,
    )
    expired_warn_rows = _config_int("OPERATIONAL_TABLE_EXPIRED_WARN_ROWS", 1000)
    return {
        "warn_bytes": max(0, int(warn_mb)) * _TABLE_SIZE_MB,
        "warn_mb": max(0, int(warn_mb)),
        "warn_rows": max(0, int(warn_rows)),
        "warn_rows_24h": max(0, int(warn_rows_24h)),
        "expired_warn_rows": max(0, int(expired_warn_rows)),
    }


def _count_query(builder, *, context: str) -> int:
    return int(_safe(lambda: int(builder() or 0), 0, context=context) or 0)


def _aggregate_table(
    *,
    table_name: str,
    model,
    pk_column,
    time_expr,
    now: datetime,
) -> dict[str, Any]:
    aggregate = _safe(
        lambda: db.session.query(func.count(pk_column), func.min(time_expr), func.max(time_expr))
        .select_from(model)
        .one(),
        (0, None, None),
        context=f"ops.operational_metrics.table_growth.{table_name}.aggregate",
    )
    try:
        row_count, oldest_at, latest_at = aggregate
    except Exception:
        row_count, oldest_at, latest_at = 0, None, None
    since_24h = now - timedelta(hours=24)
    rows_24h = _count_query(
        lambda: db.session.query(func.count(pk_column))
        .select_from(model)
        .filter(time_expr >= since_24h)
        .scalar(),
        context=f"ops.operational_metrics.table_growth.{table_name}.rows_24h",
    )
    return {
        "table": table_name,
        "row_count": int(row_count or 0),
        "rows_24h": int(rows_24h or 0),
        "oldest_at": _dt_for_json(oldest_at),
        "latest_at": _dt_for_json(latest_at),
    }


def _job_runs_expired_rows(now: datetime) -> tuple[int, dict[str, Any]]:
    retention_days = _config_int("JOB_RUN_RETENTION_DAYS", 90)
    success_days = _config_int("JOB_RUN_SUCCESS_RETENTION_DAYS", 14)
    time_expr = func.coalesce(JobRun.finished_at, JobRun.started_at)
    status_expr = func.lower(func.coalesce(JobRun.status, ""))
    success_expired = 0
    other_expired = 0
    if success_days > 0:
        success_expired = _count_query(
            lambda: db.session.query(func.count(JobRun.id))
            .filter(status_expr == "success")
            .filter(time_expr < now - timedelta(days=success_days))
            .scalar(),
            context="ops.operational_metrics.table_growth.job_runs.expired_success",
        )
    if retention_days > 0:
        other_expired = _count_query(
            lambda: db.session.query(func.count(JobRun.id))
            .filter(or_(status_expr != "success", JobRun.status.is_(None)))
            .filter(time_expr < now - timedelta(days=retention_days))
            .scalar(),
            context="ops.operational_metrics.table_growth.job_runs.expired_other",
        )
    return success_expired + other_expired, {
        "days": retention_days,
        "success_days": success_days,
    }


def _user_access_expired_rows(now: datetime) -> tuple[int, dict[str, Any]]:
    retention_days = _config_int("USER_ACCESS_LOG_RETENTION_DAYS", 90)
    expired = 0
    if retention_days > 0:
        expired = _count_query(
            lambda: db.session.query(func.count(UserAccessLog.id))
            .filter(UserAccessLog.created_at < now - timedelta(days=retention_days))
            .scalar(),
            context="ops.operational_metrics.table_growth.user_access_log.expired",
        )
    return expired, {"days": retention_days}


def _durable_jobs_expired_rows(now: datetime) -> tuple[int, dict[str, Any]]:
    retention_days = _config_int("DURABLE_JOB_RETENTION_DAYS", 30)
    failed_days = _config_int("DURABLE_JOB_FAILED_RETENTION_DAYS", 180)
    status_expr = func.lower(func.trim(func.coalesce(DurableJob.status, "")))
    time_expr = func.coalesce(DurableJob.finished_at, DurableJob.updated_at)
    finished_expired = 0
    failed_expired = 0
    if retention_days > 0:
        finished_expired = _count_query(
            lambda: db.session.query(func.count(DurableJob.id))
            .filter(status_expr.in_(["succeeded", "cancelled"]))
            .filter(time_expr < now - timedelta(days=retention_days))
            .scalar(),
            context="ops.operational_metrics.table_growth.durable_jobs.expired_finished",
        )
    if failed_days > 0:
        failed_expired = _count_query(
            lambda: db.session.query(func.count(DurableJob.id))
            .filter(status_expr == "failed")
            .filter(time_expr < now - timedelta(days=failed_days))
            .scalar(),
            context="ops.operational_metrics.table_growth.durable_jobs.expired_failed",
        )
    return finished_expired + failed_expired, {
        "finished_days": retention_days,
        "failed_days": failed_days,
    }


def _table_alerts(item: dict[str, Any], thresholds: dict[str, int]) -> list[dict[str, Any]]:
    table = str(item.get("table") or "-")
    alerts: list[dict[str, Any]] = []
    row_count = int(item.get("row_count") or 0)
    rows_24h = int(item.get("rows_24h") or 0)
    size_bytes = item.get("size_bytes")
    expired_rows = int(item.get("expired_rows") or 0)

    if thresholds["warn_rows"] and row_count >= thresholds["warn_rows"]:
        alerts.append(
            {
                "level": "warning",
                "code": f"{table}_row_count_high",
                "message": f"{table} rows={row_count} threshold={thresholds['warn_rows']}",
            }
        )
    if (
        isinstance(size_bytes, int)
        and thresholds["warn_bytes"]
        and size_bytes >= thresholds["warn_bytes"]
    ):
        alerts.append(
            {
                "level": "warning",
                "code": f"{table}_size_high",
                "message": (
                    f"{table} size_mb={round(size_bytes / _TABLE_SIZE_MB, 1)} "
                    f"threshold_mb={thresholds['warn_mb']}"
                ),
            }
        )
    if thresholds["warn_rows_24h"] and rows_24h >= thresholds["warn_rows_24h"]:
        alerts.append(
            {
                "level": "warning",
                "code": f"{table}_rows_24h_high",
                "message": f"{table} rows_24h={rows_24h} threshold={thresholds['warn_rows_24h']}",
            }
        )
    if thresholds["expired_warn_rows"] and expired_rows >= thresholds["expired_warn_rows"]:
        alerts.append(
            {
                "level": "warning",
                "code": f"{table}_retention_expired_rows",
                "message": (
                    f"{table} rows past retention={expired_rows} "
                    f"threshold={thresholds['expired_warn_rows']}"
                ),
            }
        )
    return alerts


def collect_high_volume_table_metrics(*, now: datetime | None = None) -> dict[str, Any]:
    """Collect growth and retention signals for operational tables with large churn."""
    now = now or datetime.utcnow()
    table_specs = [
        {
            "table": "job_runs",
            "model": JobRun,
            "pk": JobRun.id,
            "time_expr": func.coalesce(JobRun.finished_at, JobRun.started_at),
            "expired": _job_runs_expired_rows,
        },
        {
            "table": "user_access_log",
            "model": UserAccessLog,
            "pk": UserAccessLog.id,
            "time_expr": UserAccessLog.created_at,
            "expired": _user_access_expired_rows,
        },
        {
            "table": "durable_jobs",
            "model": DurableJob,
            "pk": DurableJob.id,
            "time_expr": DurableJob.created_at,
            "expired": _durable_jobs_expired_rows,
        },
    ]

    tables: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for spec in table_specs:
        table_name = str(spec["table"])
        thresholds = _table_thresholds(table_name)
        item = _aggregate_table(
            table_name=table_name,
            model=spec["model"],
            pk_column=spec["pk"],
            time_expr=spec["time_expr"],
            now=now,
        )
        expired_rows, retention = spec["expired"](now)
        size_bytes = _postgres_table_size_bytes(table_name)
        item.update(
            {
                "size_bytes": size_bytes,
                "size_mb": (
                    round(size_bytes / _TABLE_SIZE_MB, 2) if isinstance(size_bytes, int) else None
                ),
                "expired_rows": int(expired_rows or 0),
                "retention": retention,
                "thresholds": thresholds,
            }
        )
        item_alerts = _table_alerts(item, thresholds)
        item["alerts"] = item_alerts
        alerts.extend(item_alerts)
        tables.append(item)

    return {
        "generated_at": _dt_for_json(now),
        "tables": tables,
        "by_table": {item["table"]: item for item in tables},
        "alerts": alerts,
    }


def _external_api_failures(*, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)
    labels = {
        "openai": ("openai", "llm", "gpt"),
    }
    rows = _safe(
        lambda: db.session.query(ErrorReport.path, ErrorReport.endpoint, ErrorReport.message)
        .filter(ErrorReport.created_at >= since)
        .all(),
        [],
        context="ops.operational_metrics.external_api.rows",
    )
    out = {key: 0 for key in labels}
    for path, endpoint, message in rows:
        haystack = " ".join(str(v or "").lower() for v in (path, endpoint, message))
        for key, needles in labels.items():
            if any(needle in haystack for needle in needles):
                out[key] += 1
    return out


def _upload_parse_failures(*, now: datetime) -> int:
    since = now - timedelta(hours=24)
    return int(
        _safe(
            lambda: int(
                db.session.query(func.count(ParseFailure.id))
                .filter(ParseFailure.created_at >= since)
                .filter(
                    (ParseFailure.kind == "upload")
                    | (func.lower(func.coalesce(ParseFailure.source, "")).like("%upload%"))
                )
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.upload_parse_failures",
        )
    )


def _disk_trend() -> list[dict[str, Any]]:
    labels = _safe(
        lambda: [row[0] for row in db.session.query(DiskSample.mount_label).distinct().all()],
        [],
        context="ops.operational_metrics.disk.labels",
    )
    out = []
    for label in labels:
        samples = _safe(
            lambda label=label: DiskSample.query.filter(DiskSample.mount_label == label)
            .order_by(DiskSample.sampled_at.desc())
            .limit(2)
            .all(),
            [],
            context="ops.operational_metrics.disk.samples",
        )
        latest = samples[0] if samples else None
        previous = samples[1] if len(samples) > 1 else None
        if not latest:
            continue
        out.append(
            {
                "mount_label": str(label or "-"),
                "path": latest.path,
                "used_pct": float(latest.used_pct or 0.0),
                "free_bytes": int(latest.free_bytes or 0),
                "sampled_at": _dt_iso(latest.sampled_at),
                "used_pct_delta": (
                    round(float(latest.used_pct or 0.0) - float(previous.used_pct or 0.0), 4)
                    if previous
                    else None
                ),
            }
        )
    return out


def _heartbeat_metrics(*, now: datetime) -> dict[str, Any]:
    last_scheduler = _safe(
        lambda: JobRun.query.filter(
            JobRun.job_name == "scheduler_heartbeat",
            JobRun.status == "success",
        )
        .order_by(JobRun.finished_at.desc(), JobRun.started_at.desc())
        .first(),
        None,
        context="ops.operational_metrics.heartbeat.scheduler",
    )
    scheduler_at = None
    if last_scheduler is not None:
        scheduler_at = last_scheduler.finished_at or last_scheduler.started_at

    worker_rows = _safe(
        lambda: SystemConfig.query.filter(SystemConfig.key.like("ops.worker_heartbeat.%")).all(),
        [],
        context="ops.operational_metrics.heartbeat.worker_rows",
    )
    try:
        configured_stale_after = int(
            current_app.config.get("READY_WORKER_HEARTBEAT_MAX_AGE_SECONDS") or 0
        )
    except Exception:
        configured_stale_after = 0
    try:
        worker_interval = int(current_app.config.get("WORKER_HEARTBEAT_INTERVAL_SECONDS") or 30)
    except Exception:
        worker_interval = 30
    worker_stale_after_seconds = (
        configured_stale_after if configured_stale_after > 0 else max(120, worker_interval * 4)
    )
    workers = []
    newest_worker_at = None
    stale_worker_count = 0
    malformed_worker_count = 0
    for row in worker_rows:
        parsed = {}
        try:
            parsed = json.loads(row.value or "{}")
        except Exception:
            parsed = {}
            malformed_worker_count += 1
        updated_raw = parsed.get("updated_at")
        updated_at = _parse_iso_datetime(updated_raw)
        age_seconds = _age_seconds(updated_at, now=now)
        if age_seconds is None or age_seconds > worker_stale_after_seconds:
            stale_worker_count += 1
            continue
        if updated_at and (newest_worker_at is None or updated_at > newest_worker_at):
            newest_worker_at = updated_at
        workers.append(
            {
                "key": row.key,
                "updated_at": _dt_iso(updated_at),
                "age_seconds": age_seconds,
                "queues": parsed.get("queues") or [],
                "worker_id": parsed.get("worker_id"),
            }
        )

    return {
        "scheduler": {
            "last_success_at": _dt_iso(scheduler_at),
            "age_seconds": _age_seconds(scheduler_at, now=now),
        },
        "workers": workers,
        "newest_worker_age_seconds": _age_seconds(newest_worker_at, now=now),
        "worker_stale_after_seconds": worker_stale_after_seconds,
        "stale_worker_count": stale_worker_count,
        "malformed_worker_count": malformed_worker_count,
    }


def _db_runtime_metrics(*, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)
    rows = _safe(
        lambda: db.session.query(ErrorReport.message, ErrorReport.error_type)
        .filter(ErrorReport.created_at >= since)
        .all(),
        [],
        context="ops.operational_metrics.db_runtime.rows",
    )
    pool_wait = 0
    statement_timeout = 0
    idle_in_tx_kill = 0
    for message, error_type in rows:
        haystack = " ".join(str(v or "").lower() for v in (message, error_type))
        if "pool" in haystack and any(token in haystack for token in ("timeout", "wait")):
            pool_wait += 1
        if "statement timeout" in haystack or "query_canceled" in haystack:
            statement_timeout += 1
        if "idle in transaction" in haystack or "idle-in-tx" in haystack:
            idle_in_tx_kill += 1
    return {
        "window_hours": 24,
        "pool_wait_error_count": pool_wait,
        "statement_timeout_count": statement_timeout,
        "idle_in_tx_kill_count": idle_in_tx_kill,
    }


def _upload_metrics(*, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)

    def _parse_failure_types() -> list[dict[str, Any]]:
        label = func.coalesce(ParseFailure.field_name, ParseFailure.error, ParseFailure.source)
        rows = (
            db.session.query(ParseFailure.kind, label, func.count(ParseFailure.id))
            .filter(ParseFailure.created_at >= since)
            .filter(
                (ParseFailure.kind == "upload")
                | (func.lower(func.coalesce(ParseFailure.source, "")).like("%upload%"))
            )
            .group_by(ParseFailure.kind, label)
            .order_by(func.count(ParseFailure.id).desc())
            .limit(20)
            .all()
        )
        return [
            {
                "kind": str(kind or "-"),
                "type": str(type_label or "-")[:160],
                "count": int(count or 0),
            }
            for kind, type_label, count in rows
        ]

    parse_failure_types = _safe(
        _parse_failure_types,
        [],
        context="ops.operational_metrics.upload.parse_failure_types",
    )

    scan_command = ""
    try:
        scan_command = str(current_app.config.get("UPLOAD_VIRUS_SCAN_COMMAND") or "").strip()
    except Exception:
        scan_command = ""

    virus_rows = _safe(
        lambda: db.session.query(ErrorReport.message, ErrorReport.error_type)
        .filter(ErrorReport.created_at >= since)
        .filter(
            (ErrorReport.message.ilike("%virus_scan%"))
            | (ErrorReport.error_type.ilike("%UploadSecurity%"))
        )
        .all(),
        [],
        context="ops.operational_metrics.upload.virus_rows",
    )
    rejected = 0
    timeout = 0
    failed = 0
    for message, error_type in virus_rows:
        haystack = " ".join(str(v or "").lower() for v in (message, error_type))
        if "timeout" in haystack:
            timeout += 1
        elif "reject" in haystack or "infect" in haystack:
            rejected += 1
        else:
            failed += 1

    temp_cleanup_count = _safe(
        lambda: int(
            db.session.query(func.count(JobRun.id))
            .filter(JobRun.started_at >= since)
            .filter(JobRun.job_name.in_(["daily_housekeeping", "upload_session_cleanup"]))
            .filter(
                (JobRun.output_ref.ilike("%upload_sessions_deleted%"))
                | (JobRun.output_ref.ilike("%file_asset_staging_gc%"))
                | (JobRun.output_ref.ilike("%temp%"))
            )
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.upload.temp_cleanup_count",
    )

    return {
        "window_hours": 24,
        "parse_failure_count": sum(item["count"] for item in parse_failure_types),
        "parse_failure_types": parse_failure_types,
        "virus_scan": {
            "disabled": not bool(scan_command),
            "rejected_count": rejected,
            "timeout_count": timeout,
            "failed_count": failed,
        },
        "temp_cleanup_count": int(temp_cleanup_count or 0),
    }


def _automation_operational_metrics(*, now: datetime) -> dict[str, Any]:
    try:
        from app.services.automation.automation_monitoring import (
            check_automation_drift,
            collect_automation_metrics,
        )

        metrics = collect_automation_metrics(window_days=7)
        warning_rates = metrics.get("warning_rates") or {}
        override = SystemConfig.get_config("FOREIGN_EMAIL_AUTOMATION_LEVEL_OVERRIDE", "") or ""
        return {
            "window_days": metrics.get("window_days", 7),
            "missing_evidence_rate": float(warning_rates.get("missing_evidence") or 0.0),
            "match_miss_rate": float(warning_rates.get("missing_match") or 0.0),
            "review_rate": float(metrics.get("review_rate") or 0.0),
            "ready_rate": float(metrics.get("ready_rate") or 0.0),
            "error_rate": float(metrics.get("error_rate") or 0.0),
            "auto_downgrade": override.strip() or "",
            "alerts": check_automation_drift(metrics),
        }
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="ops.operational_metrics.automation",
            log_key="ops.operational_metrics.automation",
            log_window_seconds=300,
        )
        return {"window_days": 7, "error": type(exc).__name__}


def _matter_billing_metrics() -> dict[str, Any]:
    try:
        from app.models.assets import FileAsset, MatterFileAsset
        from app.models.matter import Matter
        from app.models.legacy_finance import (
            CaseExpenseInvoiceMap,
            ExternalInvoiceCaseLink,
            ExternalInvoiceCaseMap,
        )
        from app.models.workflow import Workflow

        file_orphans = _safe(
            lambda: int(
                db.session.query(func.count(MatterFileAsset.matter_file_id))
                .outerjoin(Matter, MatterFileAsset.matter_id == Matter.matter_id)
                .outerjoin(FileAsset, MatterFileAsset.file_asset_id == FileAsset.file_asset_id)
                .filter(
                    (Matter.matter_id.is_(None))
                    | (FileAsset.file_asset_id.is_(None))
                    | (FileAsset.is_deleted.is_(True))
                )
                .filter(MatterFileAsset.is_deleted.is_(False))
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.matter_billing.file_orphans",
        )
        invoice_link_orphans = _safe(
            lambda: int(
                db.session.query(func.count(ExternalInvoiceCaseLink.id))
                .outerjoin(Matter, ExternalInvoiceCaseLink.matter_id == Matter.matter_id)
                .filter(Matter.matter_id.is_(None))
                .filter(ExternalInvoiceCaseLink.is_deleted.is_(False))
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.matter_billing.invoice_link_orphans",
        )
        invoice_map_orphans = _safe(
            lambda: int(
                db.session.query(func.count(ExternalInvoiceCaseMap.id))
                .outerjoin(Matter, ExternalInvoiceCaseMap.matter_id == Matter.matter_id)
                .filter(Matter.matter_id.is_(None))
                .filter(ExternalInvoiceCaseMap.is_deleted.is_(False))
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.matter_billing.invoice_map_orphans",
        )
        expense_invoice_orphans = _safe(
            lambda: int(
                db.session.query(func.count(CaseExpenseInvoiceMap.id))
                .outerjoin(Matter, CaseExpenseInvoiceMap.matter_id == Matter.matter_id)
                .filter(Matter.matter_id.is_(None))
                .filter(CaseExpenseInvoiceMap.is_deleted == 0)
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.matter_billing.expense_invoice_orphans",
        )
        workflow_missing_matter = _safe(
            lambda: int(
                db.session.query(func.count(Workflow.id))
                .outerjoin(Matter, Workflow.case_id == Matter.matter_id)
                .filter(Workflow.case_id.isnot(None))
                .filter(Matter.matter_id.is_(None))
                .scalar()
                or 0
            ),
            0,
            context="ops.operational_metrics.matter_billing.workflow_missing_matter",
        )
        legacy_like_rows = _safe(
            lambda: [
                row[0]
                for row in db.session.query(Workflow.case_id)
                .outerjoin(Matter, Workflow.case_id == Matter.matter_id)
                .filter(Workflow.case_id.isnot(None))
                .filter(Matter.matter_id.is_(None))
                .limit(5000)
                .all()
            ],
            [],
            context="ops.operational_metrics.matter_billing.legacy_case_rows",
        )
        legacy_case_id_depend_count = sum(
            1 for value in legacy_like_rows if str(value or "").strip().isdigit()
        )
        return {
            "orphan_link_count": (
                int(file_orphans or 0)
                + int(invoice_link_orphans or 0)
                + int(invoice_map_orphans or 0)
                + int(expense_invoice_orphans or 0)
            ),
            "file_asset_orphan_link_count": int(file_orphans or 0),
            "external_invoice_orphan_link_count": int(invoice_link_orphans or 0)
            + int(invoice_map_orphans or 0),
            "expense_invoice_orphan_link_count": int(expense_invoice_orphans or 0),
            "workflow_missing_matter_count": int(workflow_missing_matter or 0),
            "legacy_cases_id_depend_count": int(legacy_case_id_depend_count or 0),
        }
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="ops.operational_metrics.matter_billing",
            log_key="ops.operational_metrics.matter_billing",
            log_window_seconds=300,
        )
        return {"error": type(exc).__name__}


def _security_metrics(*, now: datetime) -> dict[str, Any]:
    since = now - timedelta(hours=24)
    admin_cidr_reject_count = _safe(
        lambda: int(
            db.session.query(func.count(UserAccessLog.id))
            .filter(UserAccessLog.created_at >= since)
            .filter(UserAccessLog.status_code == 403)
            .filter(UserAccessLog.path.like("/admin%"))
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.security.admin_cidr_reject_count",
    )
    csp_violation_count = _safe(
        lambda: int(
            db.session.query(func.count(ErrorReport.id))
            .filter(ErrorReport.created_at >= since)
            .filter(
                (ErrorReport.error_type == "CSPViolation")
                | (ErrorReport.endpoint.ilike("%csp%"))
                | (ErrorReport.message.ilike("%content-security-policy%"))
            )
            .scalar()
            or 0
        ),
        0,
        context="ops.operational_metrics.security.csp_violation_count",
    )
    policy_bypass_count = 0
    try:
        from app.security.policy_engine import get_policy_guard_metrics

        policy_bypass_count = int(
            (get_policy_guard_metrics() or {}).get("policy_bypass_count") or 0
        )
    except Exception:
        policy_bypass_count = 0
    return {
        "window_hours": 24,
        "policy_bypass_count": policy_bypass_count,
        "csp_violation_count": int(csp_violation_count or 0),
        "admin_cidr_reject_count": int(admin_cidr_reject_count or 0),
    }


def _migration_status() -> dict[str, Any]:
    try:
        from app.core.setup.db_guards import check_migrations_status

        return dict(check_migrations_status(current_app._get_current_object()) or {})
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="ops.operational_metrics.migration_status",
            log_key="ops.operational_metrics.migration_status",
            log_window_seconds=300,
        )
        return {"ok": False, "error": type(exc).__name__}


def collect_operational_metrics() -> dict[str, Any]:
    now = datetime.utcnow()
    request_metrics = _request_metrics(now=now)
    upload_metrics = _upload_metrics(now=now)
    return {
        "generated_at": _dt_iso(now),
        "requests": request_metrics,
        "db_pool": _safe(
            _db_pool_metrics,
            {"status": "unavailable"},
            context="ops.operational_metrics.db_pool",
        ),
        "db_runtime": _db_runtime_metrics(now=now),
        "durable_queue": _durable_queue_metrics(now=now),
        "external_api_failures_24h": _external_api_failures(now=now),
        "policy_denied_count_24h": request_metrics.get("policy_denied_count", 0),
        "upload_parse_failure_count_24h": upload_metrics.get("parse_failure_count", 0),
        "upload": upload_metrics,
        "automation": _automation_operational_metrics(now=now),
        "matter_billing": _matter_billing_metrics(),
        "security": _security_metrics(now=now),
        "disk_usage_trend": _disk_trend(),
        "heartbeats": _heartbeat_metrics(now=now),
        "table_growth": collect_high_volume_table_metrics(now=now),
        "migration_drift": _migration_status(),
    }
