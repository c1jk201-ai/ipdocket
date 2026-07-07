from __future__ import annotations

import uuid

from bs4 import BeautifulSoup


def _create_staffed_matter(db_session, sample_user) -> str:
    from app.models.ip_records import Matter, MatterCustomField, MatterStaffAssignment, VMatterOverview

    user = db_session.merge(sample_user)
    staff_pid = (getattr(user, "staff_party_id", None) or "").strip()
    if not staff_pid:
        staff_pid = f"TEST-STAFF-{uuid.uuid4().hex[:8]}"
        user.staff_party_id = staff_pid
        db_session.add(user)

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-BASIC-{uuid.uuid4().hex[:8]}"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            inhouse_status="Text",
            status_red="",
            status_red_related_date="",
            status_blue="",
            is_deleted=False,
        )
    )
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="PATENT",
            applicants="TextA",
            clients="TextA",
            attorneys="TextA",
            entered_at="2026-01-01",
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="basic",
            data={
                "manager": "TextA",
                "attorney": "TextA",
                "handler": "TextA",
            },
        )
    )
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )
    db_session.commit()
    return matter_id


def test_matter_create_basic_info_includes_optional_handler(authenticated_client) -> None:
    resp = authenticated_client.get("/case/matter/createNewdivision=DOM&type=PATENT")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    handler_input = soup.select_one('input#staff-handler-basic[name="handler"]')

    assert handler_input is not None
    assert not handler_input.has_attr("required")


def test_matter_edit_basic_info_includes_optional_handler(
    authenticated_client, db_session, sample_user
) -> None:
    matter_id = _create_staffed_matter(db_session, sample_user)

    resp = authenticated_client.get(f"/case/matter/{matter_id}/edit")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    handler_input = soup.select_one('input#staff-handler-edit-basic[name="handler"]')

    assert handler_input is not None
    assert handler_input.get("value") == "TextA"
    assert not handler_input.has_attr("required")


def test_case_detail_basic_info_shows_handler(
    authenticated_client, db_session, sample_user
) -> None:
    matter_id = _create_staffed_matter(db_session, sample_user)

    resp = authenticated_client.get(f"/case/{matter_id}")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    basic_table = soup.select_one("#sec-basic .case-basic-table")

    assert basic_table is not None
    basic_text = basic_table.get_text(" ", strip=True)
    assert "Text" in basic_text
    assert "TextA" in basic_text


def test_update_basic_matter_info_links_unlinked_selected_staff(app, db_session) -> None:
    from app.models.ip_records import MatterCustomField, MatterStaffAssignment
    from app.models.party import Party, PartyStaff
    from app.models.user import User
    from app.services.case.helpers_staff import _update_basic_matter_info

    user = User(
        username=f"admin_{uuid.uuid4().hex[:8]}",
        display_name="Administrator",
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    matter_id = uuid.uuid4().hex
    _update_basic_matter_info(
        matter_id,
        {
            "attorney_id": str(user.id),
            "attorney": "Administrator",
            "manager_id": str(user.id),
            "manager": "Administrator",
        },
    )
    db_session.commit()

    saved_user = db_session.get(User, user.id)
    assert saved_user is not None
    assert saved_user.staff_party_id

    party = db_session.get(Party, saved_user.staff_party_id)
    staff = db_session.get(PartyStaff, saved_user.staff_party_id)
    assert party is not None
    assert party.party_kind == "staff"
    assert party.name_display == "Administrator"
    assert staff is not None
    assert staff.active == 1
    assert staff.staff_code == saved_user.username

    basic = MatterCustomField.query.filter_by(matter_id=matter_id, namespace="basic").one()
    assert basic.data["attorney"] == "Administrator"
    assert basic.data["manager"] == "Administrator"

    assignments = {
        row.staff_role_code: row.staff_party_id
        for row in MatterStaffAssignment.query.filter_by(matter_id=matter_id).all()
    }
    assert assignments == {
        "attorney": saved_user.staff_party_id,
        "manager": saved_user.staff_party_id,
    }
