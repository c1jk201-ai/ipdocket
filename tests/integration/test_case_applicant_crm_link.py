import uuid


def test_case_detail_links_applicant_to_crm_when_unique_match(
    authenticated_client, sample_user, db_session
):
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterStaffAssignment, VMatterOverview

    # Ensure sample_user is attached to the active session.
    sample_user = db_session.merge(sample_user)

    unique_name = f"Text-{uuid.uuid4().hex[:8]}"
    crm_client = Client(name=unique_name, extra={})
    db_session.add(crm_client)
    db_session.commit()
    crm_client_id = int(crm_client.id)

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref="TEST-APPLICANT-CRM-LINK-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        is_deleted=False,
    )
    db_session.add(matter)
    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=matter.our_ref,
            right_name=matter.right_name,
            right_group=matter.right_group,
            matter_type=matter.matter_type,
            applicants=unique_name,
            clients="",
            attorneys="",
            entered_at="2026-01-22",
        )
    )

    # Permission model: non-admin users may need assignment to view.
    if not (sample_user.staff_party_id or "").strip():
        sample_user.staff_party_id = "test_staff_01"
        db_session.add(sample_user)
    if not MatterStaffAssignment.query.filter_by(
        matter_id=matter_id, staff_party_id=sample_user.staff_party_id
    ).first():
        db_session.add(
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=sample_user.staff_party_id,
                staff_role_code="attorney",
            )
        )

    db_session.commit()

    resp = authenticated_client.get(f"/case/{matter_id}")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    assert unique_name in html
    assert f"/crm/clients/{crm_client_id}" in html
