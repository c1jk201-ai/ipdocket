import re
from datetime import date
from urllib.parse import urlparse


def test_case_copy_copies_people_but_skips_matching_data(
    authenticated_client, db_session, sample_user
):
    from app.models.ip_records import Matter, MatterCustomField, MatterIdentifier, MatterStaffAssignment

    user = db_session.merge(sample_user)
    staff_pid = (getattr(user, "staff_party_id", None) or "").strip()
    if not staff_pid:
        staff_pid = "TEST-STAFF-COPY-01"
        user.staff_party_id = staff_pid
        db_session.add(user)
        db_session.flush()

    yy = date.today().strftime("%y")
    source = Matter(
        matter_id="copy-source-000000000000000000000001",
        our_ref=f"{yy}PD0001US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        retained_at="2026-01-15",
        entered_at="2026-01-15",
    )
    db_session.add(source)
    db_session.flush()

    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="domestic_patent",
            data={
                "proposal_title": "Text Text",
                "application_country": "US",
                "filing_type": "Text",
                "applicant_name": "Text; Example Corp",
                "application_no": "10-2026-000001",
                "registration_no": "40-2026-000001",
                "application_date": "2026-01-20",
            },
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="basic",
            data={
                "client_name": "Text",
                "client_id": "101",
                "manager": "Manager A",
                "attorney": "Attorney B",
                "handler": "Handler C",
            },
        )
    )

    db_session.add(
        MatterStaffAssignment(
            matter_id=str(source.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="attorney",
            raw_text="Attorney B",
        )
    )
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(source.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="manager",
            raw_text="Manager A",
        )
    )

    db_session.add(
        MatterIdentifier(
            matter_id=str(source.matter_id),
            id_type="Text",
            id_value="10-2026-000001",
        )
    )
    db_session.commit()

    response = authenticated_client.post(f"/case/{source.matter_id}/copy", follow_redirects=False)
    assert response.status_code in (302, 303)

    location = response.headers.get("Location") or ""
    new_path = urlparse(location).path
    assert re.match(r"^/case/[0-9a-f]{32}$", new_path)
    new_matter_id = new_path.rsplit("/", 1)[-1]

    copied = Matter.query.get(new_matter_id)
    assert copied is not None
    assert copied.matter_id != source.matter_id
    assert copied.our_ref != source.our_ref
    assert copied.our_ref.startswith(source.our_ref)
    assert re.search(r"\(\d+\)$", copied.our_ref)
    assert copied.right_group == "DOM"
    assert copied.matter_type == "PATENT"
    assert copied.right_name == "Text Text"

    copied_registry = MatterCustomField.query.filter_by(
        matter_id=new_matter_id, namespace="domestic_patent"
    ).first()
    assert copied_registry is not None
    copied_registry_data = copied_registry.data or {}
    assert copied_registry_data.get("proposal_title") == "Text Text"
    assert copied_registry_data.get("application_country") == "US"
    assert copied_registry_data.get("applicant_name") == "Text; Example Corp"
    assert copied_registry_data.get("client_name") == "Text"
    assert copied_registry_data.get("manager") == "Manager A"
    assert copied_registry_data.get("attorney") == "Attorney B"
    assert "application_no" not in copied_registry_data
    assert "registration_no" not in copied_registry_data
    assert "application_date" not in copied_registry_data

    copied_basic = MatterCustomField.query.filter_by(
        matter_id=new_matter_id, namespace="basic"
    ).first()
    assert copied_basic is not None
    copied_basic_data = copied_basic.data or {}
    assert copied_basic_data.get("client_name") == "Text"
    assert copied_basic_data.get("client_id") == "101"
    assert copied_basic_data.get("manager") == "Manager A"
    assert copied_basic_data.get("attorney") == "Attorney B"
    assert copied_basic_data.get("handler") == "Handler C"

    copied_assignments = MatterStaffAssignment.query.filter_by(matter_id=new_matter_id).all()
    copied_role_pairs = {(row.staff_role_code, row.staff_party_id) for row in copied_assignments}
    assert ("attorney", staff_pid) in copied_role_pairs
    assert ("manager", staff_pid) in copied_role_pairs

    assert MatterIdentifier.query.filter_by(matter_id=new_matter_id).count() == 0
