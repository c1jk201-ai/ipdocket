from __future__ import annotations

from datetime import datetime
from typing import Any

from app.extensions import db
from app.services.storage.file_asset_scan_service import (
    SCAN_STATUS_CLEAN,
    SCAN_STATUS_DISABLED,
    SCAN_STATUS_ERROR,
    SCAN_STATUS_INFECTED,
    SCAN_STATUS_SCANNING,
    UploadSecurityError,
    _quarantine_infected_asset,
    _scan_error_text,
    _scan_target_path,
    _set_scan_status,
    file_asset_scan_status_from_result,
    normalize_file_asset_scan_status,
    scan_upload_path,
    virus_scan_enabled,
    virus_scan_mode,
)
from app.utils.policy_sql import policy_text as text


def _set_scan_status_committing(
    file_asset_id: str,
    status: str,
    *,
    error: str | None = None,
    checked_at: datetime | None = None,
) -> None:
    _set_scan_status(file_asset_id, status, error=error, checked_at=checked_at)
    db.session.commit()


def _quarantine_infected_asset_committing(
    *,
    file_asset_id: str,
    file_path: str,
    storage_type: str | None,
    original_name: str | None,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    scan_result = _quarantine_infected_asset(
        file_asset_id=file_asset_id,
        file_path=file_path,
        storage_type=storage_type,
        original_name=original_name,
        result=result,
    )
    db.session.commit()
    return scan_result


def run_file_asset_virus_scan(file_asset_id: str) -> dict[str, Any]:
    """Durable queue handler for async FileAsset malware scanning."""
    fid = (file_asset_id or "").strip()
    if not fid:
        return {"status": "skipped", "reason": "missing_file_asset_id"}

    row = db.session.execute(
        text(
            """
            SELECT file_asset_id,
                   file_path,
                   storage_type,
                   original_name,
                   COALESCE(virus_scan_status, :disabled)
              FROM file_asset
             WHERE file_asset_id = :fid
               AND COALESCE(is_deleted, false) = false
            """
        ).execution_options(policy_bypass=True),
        {"fid": fid, "disabled": SCAN_STATUS_DISABLED},
    ).fetchone()
    if not row:
        return {"status": "skipped", "reason": "not_found"}

    file_path = str(row[1] or "")
    storage_type = row[2]
    original_name = row[3]
    current_status = normalize_file_asset_scan_status(row[4])

    if current_status == SCAN_STATUS_INFECTED:
        return {"status": SCAN_STATUS_INFECTED}

    if not virus_scan_enabled() or virus_scan_mode() == "disabled":
        _set_scan_status_committing(fid, SCAN_STATUS_DISABLED)
        return {"status": SCAN_STATUS_DISABLED}

    _set_scan_status_committing(fid, SCAN_STATUS_SCANNING, checked_at=None)

    result: dict[str, Any] | None = None
    try:
        with _scan_target_path(
            fid,
            storage_type=storage_type,
            original_name=original_name,
        ) as path:
            result = scan_upload_path(path, filename=original_name)
    except UploadSecurityError as exc:
        if str(exc) == "virus_scan_rejected":
            return _quarantine_infected_asset_committing(
                file_asset_id=fid,
                file_path=file_path,
                storage_type=storage_type,
                original_name=original_name,
                result={"status": "rejected", "error": str(exc)},
            )
        _set_scan_status_committing(
            fid,
            SCAN_STATUS_ERROR,
            error=f"{type(exc).__name__}:{exc}"[:1000],
        )
        raise
    except Exception as exc:
        _set_scan_status_committing(
            fid,
            SCAN_STATUS_ERROR,
            error=f"{type(exc).__name__}:{exc}"[:1000],
        )
        raise

    raw_status = str((result or {}).get("status") or "").strip().lower()
    if (result or {}).get("fail_open") and raw_status in {"timeout", "error"}:
        error_text = f"fail_open:{_scan_error_text(result, fallback='virus_scan_failed')}"
        _set_scan_status_committing(fid, SCAN_STATUS_CLEAN, error=error_text[:1000])
        return {"status": SCAN_STATUS_CLEAN, "fail_open": True, "scanner_status": raw_status}

    status = file_asset_scan_status_from_result(result)
    if status in {SCAN_STATUS_CLEAN, SCAN_STATUS_DISABLED}:
        _set_scan_status_committing(fid, status)
        return {"status": status}

    if status == SCAN_STATUS_INFECTED:
        return _quarantine_infected_asset_committing(
            file_asset_id=fid,
            file_path=file_path,
            storage_type=storage_type,
            original_name=original_name,
            result=result,
        )

    error_text = _scan_error_text(result, fallback="virus_scan_failed")
    _set_scan_status_committing(fid, SCAN_STATUS_ERROR, error=error_text)
    raise RuntimeError(error_text or "virus_scan_failed")
