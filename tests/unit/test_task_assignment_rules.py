from __future__ import annotations

import uuid


def test_resolve_assignees_for_task_fallback_to_all_includes_handler(
    app, db_session, sample_matter, sample_user
):
    from app.models.matter import MatterStaffAssignment
    from app.models.party import Party, PartyStaff
    from app.models.user import User
    from app.utils.task_assignment_rules import resolve_assignees_for_task

    matter_id = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)

    attorney_user = db_session.merge(sample_user)
    attorney_spid = str(getattr(attorney_user, "staff_party_id", None) or "").strip()
    if not attorney_spid:
        attorney_spid = f"sp_test_attorney_{uuid.uuid4().hex[:8]}"
        attorney_user.staff_party_id = attorney_spid
        db_session.add(attorney_user)
        db_session.flush()

    manager_user = User(
        username=f"tas_mgr_{uuid.uuid4().hex[:8]}",
        email=f"tas_mgr_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_test_manager_{uuid.uuid4().hex[:8]}",
    )
    handler_user = User(
        username=f"tas_hdl_{uuid.uuid4().hex[:8]}",
        email=f"tas_hdl_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_test_handler_{uuid.uuid4().hex[:8]}",
    )
    db_session.add_all([manager_user, handler_user])
    db_session.flush()

    for spid, name in (
        (attorney_spid, "Attorney User"),
        (manager_user.staff_party_id, "Manager User"),
        (handler_user.staff_party_id, "Handler User"),
    ):
        if not spid:
            continue
        if db_session.get(Party, spid) is None:
            db_session.add(
                Party(
                    party_id=spid,
                    name_display=name,
                )
            )
        if db_session.get(PartyStaff, spid) is None:
            db_session.add(
                PartyStaff(
                    party_id=spid,
                    active=1,
                )
            )

    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=attorney_spid,
                staff_role_code="attorney",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=manager_user.staff_party_id,
                staff_role_code="manager",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=handler_user.staff_party_id,
                staff_role_code="handler",
            ),
        ]
    )
    db_session.commit()

    db_session.info.pop("_task_assignment_cache", None)
    assignees = resolve_assignees_for_task(
        matter_id=matter_id,
        name_ref="UNMATCHED:ALL_STAFF_TEST",
        name_free="unmatched all staff test",
        category="WORK",
        owner_staff_party_id=None,
        fallback_user_id=None,
        fallback_to_all=True,
    )

    assignee_ids = {int(row.user_id) for row in assignees}
    assert assignee_ids == {
        int(attorney_user.id),
        int(manager_user.id),
        int(handler_user.id),
    }


def test_resolve_assignees_for_task_status_red_role_set_excludes_handler(
    app, db_session, sample_matter, sample_user
):
    from app.models.matter import MatterStaffAssignment
    from app.models.party import Party, PartyStaff
    from app.models.user import User
    from app.utils.task_assignment_rules import resolve_assignees_for_task

    matter_id = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)

    attorney_user = db_session.merge(sample_user)
    attorney_spid = str(getattr(attorney_user, "staff_party_id", None) or "").strip()
    if not attorney_spid:
        attorney_spid = f"sp_test_attorney_{uuid.uuid4().hex[:8]}"
        attorney_user.staff_party_id = attorney_spid
        db_session.add(attorney_user)
        db_session.flush()

    manager_user = User(
        username=f"tas_rule_mgr_{uuid.uuid4().hex[:8]}",
        email=f"tas_rule_mgr_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_rule_manager_{uuid.uuid4().hex[:8]}",
    )
    handler_user = User(
        username=f"tas_rule_hdl_{uuid.uuid4().hex[:8]}",
        email=f"tas_rule_hdl_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_rule_handler_{uuid.uuid4().hex[:8]}",
    )
    db_session.add_all([manager_user, handler_user])
    db_session.flush()

    for spid, name in (
        (attorney_spid, "Attorney User"),
        (manager_user.staff_party_id, "Manager User"),
        (handler_user.staff_party_id, "Handler User"),
    ):
        if not spid:
            continue
        if db_session.get(Party, spid) is None:
            db_session.add(
                Party(
                    party_id=spid,
                    name_display=name,
                )
            )
        if db_session.get(PartyStaff, spid) is None:
            db_session.add(
                PartyStaff(
                    party_id=spid,
                    active=1,
                )
            )

    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=attorney_spid,
                staff_role_code="attorney",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=manager_user.staff_party_id,
                staff_role_code="manager",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=handler_user.staff_party_id,
                staff_role_code="handler",
            ),
        ]
    )
    db_session.commit()

    db_session.info.pop("_task_assignment_cache", None)
    assignees = resolve_assignees_for_task(
        matter_id=matter_id,
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        category="MGMT",
        owner_staff_party_id=None,
        fallback_user_id=None,
        fallback_to_all=False,
    )

    assignee_ids = {int(row.user_id) for row in assignees}
    assert assignee_ids == {
        int(attorney_user.id),
        int(manager_user.id),
    }


def test_resolve_assignees_from_assignment_without_party_tables(app, db_session, sample_matter):
    from app.models.matter import MatterStaffAssignment
    from app.models.user import User
    from app.utils.task_assignment_rules import _resolve_assignees_for_matter

    matter_id = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)

    manager_user = User(
        username=f"tas_no_party_mgr_{uuid.uuid4().hex[:8]}",
        email=f"tas_no_party_mgr_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_no_party_mgr_{uuid.uuid4().hex[:8]}",
    )
    db_session.add(manager_user)
    db_session.flush()

    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=manager_user.staff_party_id,
            staff_role_code="manager",
        )
    )
    db_session.commit()

    db_session.info.pop("_task_assignment_cache", None)
    rows = _resolve_assignees_for_matter(
        matter_id=matter_id,
        role_codes=("manager", "mgmt"),
    )

    assert {row.user_id for row in rows} == {int(manager_user.id)}


def test_flat_index_fallback_excludes_inactive_users(monkeypatch, app, db_session):
    from app.models.case_flat_index import CaseFlatIndex
    from app.models.user import User
    from app.utils import task_assignment_rules as tar

    matter_id = f"M-INACTIVE-{uuid.uuid4().hex[:8]}"
    active_user = User(
        username=f"tas_active_{uuid.uuid4().hex[:8]}",
        email=f"tas_active_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"sp_active_{uuid.uuid4().hex[:8]}",
    )
    inactive_user = User(
        username=f"tas_inactive_{uuid.uuid4().hex[:8]}",
        email=f"tas_inactive_{uuid.uuid4().hex[:8]}@example.com",
        role="patent_staff",
        is_active=False,
        staff_party_id=f"sp_inactive_{uuid.uuid4().hex[:8]}",
    )
    db_session.add_all([active_user, inactive_user])
    db_session.flush()

    db_session.add(
        CaseFlatIndex(
            matter_id=matter_id,
            manager_id=str(inactive_user.id),
            attorney_id=str(active_user.id),
        )
    )
    db_session.commit()

    monkeypatch.setattr(tar, "_fetch_assignees_from_assignment", lambda **kwargs: [])
    db_session.info.pop("_task_assignment_cache", None)
    rows = tar._resolve_assignees_for_matter(
        matter_id=matter_id,
        role_codes=("manager", "attorney"),
    )

    assert {row.user_id for row in rows} == {int(active_user.id)}
