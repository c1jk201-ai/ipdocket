from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _iter_files(root: str) -> Iterable[str]:
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def cleanup_directory(root: str, keep_days: int, *, sample_limit: int = 3) -> dict[str, Any]:
    """
    Cleanup old files based on modification time.
    """
    if not root or keep_days <= 0:
        return {"removed": 0, "errors": 0, "error_samples": []}

    now = time.time()
    cutoff = now - (keep_days * 86400)
    removed = 0
    errors = 0
    samples: list[dict[str, str]] = []

    if not os.path.exists(root):
        return {"removed": 0, "errors": 0, "error_samples": []}

    for fp in _iter_files(root):
        try:
            st = os.stat(fp)
            if st.st_mtime < cutoff:
                os.remove(fp)
                removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            errors += 1
            if len(samples) < sample_limit:
                try:
                    error_text = f"{type(exc).__name__}: {exc}"
                except Exception:
                    error_text = type(exc).__name__
                samples.append({"path": fp, "error": error_text})
            continue

    return {"removed": removed, "errors": errors, "error_samples": samples}


def preview_directory_cleanup(
    root: str, keep_days: int, *, sample_limit: int = 10
) -> dict[str, Any]:
    """Return a dry-run summary for files older than keep_days without deleting anything."""
    if not root or keep_days <= 0:
        return {
            "root": root,
            "retention_days": keep_days,
            "scanned": 0,
            "candidates": 0,
            "bytes_reclaimable": 0,
            "errors": 0,
            "samples": [],
        }

    if not os.path.exists(root):
        return {
            "root": root,
            "retention_days": keep_days,
            "scanned": 0,
            "candidates": 0,
            "bytes_reclaimable": 0,
            "errors": 0,
            "samples": [],
        }

    now = time.time()
    cutoff = now - (keep_days * 86400)
    scanned = 0
    candidates = 0
    bytes_reclaimable = 0
    errors = 0
    samples: list[dict[str, Any]] = []

    for fp in _iter_files(root):
        scanned += 1
        try:
            st = os.stat(fp)
            if st.st_mtime >= cutoff:
                continue
            candidates += 1
            bytes_reclaimable += int(st.st_size or 0)
            if len(samples) < sample_limit:
                try:
                    rel_path = os.path.relpath(fp, root)
                except ValueError:
                    rel_path = fp
                samples.append(
                    {
                        "path": rel_path,
                        "size_bytes": int(st.st_size or 0),
                        "mtime": datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
                    }
                )
        except FileNotFoundError:
            continue
        except Exception:
            errors += 1
            continue

    return {
        "root": root,
        "retention_days": keep_days,
        "scanned": scanned,
        "candidates": candidates,
        "bytes_reclaimable": bytes_reclaimable,
        "errors": errors,
        "samples": samples,
    }


def build_retention_preview(app) -> dict[str, Any]:
    uploads = (
        app.config.get("UPLOAD_FOLDER") or app.config.get("UPLOAD_STORAGE_ROOT") or ""
    ).strip()
    backups = (app.config.get("BACKUP_DIR") or app.config.get("BACKUP_STORAGE_ROOT") or "").strip()

    upload_days = int(app.config.get("UPLOAD_RETENTION_DAYS", 365))
    backup_days = int(app.config.get("BACKUP_RETENTION_DAYS", 90))

    return {
        "policies": {
            "upload_retention_days": upload_days,
            "backup_retention_days": backup_days,
            "ignored_inbox_email_days": int(app.config.get("INBOX_IGNORED_RETENTION_DAYS", 30)),
            "file_asset_gc_min_age_days": int(app.config.get("FILE_ASSET_GC_MIN_AGE_DAYS", 30)),
            "staging_retention_hours": int(
                app.config.get("FILE_ASSET_STAGING_RETENTION_HOURS", 24)
            ),
            "pdf_text_cache_retention_days": int(
                app.config.get("PDF_TEXT_CACHE_RETENTION_DAYS", 30)
            ),
        },
        "previews": {
            "uploads": preview_directory_cleanup(uploads, upload_days),
            "backups": preview_directory_cleanup(backups, backup_days),
        },
    }


def run_retention(app) -> dict[str, Any]:
    # Prefer the configured upload folder; fall back to the storage root used
    # by containerized deployments.
    uploads = (
        app.config.get("UPLOAD_FOLDER") or app.config.get("UPLOAD_STORAGE_ROOT") or ""
    ).strip()
    backups = (app.config.get("BACKUP_DIR") or app.config.get("BACKUP_STORAGE_ROOT") or "").strip()

    upload_days = int(app.config.get("UPLOAD_RETENTION_DAYS", 365))
    backup_days = int(app.config.get("BACKUP_RETENTION_DAYS", 90))

    upload_stats: dict[str, Any] = cleanup_directory(uploads, upload_days)
    backup_stats: dict[str, Any] = cleanup_directory(backups, backup_days)

    result: dict[str, Any] = {
        "uploads": {
            "root": uploads,
            "retention_days": upload_days,
            **upload_stats,
        },
        "backups": {
            "root": backups,
            "retention_days": backup_days,
            **backup_stats,
        },
    }
    result["total_removed"] = int(upload_stats.get("removed", 0)) + int(
        backup_stats.get("removed", 0)
    )
    result["total_errors"] = int(upload_stats.get("errors", 0)) + int(backup_stats.get("errors", 0))

    if result["total_errors"]:
        samples = []
        for scope in ("uploads", "backups"):
            for sample in result.get(scope, {}).get("error_samples") or []:
                if sample:
                    samples.append(sample)
        try:
            app.logger.warning(
                "Retention cleanup had errors (uploads=%s backups=%s). samples=%s",
                upload_stats.get("errors"),
                backup_stats.get("errors"),
                samples[:3],
            )
        except Exception:
            logger.exception("Retention cleanup: app.logger.warning failed")

    db_ref = None
    try:
        with app.app_context():
            from app.extensions import db as _db
            from app.models.job_run import JobRun

            db_ref = _db
            run_id = uuid.uuid4().hex
            now = datetime.utcnow()
            job_run = JobRun(
                job_name="retention.cleanup",
                run_id=run_id,
                status="success",
                started_at=now,
                finished_at=now,
                request_id=run_id,
            )
            try:
                job_run.output_ref = json.dumps(result, ensure_ascii=False)
            except Exception:
                job_run.output_ref = str(result)
            db_ref.session.add(job_run)
            db_ref.session.commit()
    except Exception:
        logger.exception("Retention cleanup: failed to record JobRun")
        if db_ref is not None:
            try:
                with app.app_context():
                    db_ref.session.rollback()
            except Exception:
                logger.exception("Retention cleanup: rollback failed")

    return result
