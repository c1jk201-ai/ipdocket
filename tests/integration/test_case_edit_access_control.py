from __future__ import annotations

import uuid


def test_case_edit_get_requires_edit_permission_for_view_only_user(
    authenticated_client, sample_user, db_session
):
    """
    Regression:
    - /case/matter/<id>/edit must require edit_case permission even on GET.
    - View-only access (team assignment) should still allow /case/<id> but deny edit form.
    """
    from app.models.party import PartyStaff
    from app.models.ip_records import Matter, MatterStaffAssignment, VMatterOverview

    sample_user = db_session.merge(sample_user)
    if not (sample_user.department or "").strip():
        sample_user.department = f"dept_{uuid.uuid4().hex[:6]}"
        db_session.add(sample_user)
        db_session.flush()

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-EDIT-GET-403-{matter_id[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Edit permission GET security test",
            right_group="DOM",
            matter_type="PATENT",
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
            right_name="Edit permission GET security test",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )

    # Team assignment: some staff in the same dept is assigned to the matter.
    team_staff_pid = f"party_{uuid.uuid4().hex[:8]}"
    db_session.add(PartyStaff(party_id=team_staff_pid, dept=sample_user.department, active=1))
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=team_staff_pid,
            staff_role_code="attorney",
        )
    )
    db_session.commit()

    resp_view = authenticated_client.get(f"/case/{matter_id}")
    assert resp_view.status_code == 200

    resp_edit = authenticated_client.get(f"/case/matter/{matter_id}/edit")
    assert resp_edit.status_code == 403


def test_case_detail_shows_workflow_assign_for_assign_only_role(client, db_session):
    from app.models.ip_records import Matter, VMatterOverview
    from app.models.role import Role
    from app.models.user import User
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    matter_id = uuid.uuid4().hex
    our_ref = f"TEST-ASSIGN-ONLY-{matter_id[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=our_ref,
            right_name="Assign-only workflow test",
            right_group="DOM",
            matter_type="PATENT",
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
            right_name="Assign-only workflow test",
            right_group="DOM",
            matter_type="PATENT",
            applicants="",
            clients="",
            attorneys="",
            entered_at="2026-01-01",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"assign_only_{suffix}",
        description="view+assign only role for case detail gate test",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    user = User(
        username=f"assign_only_{suffix}",
        email=f"assign_only_{suffix}@example.com",
        role="partner_attorney",
        is_active=True,
    )
    user.roles = [role]
    db_session.add_all([role, user])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = user.id
        session["_fresh"] = True

    resp = client.get(f"/case/{matter_id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-can-assign-staff="1"' in html
    assert "Text" in html
