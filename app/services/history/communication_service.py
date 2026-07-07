"""Communication (Letter) service for history management.

Handles CRUD operations for communications (letters) and their file attachments.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from flask import current_app

from app.extensions import db
from app.services.storage.file_asset_service import StagedFile, get_file_asset_service
from app.utils.search import compact_search_text as to_compact_compact
from app.utils.policy_sql import policy_text as text

if TYPE_CHECKING:
    from werkzeug.datastructures import FileStorage


def _policy_sql(sql: str):
    return text(sql).execution_options(policy_bypass=True)


@dataclass
class CommunicationData:
    """Data for creating/updating a communication."""

    matter_id: str
    comm_type: str = "M"  # M=Mail, T=Telephone, R=Response
    direction: str = ""  #  or Send
    subject: str = ""
    to_text: str = ""
    received_date: str | None = None
    sent_date: str | None = None
    due_date: str | None = None
    done_date: str | None = None
    owner_staff_party_id: str | None = None
    author_staff_party_id: str | None = None
    source_text: str | None = None
    source: str | None = None
    actor_user_id: int | str | None = None


@dataclass
class CommunicationResult:
    """Result of communication operations."""

    success: bool
    comm_id: str | None = None
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CommunicationService:
    """Service for managing communications (letters) and their attachments.

    Consolidates the CRUD logic from history_letter_new/edit/delete routes.
    """

    def create(
        self,
        data: CommunicationData,
        *,
        staged_files: list[StagedFile] | None = None,
    ) -> CommunicationResult:
        """
        Create a new communication with optional file attachments.

        Args:
            data: Communication data
            staged_files: Optional list of already-staged files to attach

        Returns:
            CommunicationResult with success status
        """
        result = CommunicationResult(success=False)

        # Validate
        if data.direction == "" and not data.received_date:
            result.errors.append("Upload Required.")
            return result
        if data.direction == "Send" and not data.sent_date:
            result.errors.append("Send Required.")
            return result

        try:
            comm_id = uuid.uuid4().hex

            with db.session.begin_nested():
                db.session.execute(
                    _policy_sql(
                        """
                        INSERT INTO communication(
                            comm_id, matter_id, comm_type,
                            received_date, sent_date,
                            due_date, done_date,
                            to_text, note, search_compact,
                            owner_staff_party_id, author_staff_party_id
                        )
                        VALUES(
                            :comm_id, :matter_id, :comm_type,
                            :received_date, :sent_date,
                            :due_date, :done_date,
                            :to_text, :note, :search_compact,
                            :owner_staff_party_id, :author_staff_party_id
                        )
                    """
                    ),
                    {
                        "comm_id": comm_id,
                        "matter_id": data.matter_id,
                        "comm_type": data.comm_type or "M",
                        "received_date": data.received_date or None,
                        "sent_date": data.sent_date or None,
                        "due_date": data.due_date or None,
                        "done_date": data.done_date or None,
                        "to_text": data.to_text or None,
                        "note": data.subject or None,
                        "search_compact": (
                            to_compact_compact(data.subject)
                            if (data.subject or "").strip()
                            else None
                        ),
                        "owner_staff_party_id": data.owner_staff_party_id,
                        "author_staff_party_id": data.author_staff_party_id
                        or data.owner_staff_party_id,
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
                            self._link_file(comm_id, file_asset_id)

                db.session.flush()
            self._mark_notice_send_candidates(data)
            result.success = True
            result.comm_id = comm_id
            result.messages.append(" Registration.")

        except Exception as e:
            current_app.logger.error(f"Failed to create communication: {e}")
            result.errors.append(f" Save failed: {e}")

        return result

    def update(
        self,
        comm_id: str,
        data: CommunicationData,
        *,
        staged_files: list[StagedFile] | None = None,
        remove_file_ids: list[str] | None = None,
    ) -> CommunicationResult:
        """
        Update an existing communication.

        Args:
            comm_id: Communication ID to update
            data: Updated communication data
            staged_files: Optional new files to attach
            remove_file_ids: Optional file asset IDs to unlink

        Returns:
            CommunicationResult with success status
        """
        result = CommunicationResult(success=False, comm_id=comm_id)

        # Validate
        if data.direction == "" and not data.received_date:
            result.errors.append("Upload Required.")
            return result
        if data.direction == "Send" and not data.sent_date:
            result.errors.append("Send Required.")
            return result

        try:
            with db.session.begin_nested():
                db.session.execute(
                    _policy_sql(
                        """
                        UPDATE communication SET
                            comm_type = :comm_type,
                            received_date = :received_date,
                            sent_date = :sent_date,
                            due_date = :due_date,
                            done_date = :done_date,
                            to_text = :to_text,
                            note = :note,
                            search_compact = :search_compact,
                            owner_staff_party_id = :owner_staff_party_id
                        WHERE comm_id = :comm_id
                    """
                    ),
                    {
                        "comm_id": comm_id,
                        "comm_type": data.comm_type or "M",
                        "received_date": data.received_date or None,
                        "sent_date": data.sent_date or None,
                        "due_date": data.due_date or None,
                        "done_date": data.done_date or None,
                        "to_text": data.to_text or None,
                        "note": data.subject or None,
                        "search_compact": (
                            to_compact_compact(data.subject)
                            if (data.subject or "").strip()
                            else None
                        ),
                        "owner_staff_party_id": data.owner_staff_party_id,
                    },
                )

                # Remove files
                if remove_file_ids:
                    for file_id in remove_file_ids:
                        self._unlink_file(comm_id, file_id)

                # Add new files
                if staged_files:
                    for sf in staged_files:
                        file_asset_id = (
                            sf.file_asset_id
                            if isinstance(sf, StagedFile)
                            else sf.get("file_asset_id")
                        )
                        if file_asset_id:
                            self._link_file(comm_id, file_asset_id)

                db.session.flush()
            self._refresh_notice_send_candidates(data)
            result.success = True
            result.messages.append(" Edit.")

        except Exception as e:
            current_app.logger.error(f"Failed to update communication {comm_id}: {e}")
            result.errors.append(f" Edit Failed: {e}")

        return result

    def delete(self, comm_id: str, matter_id: str) -> CommunicationResult:
        """
        Delete a communication and its file links.

        Args:
            comm_id: Communication ID to delete
            matter_id: Matter ID for verification

        Returns:
            CommunicationResult with success status
        """
        result = CommunicationResult(success=False, comm_id=comm_id)

        try:
            with db.session.begin_nested():
                # Verify ownership
                row = db.session.execute(
                    _policy_sql("SELECT matter_id FROM communication WHERE comm_id = :cid"),
                    {"cid": comm_id},
                ).fetchone()

                if not row:
                    result.errors.append("   none.")
                    return result

                if str(row[0]) != str(matter_id):
                    result.errors.append("Permission denied.")
                    return result

                # Delete file links
                db.session.execute(
                    _policy_sql("DELETE FROM communication_file_asset WHERE comm_id = :cid"),
                    {"cid": comm_id},
                )

                # Delete communication
                db.session.execute(
                    _policy_sql("DELETE FROM communication WHERE comm_id = :cid"),
                    {"cid": comm_id},
                )
                db.session.flush()
            self._refresh_notice_send_candidates(
                CommunicationData(
                    matter_id=str(matter_id),
                    source="communication_service.delete",
                )
            )
            result.success = True
            result.messages.append(" Delete.")

        except Exception as e:
            current_app.logger.error(f"Failed to delete communication {comm_id}: {e}")
            result.errors.append(f" Delete Failed: {e}")

        return result

    def get_attachments(self, comm_id: str) -> list[dict]:
        """Get file attachments for a communication."""
        rows = db.session.execute(
            _policy_sql(
                """
                SELECT fa.file_asset_id, fa.original_name, fa.byte_size, fa.mime_type
                FROM communication_file_asset cfa
                JOIN file_asset fa ON cfa.file_asset_id = fa.file_asset_id
                WHERE cfa.comm_id = :cid
            """
            ),
            {"cid": comm_id},
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

    def _link_file(self, comm_id: str, file_asset_id: str) -> None:
        """Link a file asset to a communication."""
        db.session.execute(
            _policy_sql(
                """
                INSERT INTO communication_file_asset(comm_file_id, comm_id, file_asset_id, role, description)
                VALUES(:cfid, :cid, :fid, 'upload', '')
                ON CONFLICT DO NOTHING
            """
            ),
            {"cfid": uuid.uuid4().hex, "cid": comm_id, "fid": file_asset_id},
        )

    def _unlink_file(self, comm_id: str, file_asset_id: str) -> None:
        """Unlink a file asset from a communication."""
        db.session.execute(
            _policy_sql(
                "DELETE FROM communication_file_asset WHERE comm_id = :cid AND file_asset_id = :fid"
            ),
            {"cid": comm_id, "fid": file_asset_id},
        )

    def _mark_notice_send_candidates(self, data: CommunicationData) -> None:
        try:
            from app.services.deadlines.notice_send_semi_close import mark_notice_send_candidates

            mark_notice_send_candidates(
                matter_id=str(data.matter_id),
                direction=data.direction,
                doc_name=data.subject,
                source_text=data.source_text,
                sent_date=data.sent_date,
                comm_type=data.comm_type,
                source=(data.source or "communication_service.create"),
                actor_user_id=data.actor_user_id,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Notice-send candidate mark skipped (matter_id=%s, source=%s): %s",
                data.matter_id,
                data.source or "communication_service.create",
                exc,
            )

    def _refresh_notice_send_candidates(self, data: CommunicationData) -> None:
        try:
            from app.services.deadlines.notice_send_semi_close import (
                refresh_notice_send_candidates_for_matter,
            )

            refresh_notice_send_candidates_for_matter(
                matter_id=str(data.matter_id),
                source=(data.source or "communication_service.refresh"),
                actor_user_id=data.actor_user_id,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Notice-send candidate refresh skipped (matter_id=%s, source=%s): %s",
                data.matter_id,
                data.source or "communication_service.refresh",
                exc,
            )


# Singleton
_service: CommunicationService | None = None


def get_communication_service() -> CommunicationService:
    """Get singleton CommunicationService instance."""
    global _service
    if _service is None:
        _service = CommunicationService()
    return _service
