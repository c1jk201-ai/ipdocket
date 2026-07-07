"""Office Action (Notice) service for history management.

Handles CRUD operations for office actions (notices) and their file attachments.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from flask import current_app

from app.extensions import db
from app.models.ip_records import Matter
from app.services.storage.file_asset_service import StagedFile, get_file_asset_service
from app.utils.search import compact_search_text as to_compact_compact
from app.utils.policy_sql import policy_text as text

if TYPE_CHECKING:
    from werkzeug.datastructures import FileStorage


@dataclass
class OfficeActionData:
    """Data for creating/updating an office action."""

    matter_id: str
    doc_name: str = ""
    received_date: str | None = None
    notified_date: str | None = None
    due_date: str | None = None
    extended_due_date: str | None = None
    done_date: str | None = None
    examiner: str | None = None


@dataclass
class OfficeActionResult:
    """Result of office action operations."""

    success: bool
    oa_id: str | None = None
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class OfficeActionAttachmentConflict:
    """Existing office action that already uses an uploaded file."""

    oa_id: str
    file_asset_id: str
    doc_name: str | None = None
    notified_date: str | None = None
    due_date: str | None = None
    original_name: str | None = None


class OfficeActionService:
    """Service for managing office actions (notices) and their attachments.

    Consolidates the CRUD logic from history_notice_new/edit/delete routes.
    """

    def create(
        self,
        data: OfficeActionData,
        *,
        staged_files: list[StagedFile] | None = None,
    ) -> OfficeActionResult:
        """
        Create a new office action with optional file attachments.

        Args:
            data: Office action data
            staged_files: Optional list of already-staged files to attach

        Returns:
            OfficeActionResult with success status
        """
        result = OfficeActionResult(success=False)

        # Validate
        if not data.doc_name:
            result.errors.append("Document name Required.")
            return result

        conflicts = self.find_attachment_conflicts(
            matter_id=str(data.matter_id),
            staged_files=staged_files,
        )
        if conflicts:
            result.errors.extend(self.format_attachment_conflict_errors(conflicts))
            return result

        try:
            oa_id = uuid.uuid4().hex

            with db.session.begin_nested():
                db.session.execute(
                    text(
                        """
                        INSERT INTO office_action(
                            oa_id, matter_id, doc_name, search_compact,
                            received_date, notified_date,
                            due_date, extended_due_date, done_date,
                            examiner
                        )
                        VALUES(
                            :oa_id, :matter_id, :doc_name, :search_compact,
                            :received_date, :notified_date,
                            :due_date, :extended_due_date, :done_date,
                            :examiner
                        )
                    """
                    ),
                    {
                        "oa_id": oa_id,
                        "matter_id": data.matter_id,
                        "doc_name": data.doc_name,
                        "search_compact": (
                            to_compact_compact(data.doc_name)
                            if (data.doc_name or "").strip()
                            else None
                        ),
                        "received_date": data.received_date or None,
                        "notified_date": data.notified_date or None,
                        "due_date": data.due_date or None,
                        "extended_due_date": data.extended_due_date or None,
                        "done_date": data.done_date or None,
                        "examiner": data.examiner or None,
                    },
                )

                # Attach files
                if staged_files:
                    for sf in staged_files:
                        file_asset_id = (
                            sf.file_asset_id
                            if isinstance(sf, StagedFile)
                            else sf.get("file_asset_id")
                        )
                        if file_asset_id:
                            self._link_file(oa_id, file_asset_id)

                self._sync_mgmt_deadline(
                    matter_id=str(data.matter_id),
                    oa_id=str(oa_id),
                    doc_name=data.doc_name,
                    received_date=data.received_date,
                    notified_date=data.notified_date,
                    due_date=data.due_date,
                    extended_due_date=data.extended_due_date,
                    done_date=data.done_date,
                )
                self._refresh_matter_auto_status_cache(str(data.matter_id))
                db.session.flush()
            result.success = True
            result.oa_id = oa_id
            result.messages.append("Office correspondence Registration.")

        except Exception as e:
            current_app.logger.error(f"Failed to create office action: {e}")
            result.errors.append(f"Office correspondence Save failed: {e}")

        return result

    def update(
        self,
        oa_id: str,
        data: OfficeActionData,
        *,
        staged_files: list[StagedFile] | None = None,
        remove_file_ids: list[str] | None = None,
    ) -> OfficeActionResult:
        """
        Update an existing office action.

        Args:
            oa_id: Office action ID to update
            data: Updated office action data
            staged_files: Optional new files to attach
            remove_file_ids: Optional file asset IDs to unlink

        Returns:
            OfficeActionResult with success status
        """
        result = OfficeActionResult(success=False, oa_id=oa_id)

        try:
            conflicts = self.find_attachment_conflicts(
                matter_id=str(data.matter_id),
                staged_files=staged_files,
                exclude_oa_id=str(oa_id),
            )
            if conflicts:
                result.errors.extend(self.format_attachment_conflict_errors(conflicts))
                return result

            with db.session.begin_nested():
                db.session.execute(
                    text(
                        """
                        UPDATE office_action SET
                            doc_name = :doc_name,
                            search_compact = :search_compact,
                            received_date = :received_date,
                            notified_date = :notified_date,
                            due_date = :due_date,
                            extended_due_date = :extended_due_date,
                            done_date = :done_date,
                            examiner = :examiner
                        WHERE oa_id = :oa_id
                    """
                    ),
                    {
                        "oa_id": oa_id,
                        "doc_name": data.doc_name,
                        "search_compact": (
                            to_compact_compact(data.doc_name)
                            if (data.doc_name or "").strip()
                            else None
                        ),
                        "received_date": data.received_date or None,
                        "notified_date": data.notified_date or None,
                        "due_date": data.due_date or None,
                        "extended_due_date": data.extended_due_date or None,
                        "done_date": data.done_date or None,
                        "examiner": data.examiner or None,
                    },
                )

                # Remove files
                if remove_file_ids:
                    for file_id in remove_file_ids:
                        self._unlink_file(oa_id, file_id)

                # Add new files
                if staged_files:
                    for sf in staged_files:
                        file_asset_id = (
                            sf.file_asset_id
                            if isinstance(sf, StagedFile)
                            else sf.get("file_asset_id")
                        )
                        if file_asset_id:
                            self._link_file(oa_id, file_asset_id)
                self._sync_mgmt_deadline(
                    matter_id=str(data.matter_id),
                    oa_id=str(oa_id),
                    doc_name=data.doc_name,
                    received_date=data.received_date,
                    notified_date=data.notified_date,
                    due_date=data.due_date,
                    extended_due_date=data.extended_due_date,
                    done_date=data.done_date,
                    force_cancel_when_missing_due=True,
                )
                self._refresh_matter_auto_status_cache(str(data.matter_id))
                db.session.flush()
            result.success = True
            result.messages.append("Office correspondence Edit.")

        except Exception as e:
            current_app.logger.error(f"Failed to update office action {oa_id}: {e}")
            result.errors.append(f"Office correspondence Edit Failed: {e}")

        return result

    def delete(self, oa_id: str, matter_id: str) -> OfficeActionResult:
        """
        Delete an office action and its file links.

        Args:
            oa_id: Office action ID to delete
            matter_id: Matter ID for verification

        Returns:
            OfficeActionResult with success status
        """
        result = OfficeActionResult(success=False, oa_id=oa_id)

        try:
            with db.session.begin_nested():
                # Verify ownership
                row = db.session.execute(
                    text(
                        """
                        SELECT matter_id, done_date
                        FROM office_action
                        WHERE oa_id = :oid
                        """
                    ),
                    {"oid": oa_id},
                ).fetchone()

                if not row:
                    result.errors.append("Office correspondence   none.")
                    return result

                if str(row[0]) != str(matter_id):
                    result.errors.append("Permission denied.")
                    return result

                close_token = (row[1] or "").strip() or f"AUTO_CANCELLED:{date.today().isoformat()}"
                self._sync_mgmt_deadline(
                    matter_id=str(matter_id),
                    oa_id=str(oa_id),
                    doc_name="",
                    received_date=None,
                    notified_date=None,
                    due_date=None,
                    extended_due_date=None,
                    done_date=close_token,
                )

                # Delete file links
                db.session.execute(
                    text("DELETE FROM office_action_file_asset WHERE oa_id = :oid"),
                    {"oid": oa_id},
                )

                # Delete office action
                db.session.execute(
                    text("DELETE FROM office_action WHERE oa_id = :oid"),
                    {"oid": oa_id},
                )
                self._refresh_matter_auto_status_cache(str(matter_id))
                db.session.flush()
            result.success = True
            result.messages.append("Office correspondence Delete.")

        except Exception as e:
            current_app.logger.error(f"Failed to delete office action {oa_id}: {e}")
            result.errors.append(f"Office correspondence Delete Failed: {e}")

        return result

    def get_attachments(self, oa_id: str) -> list[dict]:
        """Get file attachments for an office action."""
        rows = db.session.execute(
            text(
                """
                SELECT fa.file_asset_id, fa.original_name, fa.byte_size, fa.mime_type
                FROM office_action_file_asset oafa
                JOIN file_asset fa ON oafa.file_asset_id = fa.file_asset_id
                WHERE oafa.oa_id = :oid
            """
            ),
            {"oid": oa_id},
        ).fetchall()

        return [
            {
                "file_asset_id": row[0],
                "original_name": row[1],
                "byte_size": row[2],
                "mime_type": row[3],
            }
            for row in rows
        ]

    def find_attachment_conflicts(
        self,
        *,
        matter_id: str,
        staged_files: list[StagedFile] | None = None,
        exclude_oa_id: str | None = None,
    ) -> list[OfficeActionAttachmentConflict]:
        """Find uploaded files that are already attached to another notice in the matter."""
        conflicts: list[OfficeActionAttachmentConflict] = []
        seen_file_asset_ids: set[str] = set()

        for sf in staged_files or []:
            file_asset_id = self._extract_file_asset_id(sf)
            if not file_asset_id or file_asset_id in seen_file_asset_ids:
                continue
            seen_file_asset_ids.add(file_asset_id)

            row = db.session.execute(
                text(
                    """
                    SELECT
                        oa.oa_id,
                        oa.doc_name,
                        oa.notified_date,
                        oa.due_date,
                        fa.original_name
                    FROM office_action oa
                    JOIN office_action_file_asset oafa
                      ON oafa.oa_id = oa.oa_id
                    LEFT JOIN file_asset fa
                      ON fa.file_asset_id = oafa.file_asset_id
                    WHERE oa.matter_id = :mid
                      AND oafa.file_asset_id = :fid
                      AND COALESCE(LOWER(CAST(oafa.is_deleted AS TEXT)), 'false')
                          IN ('false', '0', 'f')
                      AND (:exclude_oa_id IS NULL OR oa.oa_id <> :exclude_oa_id)
                    LIMIT 1
                    """
                ),
                {
                    "mid": str(matter_id),
                    "fid": str(file_asset_id),
                    "exclude_oa_id": (str(exclude_oa_id).strip() or None),
                },
            ).fetchone()
            if not row:
                continue

            conflicts.append(
                OfficeActionAttachmentConflict(
                    oa_id=str(row[0]),
                    file_asset_id=str(file_asset_id),
                    doc_name=row[1],
                    notified_date=row[2],
                    due_date=row[3],
                    original_name=row[4],
                )
            )

        return conflicts

    def format_attachment_conflict_errors(
        self,
        conflicts: list[OfficeActionAttachmentConflict] | None,
    ) -> list[str]:
        messages: list[str] = []
        seen: set[tuple[str, str]] = set()
        for conflict in conflicts or []:
            key = (str(conflict.oa_id), str(conflict.file_asset_id))
            if key in seen:
                continue
            seen.add(key)
            messages.append(self._format_attachment_conflict_error(conflict))
        return messages

    @staticmethod
    def _extract_file_asset_id(staged_file: StagedFile | dict | None) -> str | None:
        if isinstance(staged_file, StagedFile):
            file_asset_id = staged_file.file_asset_id
        elif isinstance(staged_file, dict):
            file_asset_id = staged_file.get("file_asset_id")
        else:
            file_asset_id = None
        token = str(file_asset_id or "").strip()
        return token or None

    @staticmethod
    def _format_attachment_conflict_error(conflict: OfficeActionAttachmentConflict) -> str:
        summary = (conflict.doc_name or "").strip() or "Existing Notice"
        detail_parts = []
        if (conflict.notified_date or "").strip():
            detail_parts.append(f"Notice {conflict.notified_date}")
        elif (conflict.due_date or "").strip():
            detail_parts.append(f"Due date {conflict.due_date}")
        if (conflict.original_name or "").strip():
            detail_parts.append(f"File {conflict.original_name}")
        if detail_parts:
            summary = f"{summary} ({', '.join(detail_parts)})"
        return f" Registration Notice : {summary}"

    def _link_file(self, oa_id: str, file_asset_id: str) -> None:
        """Link a file asset to an office action."""
        db.session.execute(
            text(
                """
                INSERT INTO office_action_file_asset(oa_file_id, oa_id, file_asset_id, role, description)
                VALUES(:oafid, :oid, :fid, 'upload', '')
                ON CONFLICT DO NOTHING
            """
            ),
            {"oafid": uuid.uuid4().hex, "oid": oa_id, "fid": file_asset_id},
        )

    def _unlink_file(self, oa_id: str, file_asset_id: str) -> None:
        """Unlink a file asset from an office action."""
        db.session.execute(
            text(
                "DELETE FROM office_action_file_asset WHERE oa_id = :oid AND file_asset_id = :fid"
            ),
            {"oid": oa_id, "fid": file_asset_id},
        )

    def _sync_mgmt_deadline(
        self,
        matter_id: str,
        oa_id: str,
        doc_name: str,
        received_date: str | None,
        notified_date: str | None,
        due_date: str | None,
        extended_due_date: str | None,
        done_date: str | None,
        *,
        force_cancel_when_missing_due: bool = False,
    ) -> None:
        """Keep OA-derived docket rows in sync with office_action CRUD changes."""
        try:
            from app.services.deadlines.docket_service import complete_office_action_docket
            from app.services.deadlines.mgmt_deadlines import (
                create_office_action_due_deadline,
                sync_notice_send_sla,
            )

            due_token = (due_date or "").strip()
            ext_token = (extended_due_date or "").strip()
            done_token = (done_date or "").strip()

            sync_notice_send_sla(
                matter_id=str(matter_id),
                oa_id=str(oa_id),
                received_date=received_date,
                doc_name=doc_name,
                done_date=done_token or None,
                commit=False,
            )

            if due_token or ext_token:
                create_office_action_due_deadline(
                    matter_id=str(matter_id),
                    oa_id=str(oa_id),
                    doc_name=doc_name,
                    due_date=due_token or None,
                    extended_due_date=ext_token or None,
                    done_date=done_token or None,
                    commit=False,
                )
            else:
                if done_token:
                    complete_office_action_docket(
                        str(matter_id),
                        str(oa_id),
                        done_token,
                        commit=False,
                    )
                elif force_cancel_when_missing_due:
                    complete_office_action_docket(
                        str(matter_id),
                        str(oa_id),
                        f"AUTO_CANCELLED:{date.today().isoformat()}",
                        commit=False,
                    )

        except Exception as exc:
            current_app.logger.warning(
                "OA deadline sync failed (matter_id=%s, oa_id=%s): %s",
                matter_id,
                oa_id,
                exc,
            )

    def _refresh_matter_auto_status_cache(self, matter_id: str) -> None:
        mid = (matter_id or "").strip()
        if not mid:
            return
        try:
            from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter

            db.session.flush()
            matter = db.session.get(Matter, mid)
            if matter is None:
                return
            result = apply_auto_status_cache_to_matter(matter=matter)
            if result.changed:
                db.session.add(matter)
        except Exception as exc:
            current_app.logger.warning(
                "OA auto-status refresh failed (matter_id=%s): %s",
                mid,
                exc,
            )

# Singleton
_service: OfficeActionService | None = None


def get_office_action_service() -> OfficeActionService:
    """Get singleton OfficeActionService instance."""
    global _service
    if _service is None:
        _service = OfficeActionService()
    return _service
