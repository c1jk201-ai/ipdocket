"""Unified FileAsset storage service with streaming and race-safe operations."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from sqlalchemy import bindparam, column, event, select, table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as SASession
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.services.storage.backends import LocalStorageBackend, StorageBackend, get_storage_backend
from app.services.storage.file_asset_scan_service import (
    SCAN_STATUS_CLEAN,
    SCAN_STATUS_DISABLED,
    SCAN_STATUS_INFECTED,
    SCAN_STATUS_PENDING,
    enqueue_file_asset_scan_after_commit,
    ensure_file_asset_scan_pending,
    file_asset_scan_status_from_result,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.mime_headers import normalize_uploaded_filename
from app.utils.policy_sql import policy_text as text
from app.utils.runtime_config import runtime_config_str, runtime_storage_type, runtime_upload_folder
from app.utils.upload_io import resolve_first_positive_int

_FILE_ASSET_PENDING_PATHS_KEY = "_file_asset_pending_paths"
_FILE_ASSET_PENDING_UPLOADS_KEY = "_file_asset_pending_uploads"


def _track_pending_file_asset_upload(
    *,
    file_asset_id: str,
    storage_type: str | None,
    rel_path: str,
) -> None:
    """
    Track newly-staged physical uploads so we can clean them up on DB rollback.

    Note: FileAssetService.stage_upload/stage_bytes persist physical files before the
    surrounding transaction is committed, so a later rollback can otherwise leave
    orphaned objects in storage.
    """
    try:
        pending = db.session.info.get(_FILE_ASSET_PENDING_UPLOADS_KEY)
        if pending is None:
            pending = []
            db.session.info[_FILE_ASSET_PENDING_UPLOADS_KEY] = pending
        pending.append(
            {
                "file_asset_id": str(file_asset_id),
                "storage_type": (storage_type or "").strip() or None,
                "rel_path": str(rel_path),
            }
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_asset_service._track_pending_file_asset_upload",
            log_key="file_asset_service._track_pending_file_asset_upload",
            log_window_seconds=300,
        )


@event.listens_for(SASession, "after_commit")
def _clear_pending_file_asset_uploads(session: SASession) -> None:
    # NOTE: begin_nested() (SAVEPOINT) commits can also trigger Session after_commit.
    # Only clear tracking when the outermost transaction has finished.
    try:
        if session.in_transaction():
            return
    except Exception:
        return
    session.info.pop(_FILE_ASSET_PENDING_UPLOADS_KEY, None)


@event.listens_for(SASession, "after_rollback")
def _cleanup_pending_file_asset_uploads(session: SASession) -> None:
    pending = session.info.pop(_FILE_ASSET_PENDING_UPLOADS_KEY, None) or []
    if not pending:
        return

    for item in pending:
        file_asset_id = str((item or {}).get("file_asset_id") or "").strip()
        if not file_asset_id:
            continue
        rel_path = str((item or {}).get("rel_path") or "").strip()
        if not rel_path:
            continue
        storage_type = (item or {}).get("storage_type")

        try:
            with db.engine.connect() as conn:
                exists = bool(
                    conn.execute(
                        _policy_sql("SELECT 1 FROM file_asset WHERE file_path = :path LIMIT 1"),
                        {"path": rel_path},
                    ).scalar()
                )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="file_asset_service._cleanup_pending_file_asset_uploads.check_exists",
                log_key="file_asset_service._cleanup_pending_file_asset_uploads.check_exists",
                log_window_seconds=300,
            )
            continue

        if exists:
            continue

        try:
            backend = get_storage_backend(storage_type)
            backend.delete(rel_path)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="file_asset_service._cleanup_pending_file_asset_uploads.delete_physical",
                log_key="file_asset_service._cleanup_pending_file_asset_uploads.delete_physical",
                log_window_seconds=300,
            )


def _policy_sql(sql: str):
    return text(sql).execution_options(policy_bypass=True)


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.min.time())
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = None
        for candidate in (s, s.split(".")[0]):
            try:
                dt = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds configured size limits."""


def _format_max_bytes(max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    if max_bytes < 1024:
        return f"{max_bytes}B"
    mb = max_bytes / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.0f}MB" if mb.is_integer() else f"{mb:.1f}MB"
    kb = max_bytes / 1024
    return f"{kb:.0f}KB"


def _raise_upload_too_large(max_bytes: int | None) -> None:
    if max_bytes:
        raise UploadTooLargeError(f"Upload too large (max {_format_max_bytes(max_bytes)}).")
    raise UploadTooLargeError("Upload too large.")


class _CountingStream:
    """Wrap a stream to count bytes read and enforce max size if provided."""

    def __init__(self, stream: BinaryIO, *, max_bytes: int | None = None) -> None:
        self._stream = stream
        self._max_bytes = max_bytes or 0
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            if self._max_bytes and self.bytes_read > self._max_bytes:
                _raise_upload_too_large(self._max_bytes)
        return chunk

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


@dataclass
class StagedFile:
    """Result of staging an upload to FileAsset storage."""

    file_asset_id: str
    sha256: str
    original_name: str
    mime_type: str | None
    byte_size: int
    rel_path: str
    is_new: bool = True


@dataclass
class StoredUpload:
    """Result of storing an upload to a specific path (non-FileAsset)."""

    rel_path: str
    abs_path: Path | None  # None if S3
    sha256: str
    byte_size: int


class FileAssetService:
    """Service for managing FileAsset storage with consistent paths and streaming."""

    CHUNK_SIZE = 1024 * 1024  # 1MB chunks for streaming

    def __init__(self, upload_root: str | Path | None = None):
        # Only used for local path resolution if needed
        self._explicit_upload_root = upload_root is not None
        self._upload_root: Path | None = Path(upload_root).resolve() if upload_root else None
        self.default_backend = self._backend()

    @property
    def upload_root(self) -> Path:
        """Get resolved upload root directory from config."""
        if self._upload_root is None:
            folder = runtime_upload_folder()
            self._upload_root = Path(folder).resolve()
        return self._upload_root

    @property
    def staging_root(self) -> Path:
        configured = runtime_config_str("FILE_ASSET_STAGING_ROOT", "")
        if configured:
            root = Path(configured).resolve()
        else:
            root = self.upload_root / "_staging"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _storage_type(self) -> str:
        return runtime_storage_type()

    def _backend(self, storage_type: str | None = None) -> StorageBackend:
        return get_storage_backend(
            storage_type or self._storage_type(), upload_root=self._upload_root
        )

    def _max_upload_bytes(self) -> int:
        return resolve_first_positive_int(
            ("FILE_ASSET_MAX_BYTES", "UPLOAD_MAX_BYTES", "MAX_CONTENT_LENGTH"),
            default=0,
        )

    def normalize_rel_path(self, stored_path: str) -> str:
        """
        Normalize a DB-stored file path to a standard relative path.
        """
        raw = (stored_path or "").strip()
        if not raw:
            raise ValueError("Empty file path")

        # For S3/Generic compatibility, we just clean up slashes usually.
        # But we must respect legacy check logic (../ prevention) for local files.
        # We can reuse the logic but be careful about assumptions.

        # Simple normalization: forward slashes, no leading ./ or /
        rel = raw.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        legacy_prefix = "data/uploads/"
        if rel.lower().startswith(legacy_prefix):
            rel = rel[len(legacy_prefix) :]
        rel = rel.lstrip("/")

        parts: list[str] = []
        for part in rel.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError(f"Path traversal detected: {stored_path}")
            parts.append(part)

        if not parts:
            raise ValueError("Empty file path")

        return "/".join(parts)

    def abs_path(self, stored_path: str) -> Path:
        """
        Resolve a DB-stored file path to an absolute path (LOCAL ONLY).
        Throws error if current storage is not local, or if path is invalid.
        Usage of this method implies dependence on local filesystem.
        """
        # We allow this helper for legacy code that already knows the row is local.
        # New storage-agnostic code should use open_stream/materialize_to_temp.
        rel_path = self.normalize_rel_path(stored_path)
        full = (self.upload_root / rel_path).resolve()
        if not full.is_relative_to(self.upload_root):
            raise ValueError(f"Path traversal detected: {stored_path}")
        return full

    def stage_upload(self, file: FileStorage, *, subdir: str) -> StagedFile:
        """Stage an uploaded file."""
        original_name = normalize_uploaded_filename(getattr(file, "filename", None))
        ext = Path(original_name).suffix.lower() or ".bin"
        mime_type = getattr(file, "mimetype", None) or getattr(file, "content_type", None)

        # 1. Stream to local temp to compute SHA256 & size
        # (S3 requires size for efficient uploads usually, and we need SHA256 for dedup)
        sha256, byte_size, temp_abs = self._stream_to_temp(file.stream, ext, subdir)

        # 2. Determine final path
        # Use storage-agnostic relative path
        desired_rel_path = f"{subdir}/{sha256}{ext}".replace("\\", "/").strip("/")

        try:
            from app.services.uploads.intake_security import (
                UploadSecurityError,
                scan_upload_path,
                virus_scan_enabled,
                virus_scan_mode,
            )

            scan_enabled = virus_scan_enabled() and virus_scan_mode() != "disabled"
            scan_async = scan_enabled and virus_scan_mode() == "async"
            virus_scan_status = SCAN_STATUS_DISABLED
            if scan_enabled and not scan_async:
                scan_result = scan_upload_path(temp_abs, filename=original_name)
                virus_scan_status = file_asset_scan_status_from_result(scan_result)
                if virus_scan_status == SCAN_STATUS_INFECTED:
                    raise UploadSecurityError("virus_scan_rejected")
            elif scan_async:
                virus_scan_status = SCAN_STATUS_PENDING

            # 3. Upsert DB (Atomic)
            storage_type = self._storage_type()
            file_asset_id, is_new, stored_path, _existing_scan_status = self._upsert_file_asset(
                sha256=sha256,
                rel_path=desired_rel_path,
                original_name=original_name,
                byte_size=byte_size,
                mime_type=mime_type,
                ext=ext,
                storage_type=storage_type,
                virus_scan_status=virus_scan_status,
            )
            enqueue_scan = False
            if scan_async:
                enqueue_scan = ensure_file_asset_scan_pending(file_asset_id)

            # 4. If new, save to backend
            final_rel_path = self.normalize_rel_path(stored_path)
            backend = self._backend(storage_type)

            should_save = is_new
            if not should_save:
                try:
                    should_save = not backend.exists(final_rel_path)
                except Exception:
                    should_save = False

            if should_save:
                # Read from temp and save to backend
                with open(temp_abs, "rb") as f:
                    backend.save(f, final_rel_path, size=byte_size)
                _track_pending_file_asset_upload(
                    file_asset_id=file_asset_id,
                    storage_type=storage_type,
                    rel_path=final_rel_path,
                )

                # Cleanup temp
                try:
                    temp_abs.unlink()
                except OSError:
                    pass
            else:
                # Just cleanup temp
                try:
                    temp_abs.unlink()
                except OSError:
                    pass

            if enqueue_scan:
                enqueue_file_asset_scan_after_commit(file_asset_id)

        except Exception:
            # Cleanup temp on error
            try:
                if temp_abs.exists():
                    temp_abs.unlink()
            except Exception as cleanup_exc:
                report_swallowed_exception(
                    cleanup_exc,
                    context="file_asset_service.stage_upload.cleanup_temp",
                    log_key="file_asset_service.stage_upload.cleanup_temp",
                    log_window_seconds=300,
                )
            raise

        return StagedFile(
            file_asset_id=file_asset_id,
            sha256=sha256,
            original_name=original_name,
            mime_type=mime_type,
            byte_size=byte_size,
            rel_path=final_rel_path,
            is_new=is_new,
        )

    def stage_bytes(
        self, data: bytes, *, filename: str, subdir: str, mime_type: str | None = None
    ) -> StagedFile:
        """Stage raw bytes."""
        max_bytes = self._max_upload_bytes()
        if max_bytes and len(data) > max_bytes:
            _raise_upload_too_large(max_bytes)

        filename = normalize_uploaded_filename(filename)
        # No need for temp file for bytes if memory allows, but uniformity helps.
        # We can implement directly.
        sha256 = hashlib.sha256(data).hexdigest()
        ext = Path(filename).suffix.lower() or ".bin"
        desired_rel_path = f"{subdir}/{sha256}{ext}".replace("\\", "/").strip("/")

        from app.services.uploads.intake_security import (
            UploadSecurityError,
            scan_upload_bytes,
            virus_scan_enabled,
            virus_scan_mode,
        )

        scan_enabled = virus_scan_enabled() and virus_scan_mode() != "disabled"
        scan_async = scan_enabled and virus_scan_mode() == "async"
        virus_scan_status = SCAN_STATUS_DISABLED
        if scan_enabled and not scan_async:
            scan_result = scan_upload_bytes(data, filename=filename)
            virus_scan_status = file_asset_scan_status_from_result(scan_result)
            if virus_scan_status == SCAN_STATUS_INFECTED:
                raise UploadSecurityError("virus_scan_rejected")
        elif scan_async:
            virus_scan_status = SCAN_STATUS_PENDING

        storage_type = self._storage_type()
        file_asset_id, is_new, stored_path, _existing_scan_status = self._upsert_file_asset(
            sha256=sha256,
            rel_path=desired_rel_path,
            original_name=filename,
            byte_size=len(data),
            mime_type=mime_type,
            ext=ext,
            storage_type=storage_type,
            virus_scan_status=virus_scan_status,
        )
        enqueue_scan = False
        if scan_async:
            enqueue_scan = ensure_file_asset_scan_pending(file_asset_id)

        final_rel_path = self.normalize_rel_path(stored_path)

        should_save = is_new
        backend = self._backend(storage_type)
        if not should_save:
            try:
                should_save = not backend.exists(final_rel_path)
            except Exception:
                should_save = False

        if should_save:
            import io

            backend.save(io.BytesIO(data), final_rel_path, size=len(data))
            _track_pending_file_asset_upload(
                file_asset_id=file_asset_id,
                storage_type=storage_type,
                rel_path=final_rel_path,
            )

        if enqueue_scan:
            enqueue_file_asset_scan_after_commit(file_asset_id)

        return StagedFile(
            file_asset_id=file_asset_id,
            sha256=sha256,
            original_name=filename,
            mime_type=mime_type,
            byte_size=len(data),
            rel_path=final_rel_path,
            is_new=is_new,
        )

    def stage_path(
        self,
        path: str | Path,
        *,
        filename: str,
        subdir: str,
        mime_type: str | None = None,
    ) -> StagedFile:
        """Stage a local file."""
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {path}")

        # Check size override
        max_bytes = self._max_upload_bytes()
        if max_bytes and src.stat().st_size > max_bytes:
            _raise_upload_too_large(max_bytes)

        filename = normalize_uploaded_filename(filename)
        ext = Path(filename).suffix.lower() or src.suffix.lower() or ".bin"

        with src.open("rb") as f:
            return self.stage_upload(
                file=FileStorage(f, filename=filename, content_type=mime_type), subdir=subdir
            )

    def open_stream(self, file_asset_id: str) -> BinaryIO:
        """Open a file asset for reading."""
        row = db.session.execute(
            _policy_sql("SELECT file_path, storage_type FROM file_asset WHERE file_asset_id = :id"),
            {"id": file_asset_id},
        ).fetchone()

        if not row:
            raise FileNotFoundError(f"FileAsset not found: {file_asset_id}")

        file_path, storage_type = row
        backend = self._backend(storage_type)
        return backend.open(self.normalize_rel_path(file_path))

    def read_all(self, file_asset_id: str) -> bytes:
        """Read entire file content."""
        stream = self.open_stream(file_asset_id)
        try:
            return stream.read()
        finally:
            stream.close()

    def get_abs_path(self, file_asset_id: str) -> Path:
        """
        Get absolute path (Local only).
        Deprecated: Use open_stream/materialize_to_temp where possible.
        """
        row = db.session.execute(
            _policy_sql("SELECT file_path, storage_type FROM file_asset WHERE file_asset_id = :id"),
            {"id": file_asset_id},
        ).fetchone()

        if not row:
            raise FileNotFoundError(f"FileAsset not found: {file_asset_id}")

        file_path, storage_type = row
        if str(storage_type or "local").strip().lower() == "s3":
            raise RuntimeError(
                "FileAsset is not stored locally; use open_stream/materialize_to_temp"
            )
        return self.abs_path(file_path)

    @contextmanager
    def materialize_to_temp(self, file_asset_id: str) -> Iterator[Path]:
        """
        Yield a local path for code that still requires filesystem APIs.

        Local assets yield their real path. Object-backed assets are streamed into
        the local staging directory and removed when the context exits.
        """
        row = db.session.execute(
            _policy_sql(
                "SELECT file_path, storage_type, original_name FROM file_asset WHERE file_asset_id = :id"
            ),
            {"id": file_asset_id},
        ).fetchone()

        if not row:
            raise FileNotFoundError(f"FileAsset not found: {file_asset_id}")

        file_path, storage_type, original_name = row
        if str(storage_type or "local").strip().lower() != "s3":
            yield self.abs_path(file_path)
            return

        suffix = Path(str(original_name or file_path or "")).suffix or ".bin"
        tmp_path: Path | None = None
        stream = self.open_stream(file_asset_id)
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"file_asset_{file_asset_id}_",
                suffix=suffix,
                dir=self.staging_root,
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                shutil.copyfileobj(stream, tmp)
            yield tmp_path
        finally:
            try:
                stream.close()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_asset_service.materialize_to_temp.close_stream",
                    log_key="file_asset_service.materialize_to_temp.close_stream",
                    log_window_seconds=300,
                )
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="file_asset_service.materialize_to_temp.cleanup",
                        log_key="file_asset_service.materialize_to_temp.cleanup",
                        log_window_seconds=300,
                    )

    def get_rel_path(self, file_asset_id: str) -> str:
        """Get relative path for a file asset."""
        row = db.session.execute(
            _policy_sql("SELECT file_path FROM file_asset WHERE file_asset_id = :id"),
            {"id": file_asset_id},
        ).fetchone()

        if not row:
            raise FileNotFoundError(f"FileAsset not found: {file_asset_id}")

        return row[0]

    def delete_physical_file(
        self, stored_path: str, *, prune_empty: bool = True, storage_type: str | None = None
    ) -> bool:
        """Delete from storage."""
        # Note: prune_empty only applies to local backend
        backend = self._backend(storage_type)
        rel_path = self.normalize_rel_path(stored_path)
        return backend.delete(rel_path)

    def purge_if_orphan(
        self,
        file_asset_id: str,
        *,
        min_age_days: int = 0,
        dry_run: bool = False,
        use_separate_session: bool = True,
    ) -> dict[str, Any]:
        """Purge orphan FileAsset."""
        file_asset_id = (file_asset_id or "").strip()
        if not file_asset_id:
            return {"deleted": False, "reason": "empty_id"}

        session = SASession(db.engine) if use_separate_session else db.session
        owns_session = use_separate_session
        try:
            row = session.execute(
                _policy_sql(
                    """
                    SELECT storage_type, file_path, original_name, sha256, byte_size, mime_type, created_at
                    FROM file_asset
                    WHERE file_asset_id = :id
                    """
                ),
                {"id": file_asset_id},
            ).fetchone()
            if not row:
                return {"deleted": False, "reason": "not_found"}

            (
                storage_type,
                file_path,
                _original_name,
                _sha256,
                byte_size,
                _mime_type,
                created_at,
            ) = row

            if self._is_referenced(file_asset_id, file_path=str(file_path), session=session):
                return {"deleted": False, "reason": "still_referenced"}

            backend = self._backend(storage_type)
            rel_path = self.normalize_rel_path(file_path)

            # Check existence/size if possible
            exists = backend.exists(rel_path)

            min_age_days_i = int(min_age_days or 0)
            if min_age_days_i > 0:
                created_dt = _coerce_utc_datetime(created_at)
                if created_dt is None:
                    return {"deleted": False, "reason": "age_unknown"}
                if datetime.utcnow() - created_dt < timedelta(days=min_age_days_i):
                    return {"deleted": False, "reason": "too_young"}

            if dry_run:
                return {
                    "deleted": True,
                    "db_deleted": True,
                    "file_deleted": exists,
                    "reason": "dry_run",
                }

            file_deleted = False
            if exists:
                try:
                    file_deleted = bool(backend.delete(rel_path))
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="file_asset_service.purge_if_orphan.backend_delete",
                        log_key="file_asset_service.purge_if_orphan.backend_delete",
                        log_window_seconds=300,
                    )
                    return {
                        "deleted": False,
                        "reason": f"file_delete_failed:{type(exc).__name__}:{exc}",
                    }
                if not file_deleted:
                    return {"deleted": False, "reason": "file_delete_failed"}

            # Delete DB
            try:
                session.execute(
                    _policy_sql("DELETE FROM file_asset WHERE file_asset_id = :id"),
                    {"id": file_asset_id},
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                report_swallowed_exception(
                    exc,
                    context="file_asset_service.purge_if_orphan.db_delete",
                    log_key="file_asset_service.purge_if_orphan.db_delete",
                    log_window_seconds=300,
                )
                return {
                    "deleted": False,
                    "file_deleted": file_deleted,
                    "reason": f"db_delete_failed:{type(exc).__name__}:{exc}",
                }

            return {
                "deleted": True,
                "file_deleted": file_deleted,
                "db_deleted": True,
                "reason": "deleted",
            }
        finally:
            if owns_session:
                session.close()

    def purge_orphaned_assets(
        self,
        *,
        min_age_days: int = 30,
        limit: int = 500,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Purge orphan FileAssets."""
        # ... logic similar to original, calling purge_if_orphan ...
        # Copied from original for simplicity
        try:
            limit_i = int(limit or 0)
        except Exception:
            limit_i = 500
        if limit_i <= 0:
            limit_i = 500

        rows = (
            db.session.execute(
                _policy_sql(
                    """
                SELECT fa.file_asset_id
                FROM file_asset fa
                WHERE NOT EXISTS (
                    SELECT 1 FROM matter_file_asset mfa WHERE mfa.file_asset_id = fa.file_asset_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM communication_file_asset cfa WHERE cfa.file_asset_id = fa.file_asset_id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM office_action_file_asset oafa WHERE oafa.file_asset_id = fa.file_asset_id
                )
                LIMIT :limit
                """
                ),
                {"limit": limit_i},
            )
            .scalars()
            .all()
        )

        scanned = 0
        deleted = 0
        bytes_freed = 0
        for fid in rows:
            scanned += 1
            res = self.purge_if_orphan(
                str(fid),
                min_age_days=min_age_days,
                dry_run=dry_run,
            )
            if res.get("deleted"):
                deleted += 1
        return {"scanned": scanned, "deleted": deleted}

    def _is_referenced(
        self, file_asset_id: str, *, file_path: str | None = None, session: SASession | None = None
    ) -> bool:
        session = session or db.session
        for table_name in (
            "matter_file_asset",
            "communication_file_asset",
            "office_action_file_asset",
            "matter_memo_file_asset",
        ):
            try:
                t = table(table_name, column("file_asset_id"))
                stmt = (
                    select(1).select_from(t).where(t.c.file_asset_id == str(file_asset_id)).limit(1)
                )
                linked = session.execute(stmt).scalar()
            except SQLAlchemyError:
                continue
            if linked:
                return True

        path = (file_path or "").strip()
        if path:
            path = path.replace("\\", "/")
            candidates = {path}
            legacy_prefix = "data/uploads/"
            if path.lower().startswith(legacy_prefix):
                candidates.add(path[len(legacy_prefix) :])
            else:
                candidates.add(f"{legacy_prefix}{path}")

            params = {"paths": list(candidates)}
            try:
                linked = session.execute(
                    _policy_sql(
                        """
                        SELECT 1
                        FROM email_message
                        WHERE raw_eml_path IN :paths
                        LIMIT 1
                        """
                    ).bindparams(bindparam("paths", expanding=True)),
                    params,
                ).scalar()
                if linked:
                    return True
            except SQLAlchemyError:
                pass

            try:
                linked = session.execute(
                    _policy_sql(
                        """
                        SELECT 1
                        FROM email_attachment
                        WHERE storage_path IN :paths
                           OR extracted_text_path IN :paths
                           OR ocr_text_path IN :paths
                        LIMIT 1
                        """
                    ).bindparams(bindparam("paths", expanding=True)),
                    params,
                ).scalar()
                if linked:
                    return True
            except SQLAlchemyError:
                pass
        return False

    def store_stream_to_path(
        self,
        stream: BinaryIO,
        *,
        rel_path: str | Path,
        overwrite: bool = True,
        track_pending: bool = True,
        max_bytes: int | None = None,
        reset_stream: bool = True,
    ) -> StoredUpload:
        """Store to a specific path (Always LOCAL/Default backendNew No, this is for non-asset files)."""
        # "store_to_path" implies user control over path.
        # If using S3, we can still support this but path is object key.

        backend = self._backend()
        path_str = self.normalize_rel_path(str(rel_path))

        if not overwrite and backend.exists(path_str):
            raise FileExistsError(f"File already exists: {path_str}")

        # Resolve max size (if configured)
        limit = None
        try:
            resolved = max_bytes if max_bytes is not None else self._max_upload_bytes()
            resolved_int = int(resolved or 0)
            if resolved_int > 0:
                limit = resolved_int
        except Exception:
            limit = None

        # We need size if possible; stream might be seekable
        size = None
        pos = None
        seekable = False
        try:
            pos = stream.tell()
            seekable = True
        except Exception:
            seekable = False

        if seekable:
            try:
                stream.seek(0, 2)
                total_size = stream.tell()
                expected_size = total_size if reset_stream else max(0, total_size - (pos or 0))
                size = expected_size
                if limit and expected_size > limit:
                    _raise_upload_too_large(limit)
            finally:
                try:
                    if reset_stream:
                        stream.seek(0)
                    else:
                        stream.seek(pos or 0)
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="file_asset_service.store_stream_to_path.reset_stream",
                        log_key="file_asset_service.store_stream_to_path.reset_stream",
                        log_window_seconds=300,
                    )

        counting_stream = None
        save_stream = stream
        if size is None:
            counting_stream = _CountingStream(stream, max_bytes=limit)
            save_stream = counting_stream

        try:
            saved_path = backend.save(save_stream, path_str, size=size)
        except UploadTooLargeError:
            if isinstance(backend, LocalStorageBackend):
                try:
                    backend.delete(path_str)
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="file_asset_service.store_stream_to_path.cleanup_upload_too_large",
                        log_key="file_asset_service.store_stream_to_path.cleanup_upload_too_large",
                        log_window_seconds=300,
                    )
            raise
        except Exception:
            if isinstance(backend, LocalStorageBackend):
                try:
                    backend.delete(path_str)
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="file_asset_service.store_stream_to_path.cleanup_error",
                        log_key="file_asset_service.store_stream_to_path.cleanup_error",
                        log_window_seconds=300,
                    )
            raise

        if counting_stream is not None:
            size = counting_stream.bytes_read

        abs_path = None
        if isinstance(backend, LocalStorageBackend):
            try:
                abs_path = (backend.root / saved_path).resolve()
            except Exception:
                abs_path = None
            if abs_path is not None and size is None:
                try:
                    size = abs_path.stat().st_size
                except Exception:
                    size = None

        return StoredUpload(
            rel_path=saved_path,
            abs_path=abs_path,  # None if S3 or if path couldn't be resolved
            sha256="",  # Skipping sha for stream storage for speed if not needed
            byte_size=size or 0,
        )

    # helper for compatibility
    def store_upload_to_path(
        self,
        file: FileStorage,
        *,
        rel_path: str | Path,
        overwrite: bool = True,
        track_pending: bool = True,
        max_bytes: int | None = None,
        reset_stream: bool = True,
    ) -> StoredUpload:
        stream = getattr(file, "stream", file)
        return self.store_stream_to_path(
            stream,
            rel_path=rel_path,
            overwrite=overwrite,
            track_pending=track_pending,
            max_bytes=max_bytes,
            reset_stream=reset_stream,
        )

    def _stream_to_temp(self, stream: BinaryIO, ext: str, subdir: str) -> tuple[str, int, Path]:
        """Stream to a local staging file for hashing/scanning before backend save."""
        try:
            stream.seek(0)
        except Exception:
            # Non-seekable streams are allowed; proceed without rewinding.
            _ = None

        hasher = hashlib.sha256()
        temp_id = uuid.uuid4().hex
        # Temp is always local and separate from final object storage.
        rel_path = self.normalize_rel_path(str(Path(subdir) / f"_staging_{temp_id}{ext}"))
        staging_root = self.staging_root
        abs_path = (staging_root / rel_path).resolve()
        if not abs_path.is_relative_to(staging_root):
            raise ValueError(f"Path traversal detected: {subdir}")
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        byte_size = 0
        max_bytes = self._max_upload_bytes()

        try:
            with open(abs_path, "wb") as f:
                while True:
                    chunk = stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    if max_bytes and (byte_size + len(chunk)) > max_bytes:
                        _raise_upload_too_large(max_bytes)
                    hasher.update(chunk)
                    f.write(chunk)
                    byte_size += len(chunk)
        except Exception:
            try:
                if abs_path.exists():
                    abs_path.unlink()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_asset_service._stream_to_temp.cleanup_temp",
                    log_key="file_asset_service._stream_to_temp.cleanup_temp",
                    log_window_seconds=300,
                )
            raise

        return hasher.hexdigest(), byte_size, abs_path

    def _upsert_file_asset(
        self,
        *,
        sha256: str,
        rel_path: str,
        original_name: str,
        byte_size: int,
        mime_type: str | None,
        ext: str,
        storage_type: str | None,
        virus_scan_status: str,
    ) -> tuple[str, bool, str, str | None]:
        new_id = uuid.uuid4().hex

        # Try Advisory Lock
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""
        if dialect == "postgresql":
            try:
                db.session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:sha, 0))"),
                    {"sha": sha256},
                )
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_asset_service._upsert_file_asset.advisory_lock",
                    log_key="file_asset_service._upsert_file_asset.advisory_lock",
                    log_window_seconds=300,
                )

        existing = db.session.execute(
            text(
                """
                SELECT file_asset_id, file_path, virus_scan_status
                FROM file_asset
                WHERE sha256 = :sha
                """
            ).execution_options(policy_bypass=True),
            {"sha": sha256},
        ).fetchone()
        if existing:
            return str(existing[0]), False, str(existing[1]), existing[2]

        stype = storage_type or "local"
        checked_at = (
            datetime.utcnow()
            if virus_scan_status in {SCAN_STATUS_CLEAN, SCAN_STATUS_DISABLED}
            else None
        )

        inserted = db.session.execute(
            text(
                """
                INSERT INTO file_asset(
                    file_asset_id,
                    storage_type,
                    file_path,
                    original_name,
                    sha256,
                    byte_size,
                    mime_type,
                    created_at,
                    virus_scan_status,
                    virus_scan_checked_at
                )
                VALUES(
                    :id,
                    :stype,
                    :path,
                    :name,
                    :sha,
                    :size,
                    :mime,
                    :created,
                    :virus_scan_status,
                    :virus_scan_checked_at
                )
                ON CONFLICT DO NOTHING
                RETURNING file_asset_id
            """
            ).execution_options(policy_bypass=True),
            {
                "id": new_id,
                "stype": stype,
                "path": rel_path,
                "name": original_name,
                "sha": sha256,
                "size": byte_size,
                "mime": mime_type,
                "created": datetime.utcnow().isoformat(),
                "virus_scan_status": virus_scan_status,
                "virus_scan_checked_at": checked_at,
            },
        ).scalar()

        if inserted:
            return str(inserted), True, rel_path, virus_scan_status

        # Race cond check
        existing = db.session.execute(
            text(
                """
                SELECT file_asset_id, file_path, virus_scan_status
                FROM file_asset
                WHERE sha256 = :sha
                """
            ).execution_options(policy_bypass=True),
            {"sha": sha256},
        ).fetchone()
        if existing:
            return str(existing[0]), False, str(existing[1]), existing[2]

        raise RuntimeError("file_asset insert failed")


_service: FileAssetService | None = None


def _current_configured_upload_root() -> Path | None:
    try:
        folder = runtime_upload_folder()
    except Exception:
        return None
    try:
        return Path(folder).resolve()
    except Exception:
        return None


def _service_uses_configured_upload_root(service: FileAssetService) -> bool:
    """Return whether the cached default local service still matches app config."""
    configured_root = _current_configured_upload_root()
    if configured_root is None:
        return True

    try:
        service_root = service._upload_root
    except Exception:
        service_root = None
    if service_root is not None:
        try:
            return Path(service_root).resolve() == configured_root
        except Exception:
            return True

    backend = getattr(service, "default_backend", None)
    if isinstance(backend, LocalStorageBackend):
        try:
            return Path(backend.root).resolve() == configured_root
        except Exception:
            return True

    return True


def _current_configured_storage_type() -> str:
    try:
        return runtime_storage_type()
    except Exception:
        return "local"


def _service_uses_configured_storage_type(service: FileAssetService) -> bool:
    backend = getattr(service, "default_backend", None)
    current_storage = _current_configured_storage_type()
    if current_storage == "s3":
        return not isinstance(backend, LocalStorageBackend)
    return isinstance(backend, LocalStorageBackend)


def get_file_asset_service() -> FileAssetService:
    global _service
    if _service is None:
        _service = FileAssetService()
    elif not getattr(_service, "_explicit_upload_root", False) and (
        not _service_uses_configured_upload_root(_service)
        or not _service_uses_configured_storage_type(_service)
    ):
        _service = FileAssetService()
    return _service
