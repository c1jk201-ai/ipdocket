from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from flask import abort, current_app
from PIL import Image
from sqlalchemy import event
from sqlalchemy.orm import Session as SASession
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from app.extensions import db
from app.services.storage.file_asset_access import FileAssetAccessService
from app.services.storage.file_asset_service import get_file_asset_service
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.upload_io import resolve_first_positive_int

# Default upload size (50MB) - prevents memory exhaustion if config is unset
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_FILE_ASSET_PENDING_PATHS_KEY = "_file_asset_pending_paths"
_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _track_pending_file_asset(file_path: str, abs_path: Path) -> None:
    try:
        pending = db.session.info.get(_FILE_ASSET_PENDING_PATHS_KEY)
        if pending is None:
            pending = []
            db.session.info[_FILE_ASSET_PENDING_PATHS_KEY] = pending
        pending.append({"file_path": file_path, "abs_path": str(abs_path)})
    except Exception as exc:
        # Best-effort: pending-path tracking is advisory cleanup.
        report_swallowed_exception(
            exc,
            context="case.helpers_files._track_pending_file_asset",
            log_key="case.helpers_files._track_pending_file_asset",
            log_window_seconds=300,
        )


def _max_upload_bytes() -> int:
    return resolve_first_positive_int(
        ("FILE_ASSET_MAX_BYTES", "MAX_CONTENT_LENGTH"),
        default=MAX_UPLOAD_BYTES,
    )


def sha256_filestorage(file, chunk_size: int = 1024 * 1024) -> str:
    """Compute SHA256 hash without loading the entire uploaded file into memory."""
    digest = hashlib.sha256()
    file.stream.seek(0)
    for buf in iter(lambda: file.stream.read(chunk_size), b""):
        digest.update(buf)
    file.stream.seek(0)
    return digest.hexdigest()


@event.listens_for(SASession, "after_commit")
def _clear_file_asset_pending_paths(session):
    # SAVEPOINT commits also fire after_commit; keep pending paths until the outer tx finishes.
    try:
        if session.in_transaction():
            return
    except Exception:
        return
    session.info.pop(_FILE_ASSET_PENDING_PATHS_KEY, None)


@event.listens_for(SASession, "after_rollback")
def _cleanup_file_asset_pending_paths(session):
    pending = session.info.pop(_FILE_ASSET_PENDING_PATHS_KEY, None) or []
    if not pending:
        return
    for item in pending:
        rel_path = (item or {}).get("file_path")
        abs_path = (item or {}).get("abs_path")
        if not abs_path:
            continue
        try:
            if rel_path:
                exists = session.execute(
                    text("SELECT 1 FROM file_asset WHERE file_path = :path LIMIT 1"),
                    {"path": rel_path},
                ).scalar()
                if exists:
                    continue
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case.helpers_files._cleanup_file_asset_pending_paths.check_exists",
                log_key="case.helpers_files._cleanup_file_asset_pending_paths.check_exists",
                log_window_seconds=300,
            )
        try:
            path_obj = Path(abs_path)
            if path_obj.exists():
                path_obj.unlink()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case.helpers_files._cleanup_file_asset_pending_paths.unlink",
                log_key="case.helpers_files._cleanup_file_asset_pending_paths.unlink",
                log_window_seconds=300,
            )


def _assert_file_asset_linked_to_matter(*, matter_id: str, file_asset_id: str) -> None:
    if not FileAssetAccessService.is_linked_to_matter(
        matter_id=str(matter_id), file_asset_id=str(file_asset_id)
    ):
        abort(404)


def _load_linked_file_asset(
    *, matter_id: str, file_asset_id: str, strict_link: bool = True
) -> dict | None:
    if not (file_asset_id or "").strip():
        return None
    try:
        # SAVEPOINT : Search Failed outer  "aborted"  
        with db.session.begin_nested():
            from app.models.assets import FileAsset

            fa_obj = (
                FileAsset.query.filter_by(file_asset_id=str(file_asset_id))
                .with_entities(
                    FileAsset.file_asset_id, FileAsset.original_name, FileAsset.mime_type
                )
                .first()
            )

            # fa = (
            #     db.session.execute(
            #         text(
            #             """
            #             SELECT file_asset_id, original_name, mime_type
            #             FROM file_asset
            #             WHERE file_asset_id = :fid
            #             """
            #         ),
            #         {"fid": str(file_asset_id)},
            #     )
            #     .mappings()
            #     .first()
            # )
            if not fa_obj:
                return None
            try:
                _assert_file_asset_linked_to_matter(
                    matter_id=str(matter_id), file_asset_id=str(file_asset_id)
                )
            except HTTPException:
                if strict_link:
                    raise
                current_app.logger.warning(
                    "Ignoring unlinked file asset during optional preview "
                    "(matter_id=%s, file_asset_id=%s)",
                    matter_id,
                    file_asset_id,
                )
                return None
            return {
                "file_asset_id": fa_obj.file_asset_id,
                "original_name": fa_obj.original_name,
                "mime_type": fa_obj.mime_type,
            }
    except HTTPException:
        raise
    except Exception as e:
        current_app.logger.error(f"Error loading linked file asset: {e}")
        return None


def _get_or_create_file_asset_from_upload(*, file, subdir: str) -> str:
    """
    Create or retrieve a file asset from an upload, with streaming and race safety.
    - Streams file to disk while hashing
    - Uses ON CONFLICT for race-safe duplicate handling
    - Enforces MAX_UPLOAD_BYTES limit
    """
    try:
        original_name = (getattr(file, "filename", None) or "").strip() or "file.bin"
        ext = (Path(original_name).suffix or "").lower()
        if ext == ".eml":
            mime = (
                getattr(file, "mimetype", None) or getattr(file, "content_type", None) or ""
            ).strip()
            if not mime:
                try:
                    file.mimetype = "message/rfc822"
                except Exception as exc:
                    # Best-effort: upload object may not support attribute assignment.
                    report_swallowed_exception(
                        exc,
                        context="case.helpers_files._get_or_create_file_asset_from_upload.set_mimetype",
                        log_key="case.helpers_files._get_or_create_file_asset_from_upload.set_mimetype",
                        log_window_seconds=300,
                    )
                try:
                    file.content_type = "message/rfc822"
                except Exception as exc:
                    # Best-effort: upload object may not support attribute assignment.
                    report_swallowed_exception(
                        exc,
                        context="case.helpers_files._get_or_create_file_asset_from_upload.set_content_type",
                        log_key="case.helpers_files._get_or_create_file_asset_from_upload.set_content_type",
                        log_window_seconds=300,
                    )

        file_service = get_file_asset_service()
        staged = file_service.stage_upload(file, subdir=subdir)
        return str(staged.file_asset_id)
    finally:
        try:
            file.stream.seek(0)
        except Exception as exc:
            # Best-effort: streams may be non-seekable.
            report_swallowed_exception(
                exc,
                context="case.helpers_files._get_or_create_file_asset_from_upload.rewind",
                log_key="case.helpers_files._get_or_create_file_asset_from_upload.rewind",
                log_window_seconds=300,
            )


def _is_allowed_image_upload(file) -> bool:
    filename = (getattr(file, "filename", None) or "").strip()
    if not filename:
        return False
    safe_name = secure_filename(filename)
    ext = (Path(safe_name).suffix or "").lower()
    mime = (getattr(file, "mimetype", None) or "").strip().lower()
    if ext not in _ALLOWED_IMAGE_EXTS and not mime.startswith("image/"):
        return False
    try:
        file.stream.seek(0)
        with Image.open(file.stream) as img:
            img.verify()
        return True
    except Exception:
        return False
    finally:
        try:
            file.stream.seek(0)
        except Exception as exc:
            # Best-effort: streams may be non-seekable.
            report_swallowed_exception(
                exc,
                context="case.helpers_files._is_allowed_image_upload.rewind",
                log_key="case.helpers_files._is_allowed_image_upload.rewind",
                log_window_seconds=300,
            )


def _attach_image_file_asset(*, matter_id: str, file, data: dict) -> None:
    if not file or not (getattr(file, "filename", None) or "").strip():
        return
    if not _is_allowed_image_upload(file):
        raise ValueError("/Image Only image files can be uploaded.")
    fid = _get_or_create_file_asset_from_upload(file=file, subdir=f"matter/{matter_id}/images")
    db.session.execute(
        text(
            """
            INSERT INTO matter_file_asset(matter_file_id, matter_id, file_asset_id, role, description)
            SELECT :mfa_id, :mid, :fid, :role, :desc
            WHERE NOT EXISTS (
                SELECT 1
                FROM matter_file_asset
                WHERE matter_id = :mid
                  AND file_asset_id = :fid
                  AND role IS NOT DISTINCT FROM :role
            )
            """
        ).execution_options(policy_bypass=True),
        {
            "mfa_id": uuid.uuid4().hex,
            "mid": str(matter_id),
            "fid": fid,
            "role": "image",
            "desc": "",
        },
    )
    data["image"] = fid
