from __future__ import annotations

import uuid
from datetime import datetime

from app.services.case.helpers_files import _load_linked_file_asset


def test_load_linked_file_asset_returns_none_for_unlinked_preview(app, db_session):
    from app.models.ip_records import FileAsset, Matter

    matter = Matter(our_ref="26TM0001US")
    db_session.add(matter)

    fid = uuid.uuid4().hex
    db_session.add(
        FileAsset(
            file_asset_id=fid,
            file_path=f"unit/{fid}.png",
            original_name="preview.png",
            sha256=uuid.uuid4().hex,
            byte_size=1,
            mime_type="image/png",
            created_at=datetime.utcnow().isoformat(),
        )
    )
    db_session.commit()

    asset = _load_linked_file_asset(
        matter_id=str(matter.matter_id),
        file_asset_id=fid,
        strict_link=False,
    )

    assert asset is None


def test_load_linked_file_asset_stays_strict_by_default(app, db_session):
    from werkzeug.exceptions import NotFound

    from app.models.ip_records import FileAsset, Matter

    matter = Matter(our_ref="26TM0002US")
    db_session.add(matter)

    fid = uuid.uuid4().hex
    db_session.add(
        FileAsset(
            file_asset_id=fid,
            file_path=f"unit/{fid}.png",
            original_name="preview.png",
            sha256=uuid.uuid4().hex,
            byte_size=1,
            mime_type="image/png",
            created_at=datetime.utcnow().isoformat(),
        )
    )
    db_session.commit()

    try:
        _load_linked_file_asset(matter_id=str(matter.matter_id), file_asset_id=fid)
    except NotFound:
        return

    raise AssertionError("Expected NotFound for unlinked file asset")
