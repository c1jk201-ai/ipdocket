"""Upload session management for multi-step upload flows."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app, has_request_context
from flask_login import current_user

from app.extensions import db
from app.services.storage.file_asset_service import StagedFile
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


@dataclass
class UploadSessionData:
    """Data stored in an upload session."""

    staged_files: list[dict]  # List of StagedFile as dicts
    form_data: dict  # Form field values
    analysis_result: dict | None = None  # Optional analysis/conflict data
    validation_result: dict | None = None  # Optional intake validation/scanning evidence
    purpose: str = ""  # Purpose of the upload session


class UploadSessionService:
    """Service for managing upload sessions across requests.

    This service enables the standard "2-step POST" flow for uploads:
    1. First POST: Stage files, check duplicates, create session
    2. Second POST (confirm): Retrieve session, continue processing

    This replaces the fragile pattern of passing file data via hidden JSON
    or relying on files being re-sent on confirm.
    """

    DEFAULT_TTL_HOURS = 24
    _table_checked = False

    def _is_missing_table_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return "upload_session" in msg and (
            "does not exist" in msg or "undefinedtable" in msg or "no such table" in msg
        )

    def _ensure_table(self) -> None:
        if self._table_checked:
            return
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1 FROM upload_session LIMIT 1"))
            self._table_checked = True
            return
        except Exception as e:
            if not self._is_missing_table_error(e):
                current_app.logger.error(f"Upload session table check failed: {e}")
                raise
        current_app.logger.error(
            "upload_session table missing; apply migrations before using upload sessions"
        )
        raise RuntimeError("upload_session table missing")

    def _parse_dt(self, value) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except Exception:
            return None

    def _request_user_scope(self) -> tuple[bool, str | None]:
        """
        Determine whether we should enforce user ownership for this operation.

        Security model:
        - If we're in a request context, only the authenticated owner can
          retrieve/update/delete an upload session.
        - Outside a request context (e.g. ops/CLI), we allow access by session_id.
        """
        if not has_request_context():
            return False, None
        try:
            if current_user and current_user.is_authenticated:
                uid = getattr(current_user, "id", None)
                return True, (str(uid) if uid is not None else None)
            # In a request context but not authenticated: treat as forbidden.
            return True, None
        except Exception as exc:
            # Best-effort; default to "request context => enforce" to avoid widening access on errors.
            report_swallowed_exception(
                exc,
                context="uploads.upload_session_service.request_user_scope",
                log_key="uploads.upload_session_service.request_user_scope",
                log_window_seconds=300,
            )
            return True, None

    def create(
        self,
        *,
        purpose: str,
        staged_files: list[StagedFile],
        form_data: dict | None = None,
        analysis_result: dict | None = None,
        validation_result: dict | None = None,
        ttl_hours: int | None = None,
    ) -> str:
        """
        Create a new upload session.

        Args:
            purpose: Upload type identifier ('application', 'response', 'letter', 'notice', 'fm')
            staged_files: List of StagedFile objects that have been saved
            form_data: Optional form field values to preserve
            analysis_result: Optional analysis/conflict data
            ttl_hours: Session TTL in hours (default: 24)

        Returns:
            session_id for later retrieval
        """
        self._ensure_table()
        session_id = uuid.uuid4().hex
        ttl = ttl_hours or self.DEFAULT_TTL_HOURS
        expires_at = datetime.utcnow() + timedelta(hours=ttl)

        # Convert StagedFile objects to dicts
        staged_file_dicts = []
        for sf in staged_files:
            if isinstance(sf, StagedFile):
                staged_file_dicts.append(asdict(sf))
            elif isinstance(sf, dict):
                staged_file_dicts.append(sf)
            else:
                current_app.logger.warning(f"Unknown staged file type: {type(sf)}")

        payload = UploadSessionData(
            staged_files=staged_file_dicts,
            form_data=form_data or {},
            analysis_result=analysis_result,
            validation_result=validation_result,
            purpose=purpose,
        )

        user_id = "anonymous"
        if has_request_context():
            try:
                if current_user and current_user.is_authenticated:
                    user_id = str(current_user.id)
            except Exception as exc:
                # Best-effort: do not block uploads if request-user context cannot be resolved.
                report_swallowed_exception(
                    exc,
                    context="uploads.upload_session_service.create.current_user",
                    log_key="uploads.upload_session_service.create.current_user",
                    log_window_seconds=300,
                )

        with db.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO upload_session(session_id, user_id, purpose, payload_json, expires_at, created_at)
                    VALUES(:sid, :uid, :purpose, :payload, :expires, :created)
                    """
                ),
                {
                    "sid": session_id,
                    "uid": user_id,
                    "purpose": purpose,
                    "payload": json.dumps(asdict(payload)),
                    "expires": expires_at,
                    "created": datetime.utcnow(),
                },
            )

        current_app.logger.info(f"Created upload session {session_id} for {purpose}")
        return session_id

    def retrieve(self, session_id: str) -> UploadSessionData | None:
        """
        Retrieve upload session data.

        Args:
            session_id: The session ID from create()

        Returns:
            UploadSessionData if session is valid and not expired, None otherwise
        """
        self._ensure_table()
        if not session_id:
            return None
        enforce_user, uid = self._request_user_scope()
        if enforce_user and not uid:
            return None

        now = datetime.utcnow()
        with db.engine.connect() as conn:
            where = "session_id = :sid AND expires_at > :now"
            params: dict[str, Any] = {"sid": session_id, "now": now}
            if enforce_user:
                where += " AND user_id = :uid"
                params["uid"] = uid
            row = conn.execute(
                text(
                    f"""
                    SELECT payload_json FROM upload_session
                    WHERE {where}
                    """
                ),
                params,
            ).fetchone()

        if not row:
            with db.engine.connect() as conn:
                where = "session_id = :sid"
                params = {"sid": session_id}
                if enforce_user:
                    where += " AND user_id = :uid"
                    params["uid"] = uid
                fallback = conn.execute(
                    text(
                        f"""
                        SELECT payload_json, expires_at FROM upload_session
                        WHERE {where}
                        """
                    ),
                    params,
                ).fetchone()
            if not fallback:
                current_app.logger.warning(f"Upload session not found or expired: {session_id}")
                return None
            payload_json, expires_at = fallback
            exp_dt = self._parse_dt(expires_at)
            if exp_dt and exp_dt <= now:
                current_app.logger.warning(
                    f"Upload session expired: {session_id} (expires_at={exp_dt})"
                )
                return None
            row = (payload_json,)

        try:
            data = json.loads(row[0])
            return UploadSessionData(
                staged_files=data.get("staged_files", []),
                form_data=data.get("form_data", {}),
                analysis_result=data.get("analysis_result"),
                validation_result=data.get("validation_result"),
                purpose=data.get("purpose", ""),
            )
        except json.JSONDecodeError as e:
            current_app.logger.error(f"Failed to parse upload session {session_id}: {e}")
            return None

    def update(
        self,
        session_id: str,
        *,
        form_data: dict | None = None,
        analysis_result: dict | None = None,
        validation_result: dict | None = None,
    ) -> bool:
        """
        Update an existing upload session.

        Args:
            session_id: The session ID
            form_data: New form data (merged with existing)
            analysis_result: New analysis result (replaces existing)

        Returns:
            True if session was updated, False if not found
        """
        self._ensure_table()
        existing = self.retrieve(session_id)
        if not existing:
            return False

        # Merge updates
        if form_data:
            existing.form_data.update(form_data)
        if analysis_result is not None:
            existing.analysis_result = analysis_result
        if validation_result is not None:
            existing.validation_result = validation_result

        enforce_user, uid = self._request_user_scope()
        if enforce_user and not uid:
            return False
        with db.engine.begin() as conn:
            where = "session_id = :sid"
            params: dict[str, Any] = {
                "sid": session_id,
                "payload": json.dumps(asdict(existing)),
            }
            if enforce_user:
                where += " AND user_id = :uid"
                params["uid"] = uid
            conn.execute(
                text(
                    f"""
                    UPDATE upload_session
                    SET payload_json = :payload
                    WHERE {where}
                    """
                ),
                params,
            )
        return True

    def delete(self, session_id: str) -> None:
        """Delete an upload session."""
        self._ensure_table()
        enforce_user, uid = self._request_user_scope()
        if enforce_user and not uid:
            return
        with db.engine.begin() as conn:
            where = "session_id = :sid"
            params: dict[str, Any] = {"sid": session_id}
            if enforce_user:
                where += " AND user_id = :uid"
                params["uid"] = uid
            conn.execute(
                text(f"DELETE FROM upload_session WHERE {where}"),
                params,
            )

    def cleanup_expired(self) -> int:
        """
        Remove expired sessions.

        Returns:
            Count of deleted rows
        """
        self._ensure_table()
        expired_payloads: list[dict[str, Any]] = []
        try:
            with db.engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT payload_json FROM upload_session WHERE expires_at <= :now"),
                    {"now": datetime.utcnow()},
                ).fetchall()
            for row in rows or []:
                try:
                    payload = json.loads(row[0] or "{}")
                    if isinstance(payload, dict):
                        expired_payloads.append(payload)
                except Exception:
                    continue
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="uploads.upload_session_service.cleanup_expired.load_payloads",
                log_key="uploads.upload_session_service.cleanup_expired.load_payloads",
                log_window_seconds=300,
            )

        with db.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM upload_session WHERE expires_at <= :now"),
                {"now": datetime.utcnow()},
            )
            count = result.rowcount or 0
        self._cleanup_expired_artifacts(expired_payloads)
        if count > 0:
            current_app.logger.info(f"Cleaned up {count} expired upload sessions")
        return count

    def _cleanup_expired_artifacts(self, payloads: list[dict[str, Any]]) -> None:
        if not payloads:
            return
        try:
            from app.services.storage.file_asset_service import get_file_asset_service

            file_service = get_file_asset_service()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="uploads.upload_session_service.cleanup_expired.file_service",
                log_key="uploads.upload_session_service.cleanup_expired.file_service",
                log_window_seconds=300,
            )
            file_service = None

        for payload in payloads:
            staged_files = payload.get("staged_files") or []
            if file_service is not None:
                for item in staged_files:
                    if not isinstance(item, dict):
                        continue
                    fid = str(item.get("file_asset_id") or "").strip()
                    if not fid:
                        continue
                    try:
                        file_service.purge_if_orphan(fid, min_age_days=0)
                    except Exception as exc:
                        report_swallowed_exception(
                            exc,
                            context="uploads.upload_session_service.cleanup_expired.purge_file_asset",
                            log_key="uploads.upload_session_service.cleanup_expired.purge_file_asset",
                            log_window_seconds=300,
                        )

            raw_form_data = payload.get("form_data")
            form_data: dict[str, Any] = raw_form_data if isinstance(raw_form_data, dict) else {}
            temp_dir = str(form_data.get("temp_dir") or "").strip()
            if not temp_dir:
                continue
            try:
                upload_root = str(current_app.config.get("UPLOAD_FOLDER") or "").strip()
                base = os.path.abspath(os.path.join(upload_root, "temp", "response_upload"))
                target = os.path.abspath(temp_dir)
                if os.path.commonpath([base, target]) == base and os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="uploads.upload_session_service.cleanup_expired.temp_dir",
                    log_key="uploads.upload_session_service.cleanup_expired.temp_dir",
                    log_window_seconds=300,
                )

    def get_staged_file_ids(self, session_id: str) -> list[str]:
        """
        Get list of file_asset_ids from a session.

        Convenience method for retrieving just the file IDs.
        """
        session_data = self.retrieve(session_id)
        if not session_data:
            return []

        file_ids: list[str] = []
        for staged_file in session_data.staged_files:
            file_asset_id = str(staged_file.get("file_asset_id") or "").strip()
            if file_asset_id:
                file_ids.append(file_asset_id)
        return file_ids


# Singleton
_service: UploadSessionService | None = None


def get_upload_session_service() -> UploadSessionService:
    """Get singleton UploadSessionService instance."""
    global _service
    if _service is None:
        _service = UploadSessionService()
    return _service
