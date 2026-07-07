"""
Duplicate File Check Service

Provides SHA256-based duplicate detection for uploaded files.
"""

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Optional

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


@dataclass
class DuplicateInfo:
    """Result of a duplicate file check."""

    is_duplicate: bool
    sha256: str
    existing_file_asset_id: Optional[str] = None
    original_name: Optional[str] = None
    created_at: Optional[str] = None


def calculate_sha256(data: bytes) -> str:
    """Calculate SHA256 hash of byte data."""
    return hashlib.sha256(data).hexdigest()


def calculate_sha256_from_stream(stream: BinaryIO) -> str:
    """Calculate SHA256 hash from a file-like stream (chunked) and restore position if possible."""
    h = hashlib.sha256()
    chunk_size = 1024 * 1024  # 1MB

    # Remember current position if seekable
    pos = None
    try:
        pos = stream.tell()
    except Exception:
        pos = None

    # Prefer hashing from start
    try:
        stream.seek(0)
    except Exception as exc:
        # If not seekable, hash from current position (best-effort).
        report_swallowed_exception(
            exc,
            context="duplicate_check.calculate_sha256_from_stream.seek_start",
            log_key="duplicate_check.calculate_sha256_from_stream.seek_start",
            log_window_seconds=300,
        )

    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)

    # Restore
    try:
        if pos is not None:
            stream.seek(pos)
        else:
            stream.seek(0)
    except Exception as exc:
        # Best-effort: failing to restore position should not fail duplicate detection.
        report_swallowed_exception(
            exc,
            context="duplicate_check.calculate_sha256_from_stream.restore_position",
            log_key="duplicate_check.calculate_sha256_from_stream.restore_position",
            log_window_seconds=300,
        )

    return h.hexdigest()


def _find_active_duplicate_row(sha: str):
    if not sha:
        return None
    try:
        row = db.session.execute(
            text(
                """
                SELECT fa.file_asset_id, fa.original_name, fa.created_at
                FROM file_asset fa
                WHERE fa.sha256 = :sha
                  AND COALESCE(fa.is_deleted, false) = false
                  AND (
                    EXISTS (
                        SELECT 1
                        FROM matter_file_asset mfa
                        JOIN matter m ON m.matter_id = mfa.matter_id
                        WHERE mfa.file_asset_id = fa.file_asset_id
                          AND COALESCE(mfa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM communication_file_asset cfa
                        JOIN communication c ON c.comm_id = cfa.comm_id
                        JOIN matter m ON m.matter_id = c.matter_id
                        WHERE cfa.file_asset_id = fa.file_asset_id
                          AND COALESCE(cfa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM office_action_file_asset oafa
                        JOIN office_action oa ON oa.oa_id = oafa.oa_id
                        JOIN matter m ON m.matter_id = oa.matter_id
                        WHERE oafa.file_asset_id = fa.file_asset_id
                          AND COALESCE(oafa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                  )
                LIMIT 1
                """
            ),
            {"sha": sha},
        ).fetchone()
        return row
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="duplicate_check._find_active_duplicate_row",
            log_key="duplicate_check._find_active_duplicate_row",
            log_window_seconds=300,
        )
        return None


def is_file_asset_active(file_asset_id: str) -> bool:
    if not (file_asset_id or "").strip():
        return False
    try:
        row = db.session.execute(
            text(
                """
                SELECT 1
                FROM file_asset fa
                WHERE fa.file_asset_id = :fid
                  AND COALESCE(fa.is_deleted, false) = false
                  AND (
                    EXISTS (
                        SELECT 1
                        FROM matter_file_asset mfa
                        JOIN matter m ON m.matter_id = mfa.matter_id
                        WHERE mfa.file_asset_id = fa.file_asset_id
                          AND COALESCE(mfa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM communication_file_asset cfa
                        JOIN communication c ON c.comm_id = cfa.comm_id
                        JOIN matter m ON m.matter_id = c.matter_id
                        WHERE cfa.file_asset_id = fa.file_asset_id
                          AND COALESCE(cfa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM office_action_file_asset oafa
                        JOIN office_action oa ON oa.oa_id = oafa.oa_id
                        JOIN matter m ON m.matter_id = oa.matter_id
                        WHERE oafa.file_asset_id = fa.file_asset_id
                          AND COALESCE(oafa.is_deleted, false) = false
                          AND COALESCE(m.is_deleted, false) = false
                    )
                  )
                LIMIT 1
                """
            ),
            {"fid": file_asset_id},
        ).scalar()
        return bool(row)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="duplicate_check.is_file_asset_active",
            log_key="duplicate_check.is_file_asset_active",
            log_window_seconds=300,
        )
        return False


def check_duplicate_file(file_stream: BinaryIO) -> DuplicateInfo:
    """
    Check if a file with identical content already exists in the database.

    Args:
        file_stream: A file-like object (e.g., from request.files).

    Returns:
        DuplicateInfo with duplicate status and metadata if found.
    """
    sha = calculate_sha256_from_stream(file_stream)

    row = _find_active_duplicate_row(sha)

    if row:
        return DuplicateInfo(
            is_duplicate=True,
            sha256=sha,
            existing_file_asset_id=str(row[0]),
            original_name=row[1],
            created_at=row[2],
        )

    return DuplicateInfo(
        is_duplicate=False,
        sha256=sha,
    )


def check_duplicate_bytes(data: bytes) -> DuplicateInfo:
    """
    Check if file content (as bytes) already exists in the database.

    Args:
        data: Raw file content as bytes.

    Returns:
        DuplicateInfo with duplicate status and metadata if found.
    """
    sha = calculate_sha256(data)

    row = _find_active_duplicate_row(sha)

    if row:
        return DuplicateInfo(
            is_duplicate=True,
            sha256=sha,
            existing_file_asset_id=str(row[0]),
            original_name=row[1],
            created_at=row[2],
        )

    return DuplicateInfo(
        is_duplicate=False,
        sha256=sha,
    )


def get_existing_file_asset_id(sha256: str) -> Optional[str]:
    """
    Get the file_asset_id for an existing file by SHA256.

    Args:
        sha256: The SHA256 hash to look up.

    Returns:
        file_asset_id if found, None otherwise.
    """
    result = db.session.execute(
        text("SELECT file_asset_id FROM file_asset WHERE sha256 = :sha"),
        {"sha": sha256},
    ).scalar()

    return str(result) if result else None
