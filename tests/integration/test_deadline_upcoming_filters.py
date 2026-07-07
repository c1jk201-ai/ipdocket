from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.docket import DocketItem
from app.models.matter import Matter


def test_deadline_upcoming_excludes_soft_deleted_rows_and_deleted_matters(admin_client, db_session):
    today = date.today()
    due = (today + timedelta(days=3)).isoformat()

    mid_active = uuid.uuid4().hex
    mid_deleted = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(
                matter_id=mid_active,
                our_ref="TEST-UPCOMING-ACTIVE",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
                is_deleted=False,
            ),
            Matter(
                matter_id=mid_deleted,
                our_ref="TEST-UPCOMING-DELETED",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
                is_deleted=True,
            ),
        ]
    )

    did_active = uuid.uuid4().hex
    did_soft_deleted = uuid.uuid4().hex
    did_deleted_matter = uuid.uuid4().hex
    db_session.add_all(
        [
            DocketItem(
                docket_id=did_active,
                matter_id=mid_active,
                category="WORK",
                name_free="ACTIVE",
                due_date=due,
                done_date=None,
                is_deleted=False,
            ),
            DocketItem(
                docket_id=did_soft_deleted,
                matter_id=mid_active,
                category="WORK",
                name_free="SOFT-DELETED",
                due_date=due,
                done_date=None,
                is_deleted=True,
            ),
            DocketItem(
                docket_id=did_deleted_matter,
                matter_id=mid_deleted,
                category="WORK",
                name_free="DELETED-MATTER",
                due_date=due,
                done_date=None,
                is_deleted=False,
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get("/deadline/api/upcoming?days=30")
    assert resp.status_code == 200
    payload = resp.get_json() or {}

    ids = {
        item.get("id")
        for section in ("mgmt", "work")
        for item in (payload.get(section) or [])
        if isinstance(item, dict)
    }
    assert did_active in ids
    assert did_soft_deleted not in ids
    assert did_deleted_matter not in ids
