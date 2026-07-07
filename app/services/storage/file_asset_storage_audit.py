"""DB-aware audit helpers for local FileAsset storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.services.storage.file_asset_service import FileAssetService
from app.utils.policy_sql import policy_text as text
from app.utils.runtime_config import runtime_upload_folder

_FILE_ASSET_LINK_TABLES = (
    "matter_file_asset",
    "communication_file_asset",
    "office_action_file_asset",
    "matter_memo_file_asset",
)

_PATH_REFERENCE_COLUMNS = (
    ("email_message", "raw_eml_path"),
    ("email_attachment", "storage_path"),
    ("email_attachment", "extracted_text_path"),
    ("email_attachment", "ocr_text_path"),
)


def _sample_append(samples: list[dict[str, Any]], item: dict[str, Any], limit: int) -> None:
    if len(samples) < limit:
        samples.append(item)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _configured_upload_root(upload_root: str | Path | None) -> Path:
    if upload_root is not None:
        return Path(upload_root).resolve()
    try:
        configured = current_app.config.get("UPLOAD_FOLDER") or runtime_upload_folder()
    except Exception:
        configured = runtime_upload_folder()
    return Path(configured).resolve()


def _inspector_has_table(table_name: str) -> bool:
    try:
        return bool(inspect(db.engine).has_table(table_name))
    except Exception:
        return False


def _inspector_has_column(table_name: str, column_name: str) -> bool:
    try:
        cols = inspect(db.engine).get_columns(table_name)
        return any(str(col.get("name") or "") == column_name for col in cols)
    except Exception:
        return False


def _normalize_path(
    service: FileAssetService,
    raw_path: str,
    *,
    upload_root: Path | None = None,
) -> str:
    path_text = (raw_path or "").strip()
    if upload_root is not None:
        try:
            maybe_abs = Path(path_text)
            if maybe_abs.is_absolute():
                resolved = maybe_abs.resolve(strict=False)
                if resolved.is_relative_to(upload_root):
                    path_text = str(resolved.relative_to(upload_root))
        except Exception as exc:
            _ = exc
    return service.normalize_rel_path(path_text)


def _fetch_local_file_assets() -> list[dict[str, Any]]:
    rows = (
        db.session.execute(
            text(
                """
                SELECT file_asset_id, storage_type, file_path, original_name, sha256,
                       byte_size, mime_type, created_at
                FROM file_asset
                WHERE file_path IS NOT NULL
                  AND TRIM(file_path) <> ''
                  AND COALESCE(LOWER(storage_type), 'local') <> 's3'
                """
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _collect_link_reference_ids(*, active_only: bool) -> set[str]:
    referenced: set[str] = set()
    for table_name in _FILE_ASSET_LINK_TABLES:
        if not _inspector_has_table(table_name):
            continue
        where = "file_asset_id IS NOT NULL AND TRIM(file_asset_id) <> ''"
        if active_only and _inspector_has_column(table_name, "is_deleted"):
            where += " AND (is_deleted IS NULL OR is_deleted = false)"
        try:
            rows = db.session.execute(
                text(f"SELECT DISTINCT file_asset_id FROM {table_name} WHERE {where}")
            ).scalars()
            referenced.update(str(row) for row in rows if row)
        except SQLAlchemyError:
            db.session.rollback()
            continue
    return referenced


def _collect_path_reference_ids(
    *,
    service: FileAssetService,
    upload_root: Path,
    path_to_file_asset_ids: dict[str, set[str]],
) -> set[str]:
    if not path_to_file_asset_ids:
        return set()

    referenced: set[str] = set()
    for table_name, column_name in _PATH_REFERENCE_COLUMNS:
        if not _inspector_has_table(table_name) or not _inspector_has_column(
            table_name, column_name
        ):
            continue
        try:
            rows = db.session.execute(
                text(
                    f"""
                    SELECT DISTINCT {column_name}
                    FROM {table_name}
                    WHERE {column_name} IS NOT NULL
                      AND TRIM({column_name}) <> ''
                    """
                )
            ).scalars()
        except SQLAlchemyError:
            db.session.rollback()
            continue

        for raw_path in rows:
            try:
                rel_path = _normalize_path(service, str(raw_path), upload_root=upload_root)
            except Exception:
                continue
            referenced.update(path_to_file_asset_ids.get(rel_path, set()))
    return referenced


def audit_file_asset_storage(
    *,
    upload_root: str | Path | None = None,
    sample_limit: int = 25,
    scan_disk: bool = True,
) -> dict[str, Any]:
    """
    Compare local upload storage with FileAsset metadata.

    The report is read-only. It deliberately labels files that are only present
    on disk as "untracked_by_file_asset" instead of deleting them, because this
    deployment still has legacy/non-FileAsset paths under UPLOAD_FOLDER.
    """
    try:
        sample_limit_i = max(0, int(sample_limit))
    except Exception:
        sample_limit_i = 25

    root = _configured_upload_root(upload_root)
    service = FileAssetService(upload_root=root)
    raw_assets = _fetch_local_file_assets()

    valid_assets: list[dict[str, Any]] = []
    invalid_db_paths: dict[str, Any] = {
        "count": 0,
        "declared_bytes": 0,
        "sample": [],
    }
    path_to_file_asset_ids: dict[str, set[str]] = {}
    db_declared_bytes = 0

    for row in raw_assets:
        byte_size = _as_int(row.get("byte_size"))
        db_declared_bytes += byte_size
        try:
            rel_path = _normalize_path(
                service,
                str(row.get("file_path") or ""),
                upload_root=root,
            )
        except Exception as exc:
            invalid_db_paths["count"] += 1
            invalid_db_paths["declared_bytes"] += byte_size
            _sample_append(
                invalid_db_paths["sample"],
                {
                    "file_asset_id": str(row.get("file_asset_id") or ""),
                    "file_path": str(row.get("file_path") or ""),
                    "byte_size": byte_size,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sample_limit_i,
            )
            continue

        normalized = dict(row)
        normalized["rel_path"] = rel_path
        normalized["byte_size"] = byte_size
        valid_assets.append(normalized)
        path_to_file_asset_ids.setdefault(rel_path, set()).add(str(row.get("file_asset_id") or ""))

    any_link_ref_ids = _collect_link_reference_ids(active_only=False)
    active_link_ref_ids = _collect_link_reference_ids(active_only=True)
    path_ref_ids = _collect_path_reference_ids(
        service=service,
        upload_root=root,
        path_to_file_asset_ids=path_to_file_asset_ids,
    )
    effective_ref_ids = any_link_ref_ids | path_ref_ids

    missing_db_files: dict[str, Any] = {
        "count": 0,
        "declared_bytes": 0,
        "sample": [],
    }
    existing_db_files = {"count": 0, "bytes": 0}
    orphan_file_assets: dict[str, Any] = {
        "count": 0,
        "declared_bytes": 0,
        "existing_files": 0,
        "existing_bytes": 0,
        "sample": [],
    }

    known_db_paths = set(path_to_file_asset_ids)
    for row in valid_assets:
        fid = str(row.get("file_asset_id") or "")
        rel_path = str(row.get("rel_path") or "")
        byte_size = _as_int(row.get("byte_size"))
        abs_path = root / rel_path
        exists = False
        disk_size = 0
        try:
            if abs_path.is_file():
                exists = True
                disk_size = int(abs_path.stat().st_size)
        except Exception:
            exists = False
            disk_size = 0

        if exists:
            existing_db_files["count"] += 1
            existing_db_files["bytes"] += disk_size
        else:
            missing_db_files["count"] += 1
            missing_db_files["declared_bytes"] += byte_size
            _sample_append(
                missing_db_files["sample"],
                {
                    "file_asset_id": fid,
                    "path": rel_path,
                    "byte_size": byte_size,
                },
                sample_limit_i,
            )

        if fid and fid not in effective_ref_ids:
            orphan_file_assets["count"] += 1
            orphan_file_assets["declared_bytes"] += byte_size
            if exists:
                orphan_file_assets["existing_files"] += 1
                orphan_file_assets["existing_bytes"] += disk_size
            _sample_append(
                orphan_file_assets["sample"],
                {
                    "file_asset_id": fid,
                    "path": rel_path,
                    "byte_size": byte_size,
                    "exists": exists,
                },
                sample_limit_i,
            )

    disk_files: dict[str, Any] = {
        "scanned": 0,
        "bytes": 0,
        "errors": 0,
        "sample_errors": [],
    }
    untracked_disk_files: dict[str, Any] = {
        "count": 0,
        "bytes": 0,
        "sample": [],
        "label": "untracked_by_file_asset",
    }

    if scan_disk and root.exists():
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                rel_path = path.relative_to(root).as_posix()
                size = int(stat.st_size)
                disk_files["scanned"] += 1
                disk_files["bytes"] += size
                if rel_path not in known_db_paths:
                    untracked_disk_files["count"] += 1
                    untracked_disk_files["bytes"] += size
                    _sample_append(
                        untracked_disk_files["sample"],
                        {"path": rel_path, "bytes": size},
                        sample_limit_i,
                    )
            except Exception as exc:
                disk_files["errors"] += 1
                _sample_append(
                    disk_files["sample_errors"],
                    {"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
                    sample_limit_i,
                )

    return {
        "upload_root": str(root),
        "scan_disk": bool(scan_disk),
        "db_file_assets": {
            "count": len(raw_assets),
            "valid_local_paths": len(valid_assets),
            "declared_bytes": db_declared_bytes,
        },
        "referenced_file_assets": {
            "by_any_link": len(any_link_ref_ids),
            "by_active_link": len(active_link_ref_ids),
            "by_email_path": len(path_ref_ids),
            "effective": len(effective_ref_ids),
        },
        "existing_db_files": existing_db_files,
        "missing_db_files": missing_db_files,
        "invalid_db_paths": invalid_db_paths,
        "orphan_file_assets": orphan_file_assets,
        "disk_files": disk_files,
        "untracked_disk_files": untracked_disk_files,
        "notes": [
            "Report is read-only.",
            "untracked_by_file_asset can include legacy/non-FileAsset files; review samples before deletion.",
        ],
    }
