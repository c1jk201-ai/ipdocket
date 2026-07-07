from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.docket import DocketItem
from app.models.matter import Matter, MatterStaffAssignment


def test_deadlines_api_is_scoped_to_accessible_matters(
    authenticated_client, sample_user, db_session
):
    staff_pid = "S1"
    sample_user.staff_party_id = staff_pid
    db_session.add(sample_user)

    today = date.today()
    due = today.isoformat()

    mid_allowed = uuid.uuid4().hex
    mid_denied = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(
                matter_id=mid_allowed,
                our_ref="TEST-ALLOWED",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
            ),
            Matter(
                matter_id=mid_denied,
                our_ref="TEST-DENIED",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
            ),
            MatterStaffAssignment(
                msa_id=uuid.uuid4().hex,
                matter_id=mid_allowed,
                staff_party_id=staff_pid,
                staff_role_code="manager",
                raw_text="",
            ),
        ]
    )

    did_allowed = uuid.uuid4().hex
    did_denied = uuid.uuid4().hex
    db_session.add_all(
        [
            DocketItem(
                docket_id=did_allowed,
                matter_id=mid_allowed,
                category="WORK",
                name_free="ALLOWED",
                due_date=due,
                done_date=None,
                owner_staff_party_id=staff_pid,
                is_deleted=False,
            ),
            DocketItem(
                docket_id=did_denied,
                matter_id=mid_denied,
                category="WORK",
                name_free="DENIED",
                due_date=due,
                done_date=None,
                owner_staff_party_id=staff_pid,
                is_deleted=False,
            ),
        ]
    )
    db_session.commit()

    res = authenticated_client.get("/deadline/api/deadlinesNewfilter=todo&include_done=1")
    assert res.status_code == 200
    items = res.get_json()
    assert isinstance(items, list)

    ids = {it.get("id") for it in items if isinstance(it, dict)}
    assert did_allowed in ids
    assert did_denied not in ids


def test_deadline_events_api_is_scoped_to_accessible_matters(
    authenticated_client, sample_user, db_session
):
    staff_pid = "S1"
    sample_user.staff_party_id = staff_pid
    db_session.add(sample_user)

    today = date.today()
    start = (today - timedelta(days=1)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    mid_allowed = uuid.uuid4().hex
    mid_denied = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(
                matter_id=mid_allowed,
                our_ref="TEST-ALLOWED-EVENTS",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
            ),
            Matter(
                matter_id=mid_denied,
                our_ref="TEST-DENIED-EVENTS",
                right_name="Text Text",
                status_red="",
                status_blue="Text",
            ),
            MatterStaffAssignment(
                msa_id=uuid.uuid4().hex,
                matter_id=mid_allowed,
                staff_party_id=staff_pid,
                staff_role_code="manager",
                raw_text="",
            ),
        ]
    )

    did_allowed = uuid.uuid4().hex
    did_denied = uuid.uuid4().hex
    db_session.add_all(
        [
            DocketItem(
                docket_id=did_allowed,
                matter_id=mid_allowed,
                category="WORK",
                name_free="ALLOWED",
                due_date=today.isoformat(),
                done_date=None,
                owner_staff_party_id=staff_pid,
                is_deleted=False,
            ),
            DocketItem(
                docket_id=did_denied,
                matter_id=mid_denied,
                category="WORK",
                name_free="DENIED",
                due_date=today.isoformat(),
                done_date=None,
                owner_staff_party_id=staff_pid,
                is_deleted=False,
            ),
        ]
    )
    db_session.commit()

    res = authenticated_client.get(f"/deadline/api/eventsNewstart={start}&end={end}")
    assert res.status_code == 200
    events = res.get_json()
    assert isinstance(events, list)

    ids = {it.get("docket_id") for it in events if isinstance(it, dict)}
    assert did_allowed in ids
    assert did_denied not in ids
