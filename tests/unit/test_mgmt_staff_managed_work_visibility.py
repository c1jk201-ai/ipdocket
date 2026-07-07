from __future__ import annotations

from datetime import date, timedelta


def _login(client, *, user_id: int):
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


def test_worklog_api_tasks_allows_mgmt_staff_to_view_managed_work_workflows(
    app, client, db_session, sample_matter
):
    """
    Regression: mgmt_staff must be able to *see* WORK workflows for matters they manage
    (MatterStaffAssignment.staff_role_code in manager/mgmt), without creating extra workflows.
    """
    from app.models.ip_records import MatterStaffAssignment
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    sp_mgr = "sp_mgr_1"
    sp_att = "sp_att_1"

    mgmt_user = User(
        username="mgmt1", email="mgmt1@example.com", role="mgmt_staff", staff_party_id=sp_mgr
    )
    attorney_user = User(
        username="attorney2",
        email="attorney2@example.com",
        role="patent_staff",
        staff_party_id=sp_att,
        display_name="Text",
    )
    db_session.add_all([mgmt_user, attorney_user])
    db_session.commit()

    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=sp_mgr,
            staff_role_code="manager",
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="OA Text Text",
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=1),
            assignee_id=attorney_user.id,
        )
    )
    db_session.commit()

    mgmt_client = _login(client, user_id=mgmt_user.id)
    resp = mgmt_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (
            t
            for t in tasks
            if t.get("matter_id") == matter_id and t.get("task_name") == "OA Text Text"
        ),
        None,
    )
    assert target is not None


def test_worklog_api_tasks_does_not_expose_unmanaged_work_workflows_to_mgmt_staff(
    app, client, db_session, sample_matter
):
    """
    mgmt_staff should not see unrelated WORK workflows unless they manage the matter.
    """
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    sp_mgr = "sp_mgr_2"
    sp_att = "sp_att_2"

    mgmt_user = User(
        username="mgmt2", email="mgmt2@example.com", role="mgmt_staff", staff_party_id=sp_mgr
    )
    attorney_user = User(
        username="attorney3",
        email="attorney3@example.com",
        role="patent_staff",
        staff_party_id=sp_att,
    )
    db_session.add_all([mgmt_user, attorney_user])
    db_session.commit()

    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Text Text Text",
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=1),
            assignee_id=attorney_user.id,
        )
    )
    db_session.commit()

    mgmt_client = _login(client, user_id=mgmt_user.id)
    resp = mgmt_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    assert not any(
        t.get("matter_id") == matter_id and t.get("task_name") == "Text Text Text" for t in tasks
    )
