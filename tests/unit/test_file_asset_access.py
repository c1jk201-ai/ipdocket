from __future__ import annotations

import uuid
from datetime import datetime

import pytest


def _make_file_asset(*, file_asset_id: str) -> dict:
    # Keep sha256 unique per asset to satisfy unique constraint.
    sha = uuid.uuid4().hex
    return {
        "file_asset_id": file_asset_id,
        "file_path": f"unit/{sha}.bin",
        "original_name": "unit.bin",
        "sha256": sha,
        "byte_size": 1,
        "mime_type": "application/octet-stream",
        "created_at": datetime.utcnow().isoformat(),
    }


def test_filter_accessible_file_assets_allows_unlinked_asset(app, db_session):
    from app.models.ip_records import FileAsset
    from app.models.user import User
    from app.services.storage.file_asset_access import filter_accessible_file_assets

    user = User(
        username="fa_user",
        email="fa_user@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=uuid.uuid4().hex,
    )
    db_session.add(user)

    fid = uuid.uuid4().hex
    db_session.add(FileAsset(**_make_file_asset(file_asset_id=fid)))
    db_session.commit()

    assert filter_accessible_file_assets([fid], user=user) == [fid]


def test_filter_accessible_file_assets_rejects_missing_asset(app, db_session):
    from app.models.user import User
    from app.services.storage.file_asset_access import filter_accessible_file_assets

    user = User(
        username="fa_user2",
        email="fa_user2@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=uuid.uuid4().hex,
    )
    db_session.add(user)
    db_session.commit()

    with pytest.raises(ValueError):
        filter_accessible_file_assets([uuid.uuid4().hex], user=user)


def test_filter_accessible_file_assets_blocks_cross_matter_link(app, db_session):
    from app.models.ip_records import FileAsset, Matter, MatterFileAsset, MatterStaffAssignment
    from app.models.user import User
    from app.services.storage.file_asset_access import filter_accessible_file_assets

    staff_pid = uuid.uuid4().hex
    user = User(
        username="fa_user3",
        email="fa_user3@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=staff_pid,
    )
    db_session.add(user)

    target = Matter(our_ref="26PD0001US")
    other = Matter(our_ref="26PD0002US")
    db_session.add_all([target, other])
    db_session.commit()

    # Give the user access to *target* only.
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(target.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )

    fid = uuid.uuid4().hex
    db_session.add(FileAsset(**_make_file_asset(file_asset_id=fid)))
    # Link the asset to the inaccessible matter.
    db_session.add(
        MatterFileAsset(
            matter_file_id=uuid.uuid4().hex,
            matter_id=str(other.matter_id),
            file_asset_id=fid,
            role="application",
            created_at=datetime.utcnow().isoformat(),
        )
    )
    db_session.commit()

    with pytest.raises(PermissionError):
        filter_accessible_file_assets([fid], user=user)


def test_file_asset_access_service_rejects_forged_matter_file_pair(app, db_session):
    from app.models.ip_records import FileAsset, Matter, MatterFileAsset, MatterStaffAssignment
    from app.models.user import User
    from app.services.storage.file_asset_access import FileAssetAccessService

    staff_pid = uuid.uuid4().hex
    user = User(
        username="fa_user4",
        email="fa_user4@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=staff_pid,
    )
    target = Matter(our_ref="26PD0003US")
    other = Matter(our_ref="26PD0004US")
    db_session.add_all([user, target, other])
    db_session.commit()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(target.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )

    fid = uuid.uuid4().hex
    db_session.add(FileAsset(**_make_file_asset(file_asset_id=fid)))
    db_session.add(
        MatterFileAsset(
            matter_file_id=uuid.uuid4().hex,
            matter_id=str(other.matter_id),
            file_asset_id=fid,
            role="attachment",
            created_at=datetime.utcnow().isoformat(),
        )
    )
    db_session.commit()

    assert FileAssetAccessService.can_read(user, str(target.matter_id), fid) is False
    with pytest.raises(PermissionError):
        FileAssetAccessService.authorize_read(user, str(target.matter_id), fid)


def test_file_asset_access_service_blocks_deleted_file(app, db_session):
    from app.models.ip_records import FileAsset, Matter, MatterFileAsset, MatterStaffAssignment
    from app.models.user import User
    from app.services.storage.file_asset_access import FileAssetAccessService

    staff_pid = uuid.uuid4().hex
    user = User(
        username="fa_user5",
        email="fa_user5@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=staff_pid,
    )
    matter = Matter(our_ref="26PD0005US")
    db_session.add_all([user, matter])
    db_session.commit()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(matter.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )

    fid = uuid.uuid4().hex
    asset_data = _make_file_asset(file_asset_id=fid)
    asset_data["is_deleted"] = True
    db_session.add(FileAsset(**asset_data))
    db_session.add(
        MatterFileAsset(
            matter_file_id=uuid.uuid4().hex,
            matter_id=str(matter.matter_id),
            file_asset_id=fid,
            role="attachment",
            created_at=datetime.utcnow().isoformat(),
        )
    )
    db_session.commit()

    assert FileAssetAccessService.can_read(user, str(matter.matter_id), fid) is False
    with pytest.raises(FileNotFoundError):
        FileAssetAccessService.authorize_read(user, str(matter.matter_id), fid)
