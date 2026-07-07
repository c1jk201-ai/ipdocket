"""Asynchronous malware scanning for FileAsset uploads."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from flask import current_app, has_app_context

from app.extensions import db
from app.services.storage.backends import LocalStorageBackend, get_storage_backend
from app.services.uploads.intake_security import (
    UploadSecurityError,
    scan_upload_path,
    virus_scan_enabled,
    virus_scan_mode,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.mime_headers import normalize_uploaded_filename
from app.utils.policy_sql import policy_text as text

SCAN_STATUS_DISABLED = "disabled"
SCAN_STATUS_PENDING = "pending_scan"
SCAN_STATUS_SCANNING = "scanning"
SCAN_STATUS_CLEAN = "clean"
SCAN_STATUS_INFECTED = "infected"
SCAN_STATUS_ERROR = "scan_error"

READ_ALLOWED_SCAN_STATUSES = {SCAN_STATUS_DISABLED, SCAN_STATUS_CLEAN}
READ_BLOCKED_SCAN_STATUSES = {
    SCAN_STATUS_PENDING,
    SCAN_STATUS_SCANNING,
    SCAN_STATUS_INFECTED,
    SCAN_STATUS_ERROR,
}

FILE_ASSET_VIRUS_SCAN_TASK = "file_asset.virus_scan"
FILE_ASSET_VIRUS_SCAN_QUEUE = "deferred"


class FileAssetScanBlocked(PermissionError):
    """Raised when a file asset exists but is not safe to expose to users."""

    def __init__(self, status: str | None):
        normalized = normalize_file_asset_scan_status(status)
        super().__init__(f"file_asset_scan_{normalized}")
        self.status = normalized


def normalize_file_asset_scan_status(status: str | None) -> str:
    value = str(status or "").strip().lower()
    return value or SCAN_STATUS_DISABLED


def file_asset_scan_allows_read(status: str | None) -> bool:
    return normalize_file_asset_scan_status(status) in READ_ALLOWED_SCAN_STATUSES


def assert_file_asset_scan_allows_read(status: str | None) -> None:
    if not file_asset_scan_allows_read(status):
        raise FileAssetScanBlocked(status)


def initial_file_asset_scan_status() -> str:
    if not virus_scan_enabled() or virus_scan_mode() == "disabled":
        return SCAN_STATUS_DISABLED
    if virus_scan_mode() == "sync":
        return SCAN_STATUS_CLEAN
    return SCAN_STATUS_PENDING


def file_asset_scan_status_from_result(result: dict[str, Any] | None) -> str:
    status = str((result or {}).get("status") or "").strip().lower()
    if status == "ok":
        return SCAN_STATUS_CLEAN
    if status == "disabled":
        return SCAN_STATUS_DISABLED
    if status == "rejected":
        return SCAN_STATUS_INFECTED
    if status in {"timeout", "error"}:
        return SCAN_STATUS_ERROR
    return SCAN_STATUS_ERROR


def _scan_error_text(result: dict[str, Any] | None, fallback: str = "") -> str:
    if not result:
        return fallback[:1000]
    parts: list[str] = []
    status = result.get("status")
    if status:
        parts.append(f"status={status}")
    returncode = result.get("returncode")
    if returncode is not None:
        parts.append(f"returncode={returncode}")
    timeout = result.get("timeout_seconds")
    if timeout is not None:
        parts.append(f"timeout_seconds={timeout}")
    error = result.get("error")
    if error:
        parts.append(f"error={error}")
    output = str(result.get("output_tail") or "").strip()
    if output:
        parts.append(output[-800:])
    text_value = "; ".join(parts) or fallback
    return text_value[:1000]


def _set_scan_status(
    file_asset_id: str,
    status: str,
    *,
    error: str | None = None,
    checked_at: datetime | None = None,
) -> None:
    params = {
        "fid": str(file_asset_id),
        "status": normalize_file_asset_scan_status(status),
        "checked_at": checked_at or datetime.utcnow(),
        "error": error,
    }
    db.session.execute(
        text(
            """
            UPDATE file_asset
               SET virus_scan_status = :status,
                   virus_scan_checked_at = :checked_at,
                   virus_scan_error = :error
             WHERE file_asset_id = :fid
            """
        ).execution_options(policy_bypass=True),
        params,
    )


def ensure_file_asset_scan_pending(file_asset_id: str) -> bool:
    """Mark an existing FileAsset for async scanning when the scanner is enabled."""
    fid = (file_asset_id or "").strip()
    if not fid or not virus_scan_enabled() or virus_scan_mode() != "async":
        return False

    row = db.session.execute(
        text(
            """
            SELECT COALESCE(virus_scan_status, :disabled)
              FROM file_asset
             WHERE file_asset_id = :fid
               AND COALESCE(is_deleted, false) = false
            """
        ).execution_options(policy_bypass=True),
        {"fid": fid, "disabled": SCAN_STATUS_DISABLED},
    ).fetchone()
    if not row:
        return False

    status = normalize_file_asset_scan_status(row[0])
    if status == SCAN_STATUS_INFECTED:
        raise UploadSecurityError("virus_scan_rejected")
    if status in {SCAN_STATUS_CLEAN, SCAN_STATUS_PENDING, SCAN_STATUS_SCANNING}:
        return status in {SCAN_STATUS_PENDING, SCAN_STATUS_SCANNING}

    db.session.execute(
        text(
            """
            UPDATE file_asset
               SET virus_scan_status = :pending,
                   virus_scan_checked_at = NULL,
                   virus_scan_error = NULL
             WHERE file_asset_id = :fid
            """
        ).execution_options(policy_bypass=True),
        {"fid": fid, "pending": SCAN_STATUS_PENDING},
    )
    return True


def enqueue_file_asset_scan_after_commit(file_asset_id: str) -> bool:
    """Queue a FileAsset virus scan after the surrounding DB commit succeeds."""
    fid = (file_asset_id or "").strip()
    if not fid or not virus_scan_enabled() or virus_scan_mode() != "async":
        return False
    try:
        from app.ops.durable_queue import build_queue_from_app

        app = current_app._get_current_object() if has_app_context() else None
        if app is None:
            return False
        build_queue_from_app(app).enqueue_after_commit(
            FILE_ASSET_VIRUS_SCAN_TASK,
            {"file_asset_id": fid},
            queue=FILE_ASSET_VIRUS_SCAN_QUEUE,
            max_attempts=12,
            dedupe_key=f"{FILE_ASSET_VIRUS_SCAN_TASK}:{fid}",
            session=db.session,
        )
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_asset_scan.enqueue_file_asset_scan_after_commit",
            log_key="file_asset_scan.enqueue_file_asset_scan_after_commit",
            log_window_seconds=300,
        )
        return False


@contextmanager
def _scan_target_path(
    file_asset_id: str,
    *,
    storage_type: str | None,
    original_name: str | None,
) -> Iterator[Path]:
    from app.services.storage.file_asset_service import get_file_asset_service

    service = get_file_asset_service()
    tmp_path: str | None = None

    storage = str(storage_type or "local").strip().lower() or "local"
    if storage != "s3":
        try:
            local_path = service.get_abs_path(file_asset_id)
            if local_path.exists():
                yield local_path
                return
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="file_asset_scan.scan_target.local_path",
                log_key="file_asset_scan.scan_target.local_path",
                log_window_seconds=300,
            )

    stream: BinaryIO | None = None
    try:
        suffix = Path(original_name or "").suffix or ".bin"
        with tempfile.NamedTemporaryFile(
            prefix="ipm_asset_scan_",
            suffix=suffix,
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
            stream = service.open_stream(file_asset_id)
            shutil.copyfileobj(stream, tmp, length=1024 * 1024)
        yield Path(tmp_path)
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_asset_scan.scan_target.close_stream",
                    log_key="file_asset_scan.scan_target.close_stream",
                    log_window_seconds=300,
                )
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                report_swallowed_exception(
                    exc,
                    context="file_asset_scan.scan_target.cleanup_temp",
                    log_key="file_asset_scan.scan_target.cleanup_temp",
                    log_window_seconds=300,
                )


def _quarantine_rel_path(file_asset_id: str, original_name: str | None) -> str:
    safe_name = normalize_uploaded_filename(original_name, default=f"{file_asset_id}.bin")
    suffix = Path(safe_name).suffix or ".bin"
    date_prefix = datetime.utcnow().strftime("%Y/%m/%d")
    return f"_quarantine/virus/{date_prefix}/{file_asset_id}{suffix}"


def _quarantine_infected_asset(
    *,
    file_asset_id: str,
    file_path: str,
    storage_type: str | None,
    original_name: str | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.services.storage.file_asset_service import get_file_asset_service

    service = get_file_asset_service()
    rel_path = service.normalize_rel_path(file_path)
    new_rel_path = rel_path
    quarantine_error = ""

    try:
        backend = get_storage_backend(storage_type)
        if isinstance(backend, LocalStorageBackend):
            old_abs = service.abs_path(rel_path)
            if old_abs.exists():
                new_rel_path = _quarantine_rel_path(file_asset_id, original_name)
                new_abs = service.abs_path(new_rel_path)
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_abs), str(new_abs))
        else:
            backend.delete(rel_path)
    except Exception as exc:
        quarantine_error = f"quarantine_failed:{type(exc).__name__}:{exc}"[:1000]
        report_swallowed_exception(
            exc,
            context="file_asset_scan.quarantine_infected_asset",
            log_key="file_asset_scan.quarantine_infected_asset",
            log_window_seconds=300,
        )

    error_text = _scan_error_text(result, fallback="virus_scan_rejected")
    if quarantine_error:
        error_text = f"{error_text}; {quarantine_error}"[:1000]

    now = datetime.utcnow()
    db.session.execute(
        text(
            """
            UPDATE file_asset
               SET file_path = :path,
                   virus_scan_status = :status,
                   virus_scan_checked_at = :checked_at,
                   virus_scan_error = :error,
                   quarantined_at = :quarantined_at
             WHERE file_asset_id = :fid
            """
        ).execution_options(policy_bypass=True),
        {
            "fid": file_asset_id,
            "path": new_rel_path,
            "status": SCAN_STATUS_INFECTED,
            "checked_at": now,
            "error": error_text,
            "quarantined_at": now,
        },
    )
    return {"status": SCAN_STATUS_INFECTED, "quarantined_path": new_rel_path}


def run_file_asset_virus_scan(file_asset_id: str) -> dict[str, Any]:
    from app.services.storage.file_asset_scan_queue import run_file_asset_virus_scan as _impl

    return _impl(file_asset_id)
