from __future__ import annotations

import uuid
from datetime import datetime


def _seed_matter_file(db_session, *, role: str = "internal", parent_id: str | None = None):
    from app.models.ip_records import FileAsset, Matter, MatterFileAsset

    matter_id = uuid.uuid4().hex
    file_asset_id = uuid.uuid4().hex
    matter_file_id = uuid.uuid4().hex

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=f"TEST-FM-{matter_id[:8]}",
            right_name="FM compatibility test",
            right_group="DOM",
            matter_type="PATENT",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        FileAsset(
            file_asset_id=file_asset_id,
            storage_type="local",
            file_path=f"fm/{file_asset_id}.txt",
            original_name="compat.txt",
            sha256=uuid.uuid4().hex,
            byte_size=10,
            mime_type="text/plain",
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            is_deleted=False,
        )
    )
    db_session.add(
        MatterFileAsset(
            matter_file_id=matter_file_id,
            matter_id=matter_id,
            file_asset_id=file_asset_id,
            role=role,
            parent_id=parent_id,
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            is_deleted=False,
        )
    )
    db_session.commit()
    return matter_id, matter_file_id


def test_move_fm_item_accepts_matter_file_id_and_target_role(admin_client, db_session, monkeypatch):
    from app.blueprints.case.views import file_manager as fm_view
    from app.models.ip_records import MatterFileAsset

    monkeypatch.setattr(fm_view, "record_case_audit", lambda **kwargs: None)

    parent_id = uuid.uuid4().hex
    matter_id, matter_file_id = _seed_matter_file(
        db_session,
        role="internal",
        parent_id=parent_id,
    )

    response = admin_client.post(
        f"/case/{matter_id}/fm/move",
        data={
            "matter_file_id": matter_file_id,
            "target_role": "submission",
            "current_folder_id": parent_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers.get("Location", "")
    assert "fm_folder_id=" in location
    assert f"fm_folder_id={parent_id}" in location
    assert location.endswith("#sec-files")

    updated = db_session.get(MatterFileAsset, matter_file_id)
    assert updated is not None
    assert updated.role == "submission"
    assert updated.parent_id == parent_id


def test_delete_fm_item_accepts_matter_file_id_and_current_folder_id(
    admin_client, db_session, monkeypatch
):
    from app.blueprints.case.views import file_manager as fm_view
    from app.models.ip_records import MatterFileAsset

    monkeypatch.setattr(fm_view, "record_case_audit", lambda **kwargs: None)

    parent_id = uuid.uuid4().hex
    matter_id, matter_file_id = _seed_matter_file(
        db_session,
        role="internal",
        parent_id=parent_id,
    )

    response = admin_client.post(
        f"/case/{matter_id}/fm/delete",
        data={
            "matter_file_id": matter_file_id,
            "current_folder_id": parent_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers.get("Location", "")
    assert "fm_folder_id=" in location
    assert f"fm_folder_id={parent_id}" in location
    assert location.endswith("#sec-files")
    assert db_session.get(MatterFileAsset, matter_file_id) is None


def test_move_fm_item_keeps_item_id_new_parent_id_flow(admin_client, db_session, monkeypatch):
    from app.blueprints.case.views import file_manager as fm_view
    from app.models.ip_records import FileAsset, MatterFileAsset

    monkeypatch.setattr(fm_view, "record_case_audit", lambda **kwargs: None)

    matter_id, matter_file_id = _seed_matter_file(
        db_session,
        role="internal",
        parent_id=None,
    )

    folder_file_asset_id = uuid.uuid4().hex
    folder_id = uuid.uuid4().hex
    db_session.add(
        FileAsset(
            file_asset_id=folder_file_asset_id,
            storage_type="folder",
            file_path=f"fm/{folder_file_asset_id}",
            original_name="folder",
            sha256=uuid.uuid4().hex,
            byte_size=0,
            mime_type="inode/directory",
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            is_deleted=False,
        )
    )
    db_session.add(
        MatterFileAsset(
            matter_file_id=folder_id,
            matter_id=matter_id,
            file_asset_id=folder_file_asset_id,
            role="folder",
            parent_id=None,
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            is_deleted=False,
        )
    )
    db_session.commit()

    response = admin_client.post(
        f"/case/{matter_id}/fm/move",
        data={
            "item_id": matter_file_id,
            "new_parent_id": folder_id,
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers.get("Location", "")
    assert f"fm_folder_id={folder_id}" in location
    assert location.endswith("#sec-files")

    updated = db_session.get(MatterFileAsset, matter_file_id)
    assert updated is not None
    assert updated.parent_id == folder_id
