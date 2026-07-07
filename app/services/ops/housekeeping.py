"""Operational housekeeping tasks (disk/DB size controls)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models.job_run import JobRun
from app.models.user_access_log import UserAccessLog
from app.ops.models import DiskSample, DurableJob
from app.services.ops.operational_metrics import collect_high_volume_table_metrics
from app.services.storage.file_asset_service import get_file_asset_service
from app.services.uploads.upload_session_service import get_upload_session_service
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from config import Config


def cleanup_error_reports(*, retention_days: int) -> int:
    """Delete old rows from error_reports and return deleted count."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=days)
    res = db.session.execute(
        text("DELETE FROM error_reports WHERE created_at < :cutoff"),
        {"cutoff": cutoff},
    )
    db.session.commit()
    return int(res.rowcount or 0)


def cleanup_job_runs(*, retention_days: int) -> int:
    """Delete old job_runs rows and return deleted count."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        deleted = (
            db.session.query(JobRun)
            .filter(func.coalesce(JobRun.finished_at, JobRun.started_at) < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(deleted or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_job_runs",
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_job_runs.rollback",
                log_key="housekeeping.cleanup_job_runs.rollback",
                log_window_seconds=300,
            )
        return 0


def cleanup_success_job_runs(*, retention_days: int) -> int:
    """Delete successful job_runs sooner than failures/diagnostic rows."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        deleted = (
            db.session.query(JobRun)
            .filter(JobRun.status == "success")
            .filter(func.coalesce(JobRun.finished_at, JobRun.started_at) < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(deleted or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_success_job_runs",
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_success_job_runs.rollback",
                log_key="housekeeping.cleanup_success_job_runs.rollback",
                log_window_seconds=300,
            )
        return 0


def cleanup_stale_job_runs(*, stale_minutes: int) -> int:
    """Mark long-running queued/running job_runs as failed and return count."""
    minutes = int(stale_minutes or 0)
    if minutes <= 0:
        return 0

    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    try:
        rows = (
            db.session.query(JobRun)
            .filter(JobRun.status.in_(["queued", "running"]))
            .filter(JobRun.started_at.isnot(None))
            .filter(JobRun.started_at < cutoff)
            .all()
        )
        if not rows:
            return 0
        for row in rows:
            row.status = "failed"
            row.error = "stale job_run cleanup"
            row.finished_at = datetime.utcnow()
        db.session.commit()
        return int(len(rows))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_stale_job_runs",
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_stale_job_runs.rollback",
                log_key="housekeeping.cleanup_stale_job_runs.rollback",
                log_window_seconds=300,
            )
        return 0


def cleanup_durable_jobs(
    *,
    retention_days: int,
    failed_retention_days: int,
) -> dict[str, int]:
    """Delete finished durable queue rows while preserving active/retry diagnostics."""
    try:
        finished_days = int(retention_days or 0)
    except Exception:
        finished_days = 0
    try:
        failed_days = int(failed_retention_days or 0)
    except Exception:
        failed_days = 0
    if finished_days <= 0 and failed_days <= 0:
        return {"finished_deleted": 0, "failed_deleted": 0}

    now = datetime.utcnow()
    finished_deleted = 0
    failed_deleted = 0
    try:
        if finished_days > 0:
            cutoff = now - timedelta(days=finished_days)
            finished_deleted = (
                db.session.query(DurableJob)
                .filter(DurableJob.status.in_(["succeeded", "cancelled"]))
                .filter(func.coalesce(DurableJob.finished_at, DurableJob.updated_at) < cutoff)
                .delete(synchronize_session=False)
            )
            db.session.commit()

        if failed_days > 0:
            cutoff = now - timedelta(days=failed_days)
            failed_deleted = (
                db.session.query(DurableJob)
                .filter(DurableJob.status == "failed")
                .filter(func.coalesce(DurableJob.finished_at, DurableJob.updated_at) < cutoff)
                .delete(synchronize_session=False)
            )
            db.session.commit()

        return {
            "finished_deleted": int(finished_deleted or 0),
            "failed_deleted": int(failed_deleted or 0),
        }
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_durable_jobs",
            log_key="housekeeping.cleanup_durable_jobs",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_durable_jobs.rollback",
                log_key="housekeeping.cleanup_durable_jobs.rollback",
                log_window_seconds=300,
            )
        return {"finished_deleted": 0, "failed_deleted": 0}


def _normalize_task_retention_days(raw: Any) -> dict[str, int]:
    if isinstance(raw, str):
        text_value = raw.strip()
        if not text_value:
            return {}
        try:
            raw = json.loads(text_value)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="housekeeping.normalize_task_retention_days",
                log_key="housekeeping.normalize_task_retention_days",
                log_window_seconds=300,
            )
            return {}
    if not isinstance(raw, dict):
        return {}

    out: dict[str, int] = {}
    for key, value in raw.items():
        task = str(key or "").strip()
        if not task:
            continue
        try:
            days = int(value or 0)
        except Exception:
            continue
        if days > 0:
            out[task] = days
    return out


def cleanup_durable_jobs_by_task(
    *,
    retention_days_by_task: Any,
    batch_size: int = 5000,
) -> dict[str, int]:
    """Delete high-volume finished durable jobs with task-specific retention."""
    task_days = _normalize_task_retention_days(retention_days_by_task)
    if not task_days:
        return {}

    now = datetime.utcnow()
    batch = max(100, min(20000, int(batch_size or 5000)))
    deleted_by_task: dict[str, int] = {}
    try:
        for task, days in sorted(task_days.items()):
            task_deleted = 0
            cutoff = now - timedelta(days=days)
            while True:
                ids = [
                    row[0]
                    for row in db.session.query(DurableJob.id)
                    .filter(DurableJob.task == task)
                    .filter(DurableJob.status.in_(["succeeded", "cancelled"]))
                    .filter(func.coalesce(DurableJob.finished_at, DurableJob.updated_at) < cutoff)
                    .order_by(DurableJob.id.asc())
                    .limit(batch)
                    .all()
                ]
                if not ids:
                    break
                deleted = (
                    db.session.query(DurableJob)
                    .filter(DurableJob.id.in_(ids))
                    .delete(synchronize_session=False)
                )
                db.session.commit()
                task_deleted += int(deleted or 0)
                if len(ids) < batch:
                    break
            deleted_by_task[task] = task_deleted
        return deleted_by_task
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_durable_jobs_by_task",
            log_key="housekeeping.cleanup_durable_jobs_by_task",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_durable_jobs_by_task.rollback",
                log_key="housekeeping.cleanup_durable_jobs_by_task.rollback",
                log_window_seconds=300,
            )
        return {task: 0 for task in task_days}


def cleanup_user_access_logs(*, retention_days: int) -> int:
    """Delete old user access log rows and return deleted count."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        deleted = (
            db.session.query(UserAccessLog)
            .filter(UserAccessLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(deleted or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_user_access_logs",
            log_key="housekeeping.cleanup_user_access_logs",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_user_access_logs.rollback",
                log_key="housekeeping.cleanup_user_access_logs.rollback",
                log_window_seconds=300,
            )
        return 0


def cleanup_disk_samples(*, retention_days: int) -> int:
    """Delete old disk usage samples and return deleted count."""
    days = int(retention_days or 0)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        deleted = (
            db.session.query(DiskSample)
            .filter(DiskSample.sampled_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(deleted or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_disk_samples",
            log_key="housekeeping.cleanup_disk_samples",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_disk_samples.rollback",
                log_key="housekeeping.cleanup_disk_samples.rollback",
                log_window_seconds=300,
            )
        return 0


def cleanup_upload_sessions() -> int:
    """Delete expired upload_session rows and return deleted count."""
    try:
        return int(get_upload_session_service().cleanup_expired() or 0)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_upload_sessions",
        )
        return 0


def cleanup_orphan_file_assets(*, min_age_days: int, limit: int) -> dict[str, int]:
    """Purge orphan file assets (disk + DB) and return summary."""
    file_service = get_file_asset_service()
    return file_service.purge_orphaned_assets(
        min_age_days=int(min_age_days or 0),
        limit=int(limit or 0),
        dry_run=False,
    )


def cleanup_file_delete_queue(*, limit: int) -> dict[str, int]:
    """Retry failed physical file deletes and return summary."""
    try:
        from app.services.files.file_delete_queue import drain_file_delete_queue

        return drain_file_delete_queue(limit=int(limit or 0))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_file_delete_queue",
            log_key="housekeeping.cleanup_file_delete_queue",
            log_window_seconds=300,
        )
        return {"picked": 0, "deleted": 0, "retried": 0, "failed": 0}


def cleanup_staging_file_assets(*, retention_hours: int) -> dict[str, int]:
    """Delete stale staging files under UPLOAD_FOLDER and return summary."""
    hours = int(retention_hours or 0)
    if hours <= 0:
        return {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    root = Path(current_app.config.get("UPLOAD_FOLDER") or Config.UPLOAD_FOLDER)
    if not root.exists():
        return {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    scanned = 0
    deleted = 0
    bytes_freed = 0
    try:
        first_exc: Exception | None = None
        error_count = 0
        for path in root.rglob("_staging_*"):
            try:
                if not path.is_file():
                    continue
                scanned += 1
                mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
                if mtime < cutoff:
                    bytes_freed += path.stat().st_size
                    path.unlink(missing_ok=True)
                    deleted += 1
            except Exception as exc:
                error_count += 1
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            report_swallowed_exception(
                first_exc,
                context=f"housekeeping.cleanup_staging_file_assets.file_errors (count={error_count})",
                log_key="housekeeping.cleanup_staging_file_assets.file_errors",
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_staging_file_assets.walk",
        )
        return {"scanned": scanned, "deleted": deleted, "bytes_freed": bytes_freed}

    return {"scanned": scanned, "deleted": deleted, "bytes_freed": bytes_freed}


def cleanup_old_backups() -> bool:
    """Run backup directory cleanup (best-effort)."""
    try:
        from app.blueprints.billing_invoices.routes.admin import _cleanup_old_backups

        _cleanup_old_backups()
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_old_backups",
        )
        return False


def cleanup_pdf_text_cache(*, cache_dir: str, retention_days: int) -> dict[str, int]:
    """Delete old cached PDF text files and return summary."""
    days = int(retention_days or 0)
    if days <= 0:
        return {"scanned": 0, "deleted": 0, "bytes_freed": 0}
    root = Path(cache_dir)
    if not root.exists():
        return {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    cutoff = datetime.utcnow() - timedelta(days=days)
    scanned = 0
    deleted = 0
    bytes_freed = 0
    try:
        first_exc: Exception | None = None
        error_count = 0
        for path in root.rglob("*.txt"):
            try:
                scanned += 1
                mtime = datetime.utcfromtimestamp(path.stat().st_mtime)
                if mtime < cutoff:
                    bytes_freed += path.stat().st_size
                    path.unlink(missing_ok=True)
                    deleted += 1
            except Exception as exc:
                error_count += 1
                if first_exc is None:
                    first_exc = exc
                continue
        if first_exc is not None:
            report_swallowed_exception(
                first_exc,
                context=f"housekeeping.cleanup_pdf_text_cache.file_errors (count={error_count})",
                log_key="housekeeping.cleanup_pdf_text_cache.file_errors",
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_pdf_text_cache.walk",
        )
        return {"scanned": scanned, "deleted": deleted, "bytes_freed": bytes_freed}
    return {"scanned": scanned, "deleted": deleted, "bytes_freed": bytes_freed}


def cleanup_stale_worker_heartbeats(
    *,
    retention_days: int | None = None,
    retention_minutes: int | None = None,
) -> dict[str, int]:
    """Delete stale durable-queue worker heartbeat rows from system_config."""
    if retention_minutes is not None:
        minutes = int(retention_minutes or 0)
        retention = timedelta(minutes=minutes)
    else:
        days = int(retention_days or 0)
        retention = timedelta(days=days)
    if retention.total_seconds() <= 0:
        return {"scanned": 0, "deleted": 0}

    cutoff = datetime.utcnow() - retention
    scanned = 0
    delete_keys: list[str] = []
    try:
        rows = (
            db.session.execute(
                text("""
                    SELECT key, value
                      FROM system_config
                     WHERE key LIKE :prefix
                    """),
                {"prefix": "ops.worker_heartbeat.%"},
            )
            .mappings()
            .all()
        )
        for row in rows:
            scanned += 1
            key = str(row.get("key") or "")
            try:
                payload = json.loads(str(row.get("value") or "{}"))
                updated_at_raw = payload.get("updated_at")
                updated_at = datetime.fromisoformat(str(updated_at_raw or ""))
            except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                delete_keys.append(key)
                continue
            if updated_at.tzinfo is not None:
                updated_at = updated_at.astimezone().replace(tzinfo=None)
            if updated_at < cutoff:
                delete_keys.append(key)

        deleted = 0
        for key in delete_keys:
            if not key:
                continue
            res = db.session.execute(
                text("DELETE FROM system_config WHERE key = :key"),
                {"key": key},
            )
            deleted += int(res.rowcount or 0)
        db.session.commit()
        return {"scanned": scanned, "deleted": deleted}
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.cleanup_stale_worker_heartbeats",
            log_key="housekeeping.cleanup_stale_worker_heartbeats",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="housekeeping.cleanup_stale_worker_heartbeats.rollback",
                log_key="housekeeping.cleanup_stale_worker_heartbeats.rollback",
                log_window_seconds=300,
            )
        return {"scanned": scanned, "deleted": 0}


def run_housekeeping() -> dict[str, Any]:
    """Run all enabled housekeeping tasks and return a summary dict."""
    cfg = current_app.config
    out: dict[str, Any] = {}

    # 1) Expired upload sessions (DB)
    out["upload_sessions_deleted"] = cleanup_upload_sessions()

    # 2) Error reports retention (DB)
    try:
        retention_days = int(cfg.get("ERROR_REPORT_RETENTION_DAYS", 90) or 0)
    except Exception:
        retention_days = 90
    out["error_reports_deleted"] = cleanup_error_reports(retention_days=retention_days)

    # 3) Job runs retention (DB)
    try:
        retention_days = int(cfg.get("JOB_RUN_RETENTION_DAYS", 90) or 0)
    except Exception:
        retention_days = 90
    out["job_runs_deleted"] = cleanup_job_runs(retention_days=retention_days)

    try:
        retention_days = int(cfg.get("JOB_RUN_SUCCESS_RETENTION_DAYS", 14) or 0)
    except Exception:
        retention_days = 14
    out["job_runs_success_deleted"] = cleanup_success_job_runs(retention_days=retention_days)

    # 3-2) Stale job runs (DB)
    try:
        stale_minutes = int(cfg.get("JOB_RUN_STALE_MINUTES", 720) or 0)
    except Exception:
        stale_minutes = 720
    out["job_runs_stale_marked"] = cleanup_stale_job_runs(stale_minutes=stale_minutes)

    # 3-2-1) Stale durable worker heartbeat rows (DB)
    try:
        retention_minutes = int(cfg.get("WORKER_HEARTBEAT_RETENTION_MINUTES", 60) or 0)
    except Exception:
        retention_minutes = 60
    out["worker_heartbeats_gc"] = cleanup_stale_worker_heartbeats(
        retention_minutes=retention_minutes,
    )

    try:
        retention_days = int(cfg.get("DURABLE_JOB_RETENTION_DAYS", 30) or 0)
    except Exception:
        retention_days = 30
    try:
        failed_retention_days = int(cfg.get("DURABLE_JOB_FAILED_RETENTION_DAYS", 180) or 0)
    except Exception:
        failed_retention_days = 180
    out["durable_jobs_deleted"] = cleanup_durable_jobs(
        retention_days=retention_days,
        failed_retention_days=failed_retention_days,
    )
    out["durable_jobs_task_deleted"] = cleanup_durable_jobs_by_task(
        retention_days_by_task=cfg.get("DURABLE_JOB_TASK_RETENTION_DAYS_JSON") or {},
    )

    try:
        retention_days = int(cfg.get("USER_ACCESS_LOG_RETENTION_DAYS", 90) or 0)
    except Exception:
        retention_days = 90
    out["user_access_logs_deleted"] = cleanup_user_access_logs(retention_days=retention_days)

    try:
        retention_days = int(cfg.get("DISK_SAMPLE_RETENTION_DAYS", 180) or 0)
    except Exception:
        retention_days = 180
    out["disk_samples_deleted"] = cleanup_disk_samples(retention_days=retention_days)
    try:
        out["high_volume_table_snapshot"] = collect_high_volume_table_metrics()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="housekeeping.high_volume_table_snapshot",
            log_key="housekeeping.high_volume_table_snapshot",
            log_window_seconds=300,
        )
        out["high_volume_table_snapshot"] = {"tables": [], "alerts": []}

    # 4) Orphaned file assets (disk + DB)
    try:
        enabled = bool(cfg.get("FILE_ASSET_GC_ENABLED", True))
    except Exception:
        enabled = True
    if enabled:
        try:
            min_age_days = int(cfg.get("FILE_ASSET_GC_MIN_AGE_DAYS", 30) or 0)
        except Exception:
            min_age_days = 30
        try:
            limit = int(cfg.get("FILE_ASSET_GC_LIMIT", 500) or 0)
        except Exception:
            limit = 500
        out["file_asset_gc"] = cleanup_orphan_file_assets(
            min_age_days=min_age_days,
            limit=limit,
        )
    else:
        out["file_asset_gc"] = {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    # 4-0) Retry failed file deletes from GC (DB + disk)
    try:
        enabled = bool(cfg.get("FILE_DELETE_QUEUE_ENABLED", True))
    except Exception:
        enabled = True
    if enabled:
        try:
            limit = int(cfg.get("FILE_DELETE_QUEUE_DRAIN_LIMIT", 200) or 0)
        except Exception:
            limit = 200
        out["file_delete_queue"] = cleanup_file_delete_queue(limit=limit)
    else:
        out["file_delete_queue"] = {"picked": 0, "deleted": 0, "retried": 0, "failed": 0}

    # 4-1) Staging file cleanup (disk)
    try:
        enabled = bool(cfg.get("FILE_ASSET_STAGING_GC_ENABLED", True))
    except Exception:
        enabled = True
    if enabled:
        try:
            retention_hours = int(cfg.get("FILE_ASSET_STAGING_RETENTION_HOURS", 24) or 0)
        except Exception:
            retention_hours = 24
        out["file_asset_staging_gc"] = cleanup_staging_file_assets(
            retention_hours=retention_hours,
        )
    else:
        out["file_asset_staging_gc"] = {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    # 5) PDF text cache cleanup (disk)
    try:
        enabled = bool(cfg.get("PDF_TEXT_CACHE_GC_ENABLED", True))
    except Exception:
        enabled = True
    if enabled:
        try:
            retention_days = int(cfg.get("PDF_TEXT_CACHE_RETENTION_DAYS", 30) or 0)
        except Exception:
            retention_days = 30
        cache_dir = cfg.get("PDF_TEXT_CACHE_DIR") or ""
        out["pdf_text_cache_gc"] = cleanup_pdf_text_cache(
            cache_dir=cache_dir,
            retention_days=retention_days,
        )
    else:
        out["pdf_text_cache_gc"] = {"scanned": 0, "deleted": 0, "bytes_freed": 0}

    # 6) Backup cleanup (disk)
    out["backup_cleanup_ran"] = cleanup_old_backups()

    return out
