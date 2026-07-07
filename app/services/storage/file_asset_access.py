"""FileAsset access and linking guardrails.

This module centralizes logic for determining whether a user may attach/link an
existing FileAsset to another object (e.g. via uploads or extracted-params flows).

Security model:
- If a FileAsset is already linked to one or more matters, the user must be able
  to *view* all those matters to re-use the FileAsset.
- If a FileAsset has no links (staged/orphaned), it is allowed to be linked.

Rationale:
- Prevents cross-matter leakage via shared SHA256 deduped FileAsset rows.
- Keeps behavior consistent across multiple upload/confirmation entrypoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from app.extensions import db
from app.models.ip_records import FileAsset, Matter
from app.services.storage.file_asset_scan_service import (
    assert_file_asset_scan_allows_read,
    file_asset_scan_allows_read,
)
from app.services.storage.file_asset_service import get_file_asset_service
from app.utils.permissions import can_access_matter
from app.utils.policy_sql import policy_text as text


@dataclass(frozen=True)
class AuthorizedFileAsset:
    file_asset_id: str
    file_path: str
    original_name: str | None
    mime_type: str | None
    storage_type: str | None
    virus_scan_status: str | None


class FileAssetAccessService:
    """Central file read policy for Matter-linked FileAsset rows."""

    @staticmethod
    def _matter_exists(matter_id: str) -> bool:
        mid = (matter_id or "").strip()
        if not mid:
            return False
        q = Matter.query.filter(Matter.matter_id == mid)
        if hasattr(Matter, "is_deleted"):
            q = q.filter((Matter.is_deleted.is_(False)) | (Matter.is_deleted.is_(None)))
        return bool(q.first())

    @staticmethod
    def _file_asset_row(file_asset_id: str) -> AuthorizedFileAsset | None:
        fid = (file_asset_id or "").strip()
        if not fid:
            return None
        row = db.session.execute(
            text(
                """
                SELECT file_asset_id,
                       file_path,
                       original_name,
                       mime_type,
                       storage_type,
                       virus_scan_status
                FROM file_asset
                WHERE file_asset_id = :fid
                  AND COALESCE(is_deleted, false) = false
                """
            ).execution_options(policy_bypass=True),
            {"fid": fid},
        ).fetchone()
        if not row:
            return None
        return AuthorizedFileAsset(
            file_asset_id=str(row[0]),
            file_path=str(row[1] or ""),
            original_name=row[2],
            mime_type=row[3],
            storage_type=row[4],
            virus_scan_status=row[5],
        )

    @staticmethod
    def is_linked_to_matter(*, matter_id: str, file_asset_id: str) -> bool:
        mid = (matter_id or "").strip()
        fid = (file_asset_id or "").strip()
        if not mid or not fid:
            return False
        linked = db.session.execute(
            text(
                """
                SELECT 1
                FROM matter_file_asset mfa
                WHERE mfa.matter_id = :mid
                  AND mfa.file_asset_id = :fid
                  AND COALESCE(mfa.is_deleted, false) = false
                UNION
                SELECT 1
                FROM communication_file_asset cfa
                JOIN communication c ON c.comm_id = cfa.comm_id
                WHERE c.matter_id = :mid
                  AND cfa.file_asset_id = :fid
                  AND COALESCE(cfa.is_deleted, false) = false
                UNION
                SELECT 1
                FROM office_action_file_asset oafa
                JOIN office_action oa ON oa.oa_id = oafa.oa_id
                WHERE oa.matter_id = :mid
                  AND oafa.file_asset_id = :fid
                  AND COALESCE(oafa.is_deleted, false) = false
                UNION
                SELECT 1
                FROM matter_memo_file_asset mmfa
                JOIN matter_memo mm ON mm.id = mmfa.memo_id
                WHERE mm.matter_id = :mid
                  AND mmfa.file_asset_id = :fid
                  AND COALESCE(mmfa.is_deleted, false) = false
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid, "fid": fid},
        ).scalar()
        return bool(linked)

    @classmethod
    def can_read(cls, user, matter_id: str, file_asset_id: str) -> bool:
        mid = (matter_id or "").strip()
        fid = (file_asset_id or "").strip()
        if not mid or not fid:
            return False
        if not cls._matter_exists(mid):
            return False
        asset = cls._file_asset_row(fid)
        if asset is None:
            return False
        if not file_asset_scan_allows_read(asset.virus_scan_status):
            return False
        if not can_access_matter(user, mid, action="view"):
            return False
        return cls.is_linked_to_matter(matter_id=mid, file_asset_id=fid)

    @classmethod
    def can_read_matter(cls, user, matter_id: str) -> bool:
        mid = (matter_id or "").strip()
        if not mid:
            return False
        return cls._matter_exists(mid) and can_access_matter(user, mid, action="view")

    @classmethod
    def authorize_read(
        cls,
        user,
        matter_id: str,
        file_asset_id: str,
    ) -> AuthorizedFileAsset:
        mid = (matter_id or "").strip()
        fid = (file_asset_id or "").strip()
        if not mid or not fid or not cls._matter_exists(mid):
            raise FileNotFoundError("matter_not_found")

        asset = cls._file_asset_row(fid)
        if asset is None:
            raise FileNotFoundError("file_asset_not_found")

        if not can_access_matter(user, mid, action="view"):
            raise PermissionError("matter_access_denied")
        if not cls.is_linked_to_matter(matter_id=mid, file_asset_id=fid):
            raise PermissionError("file_asset_not_linked_to_matter")
        assert_file_asset_scan_allows_read(asset.virus_scan_status)
        return asset

    @classmethod
    def open_authorized_stream(
        cls,
        user,
        matter_id: str,
        file_asset_id: str,
    ) -> tuple[AuthorizedFileAsset, BinaryIO]:
        asset = cls.authorize_read(user, matter_id, file_asset_id)
        return asset, get_file_asset_service().open_stream(asset.file_asset_id)


def linked_matter_ids_for_file_asset(file_asset_id: str) -> list[str]:
    """Return distinct matter_ids that reference the given file_asset_id."""
    if not (file_asset_id or "").strip():
        return []
    rows = (
        db.session.execute(
            text(
                """
            SELECT DISTINCT matter_id FROM matter_file_asset WHERE file_asset_id = :fid
            UNION
            SELECT DISTINCT c.matter_id
            FROM communication c
            JOIN communication_file_asset cfa ON c.comm_id = cfa.comm_id
            WHERE cfa.file_asset_id = :fid
            UNION
            SELECT DISTINCT oa.matter_id
            FROM office_action oa
            JOIN office_action_file_asset oafa ON oa.oa_id = oafa.oa_id
            WHERE oafa.file_asset_id = :fid
            """
            ),
            {"fid": str(file_asset_id)},
        )
        .scalars()
        .all()
    )
    return [str(r) for r in rows if r]


def filter_accessible_file_assets(file_asset_ids: list[str], *, user) -> list[str]:
    """
    Validate that the user may attach/link the given file assets.

    Raises:
      - ValueError("missing_file_asset") if any id does not exist
      - PermissionError("file_asset_access_denied") if any id is linked to an
        inaccessible matter.
    """
    if not file_asset_ids:
        return []

    existing = FileAsset.query.filter(FileAsset.file_asset_id.in_(file_asset_ids)).all()
    existing_ids = {fa.file_asset_id for fa in existing}
    missing = [fid for fid in file_asset_ids if fid not in existing_ids]
    if missing:
        raise ValueError("missing_file_asset")

    allowed: list[str] = []
    for fid in file_asset_ids:
        linked_matter_ids = linked_matter_ids_for_file_asset(fid)
        if not linked_matter_ids:
            # Policy: allow unlinked assets so users can attach staged uploads here.
            allowed.append(fid)
            continue
        if all(can_access_matter(user, mid, action="view") for mid in linked_matter_ids):
            allowed.append(fid)
            continue
        raise PermissionError("file_asset_access_denied")

    return allowed
