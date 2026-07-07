from __future__ import annotations

import uuid
from datetime import date, timedelta


def _add_case_staff(db_session, *, matter_id: str, role_code: str, name: str):
    from app.models.party import Party, PartyStaff
    from app.models.ip_records import MatterStaffAssignment
    from app.models.user import User

    token = uuid.uuid4().hex[:8]
    staff_party_id = f"spid-{role_code}-{token}"
    user_role = "mgmt_staff" if role_code in ("manager", "mgmt") else "patent_staff"

    user = User(
        username=f"user_{role_code}_{token}",
        email=f"user_{role_code}_{token}@example.com",
        display_name=name,
        staff_party_id=staff_party_id,
        role=user_role,
        is_active=True,
    )
    db_session.add(user)
    db_session.add(Party(party_id=staff_party_id, name_display=name))
    db_session.add(PartyStaff(party_id=staff_party_id, staff_code=staff_party_id, active=1))
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=staff_party_id,
            staff_role_code=role_code,
            raw_text=name,
        )
    )
    db_session.flush()
    return user, staff_party_id


def _find_task(tasks: list[dict], *, matter_id: str, task_name: str) -> dict | None:
    return next(
        (
            t
            for t in tasks
            if str(t.get("matter_id") or "") == str(matter_id)
            and str(t.get("task_name") or "") == task_name
        ),
        None,
    )


def test_worklog_owner_manager_only_notice_uses_manager_only(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    manager_user, manager_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="manager",
        name="Text",
    )
    attorney_user, attorney_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="attorney",
        name="Text",
    )
    _handler_user, _handler_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="handler",
        name="Text",
    )

    task_name = "Text Text(3Text Text)"
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="MGMT",
            due_date=date.today() + timedelta(days=3),
            assignee_id=manager_user.id,
            attorney_assignee_id=attorney_user.id,
            inspector_id=manager_user.id,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    tasks = (resp.get_json() or {}).get("tasks") or []
    target = _find_task(tasks, matter_id=matter_id, task_name=task_name)
    assert target is not None

    owner_ids = {
        str(row.get("id") or "").strip()
        for row in (target.get("owners") or [])
        if str(row.get("id") or "").strip()
    }
    assert owner_ids == {manager_spid}

    # owner_role=owner should follow manager-only ownership, not any assigned role.
    resp_owner = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=30&owner_role=owner&owner={manager_spid}"
    )
    assert resp_owner.status_code == 200
    task2 = _find_task(
        (resp_owner.get_json() or {}).get("tasks") or [], matter_id=matter_id, task_name=task_name
    )
    assert task2 is not None

    resp_not_owner = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=30&owner_role=owner&owner={attorney_spid}"
    )
    assert resp_not_owner.status_code == 200
    task3 = _find_task(
        (resp_not_owner.get_json() or {}).get("tasks") or [],
        matter_id=matter_id,
        task_name=task_name,
    )
    assert task3 is None

    summary_owner = admin_client.get(
        f"/worklog/api/summaryNewdays=30&owner_role=owner&owner={manager_spid}"
    )
    assert summary_owner.status_code == 200
    assert int((summary_owner.get_json() or {}).get("pending") or 0) >= 1

    summary_not_owner = admin_client.get(
        f"/worklog/api/summaryNewdays=30&owner_role=owner&owner={attorney_spid}"
    )
    assert summary_not_owner.status_code == 200
    assert int((summary_not_owner.get_json() or {}).get("pending") or 0) == 0


def test_worklog_owner_all_staff_filing_deadline_includes_all_case_staff(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    manager_user, manager_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="manager",
        name="Text",
    )
    attorney_user, attorney_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="attorney",
        name="Text",
    )
    handler_user, handler_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="handler",
        name="Text",
    )

    task_name = "\ucd9c\uc6d0"
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=5),
            assignee_id=handler_user.id,
            attorney_assignee_id=attorney_user.id,
            inspector_id=manager_user.id,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    tasks = (resp.get_json() or {}).get("tasks") or []
    target = _find_task(tasks, matter_id=matter_id, task_name=task_name)
    assert target is not None

    owner_ids = {
        str(row.get("id") or "").strip()
        for row in (target.get("owners") or [])
        if str(row.get("id") or "").strip()
    }
    assert owner_ids == {manager_spid, attorney_spid, handler_spid}

    for owner_id in (manager_spid, attorney_spid, handler_spid):
        resp_owner = admin_client.get(
            f"/worklog/api/tasksNewfilter=todo&days=30&owner_role=owner&owner={owner_id}"
        )
        assert resp_owner.status_code == 200
        task2 = _find_task(
            (resp_owner.get_json() or {}).get("tasks") or [],
            matter_id=matter_id,
            task_name=task_name,
        )
        assert task2 is not None

        summary = admin_client.get(
            f"/worklog/api/summaryNewdays=30&owner_role=owner&owner={owner_id}"
        )
        assert summary.status_code == 200
        assert int((summary.get_json() or {}).get("pending") or 0) >= 1


def test_worklog_owner_manager_only_does_not_fallback_to_owner_when_manager_missing(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    attorney_user, _attorney_spid = _add_case_staff(
        db_session,
        matter_id=matter_id,
        role_code="attorney",
        name="Text",
    )

    task_name = "\uc548\ub0b4\uc11c\uc2e0(3\uac1c\uc6d4) - no manager assigned"
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="MGMT_WORK",
            due_date=date.today() + timedelta(days=2),
            assignee_id=attorney_user.id,
            attorney_assignee_id=attorney_user.id,
            inspector_id=None,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    tasks = (resp.get_json() or {}).get("tasks") or []
    target = _find_task(tasks, matter_id=matter_id, task_name=task_name)
    assert target is not None

    owner_ids = {
        str(row.get("id") or "").strip()
        for row in (target.get("owners") or [])
        if str(row.get("id") or "").strip()
    }
    assert owner_ids == set()
    assert (target.get("owner_name") or "") == ""
    assert target.get("category_type") == "mgmt"
    assert target.get("category_display") == ""
