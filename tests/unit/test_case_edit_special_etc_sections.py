from __future__ import annotations

import uuid

import pytest
from bs4 import BeautifulSoup


def _ensure_staff_party_id(db_session, sample_user) -> str:
    user = db_session.merge(sample_user)
    staff_pid = (getattr(user, "staff_party_id", None) or "").strip()
    if not staff_pid:
        staff_pid = f"TEST-STAFF-{uuid.uuid4().hex[:8]}"
        user.staff_party_id = staff_pid
        db_session.add(user)
    return staff_pid


def _create_special_etc_matter(
    db_session,
    sample_user,
    *,
    matter_type: str,
    namespace: str,
    right_name: str,
    custom_data: dict,
) -> tuple[str, str]:
    from app.models.ip_records import Matter, MatterCustomField, MatterStaffAssignment, VMatterOverview

    staff_pid = _ensure_staff_party_id(db_session, sample_user)

    matter_id = uuid.uuid4().hex
    suffix_map = {
        "PCT": "PCT",
        "MADRID": "US",
        "HAGUE": "WO",
        "COPYRIGHT": "US",
    }
    our_ref = f"26ET{uuid.uuid4().hex[:4].upper()}{suffix_map.get(matter_type, 'US')}"
    if matter_type == "PCT":
        our_ref = f"26PD{uuid.uuid4().hex[:4].upper()}PCT"

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name=right_name,
            right_group="ETC",
            matter_type=matter_type,
            retained_at="2026-03-11",
            entered_at="2026-03-11",
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
            right_name=right_name,
            right_group="ETC",
            matter_type=matter_type,
            applicants="TextA",
            clients="TextA",
            attorneys="TextA",
            entered_at="2026-03-11",
        )
    )
    db_session.add(
        MatterCustomField(matter_id=matter_id, namespace=namespace, data=dict(custom_data))
    )
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )
    db_session.commit()
    return matter_id, our_ref


@pytest.mark.parametrize(
    ("matter_type", "namespace", "right_name", "custom_data", "heading", "field_checks"),
    [
        (
            "PCT",
            "pct",
            "PCT Text Text",
            {
                "application_no": "PCT/US2026/000123",
                "application_date": "2026-03-10",
                "applicant_name": "TextA",
                "national_phase_countries": "US;US;EP",
            },
            "PCT Registry",
            {
                "application_no": "PCT/US2026/000123",
                "application_date": "2026-03-10",
                "applicant_name": "TextA",
                "national_phase_countries": "US;US;EP",
            },
        ),
        (
            "MADRID",
            "outgoing_trademark",
            "Text Text Text",
            {
                "app_route": "Text",
                "madrid_application_no": "IR123456",
                "madrid_application_date": "2026-03-12",
            },
            "Foreign Trademark Registry",
            {
                "madrid_application_no": "IR123456",
                "madrid_application_date": "2026-03-12",
            },
        ),
        (
            "HAGUE",
            "outgoing_design",
            "Text Text Text",
            {
                "app_route": "HAGUE",
                "hague_application_no": "DM/123456",
                "hague_application_date": "2026-03-13",
            },
            "Foreign Design Registry",
            {
                "hague_application_no": "DM/123456",
                "hague_application_date": "2026-03-13",
            },
        ),
        (
            "COPYRIGHT",
            "misc",
            "Text Text Text",
            {
                "right_type": "Text",
                "case_kind": "Text",
                "application_no": "C-2026-0001",
                "application_date": "2026-03-14",
                "applicant_name": "TextA",
            },
            "Copyright Registry",
            {
                "case_kind": "Text",
                "application_no": "C-2026-0001",
                "application_date": "2026-03-14",
                "applicant_name": "TextA",
            },
        ),
    ],
)
def test_special_etc_edit_page_renders_custom_inputs(
    authenticated_client,
    db_session,
    sample_user,
    matter_type: str,
    namespace: str,
    right_name: str,
    custom_data: dict,
    heading: str,
    field_checks: dict[str, str],
) -> None:
    matter_id, _ = _create_special_etc_matter(
        db_session,
        sample_user,
        matter_type=matter_type,
        namespace=namespace,
        right_name=right_name,
        custom_data=custom_data,
    )

    resp = authenticated_client.get(f"/case/matter/{matter_id}/edit")

    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")

    assert soup.find(string=heading) is not None

    for field_name, expected in field_checks.items():
        input_el = soup.select_one(f'input[name="{field_name}"]')
        if input_el is None:
            input_el = soup.select_one(f'textarea[name="{field_name}"]')
        assert input_el is not None, field_name
        assert input_el.get("value", input_el.text) == expected


@pytest.mark.parametrize(
    ("matter_type", "namespace", "right_name", "initial_data", "update_field", "update_value"),
    [
        (
            "PCT",
            "pct",
            "PCT Text Text",
            {"application_no": "PCT/US2026/000123"},
            "national_phase_countries",
            "US;US;CN",
        ),
        (
            "MADRID",
            "outgoing_trademark",
            "Text Text Text",
            {"app_route": "Text", "madrid_application_no": "IR123456"},
            "madrid_application_no",
            "IR999999",
        ),
        (
            "HAGUE",
            "outgoing_design",
            "Text Text Text",
            {"app_route": "HAGUE", "hague_application_no": "DM/123456"},
            "hague_application_no",
            "DM/999999",
        ),
        (
            "COPYRIGHT",
            "misc",
            "Text Text Text",
            {"right_type": "Text", "case_kind": "Text"},
            "application_no",
            "C-2026-9999",
        ),
    ],
)
def test_special_etc_edit_page_persists_custom_updates(
    admin_client,
    db_session,
    sample_user,
    matter_type: str,
    namespace: str,
    right_name: str,
    initial_data: dict,
    update_field: str,
    update_value: str,
) -> None:
    from app.models.ip_records import MatterCustomField

    matter_id, our_ref = _create_special_etc_matter(
        db_session,
        sample_user,
        matter_type=matter_type,
        namespace=namespace,
        right_name=right_name,
        custom_data=initial_data,
    )

    resp = admin_client.post(
        f"/case/matter/{matter_id}/edit",
        data={
            "idempotency_key": uuid.uuid4().hex,
            "our_ref": our_ref,
            "old_our_ref": "",
            "your_ref": "",
            "right_name": right_name,
            "inhouse_status": "Text",
            "retained_at": "2026-03-11",
            "entered_at": "2026-03-11",
            "memo": "",
            update_field: update_value,
        },
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303)

    db_session.expire_all()
    row = MatterCustomField.query.filter_by(matter_id=matter_id, namespace=namespace).first()
    assert row is not None
    assert (row.data or {}).get(update_field) == update_value
