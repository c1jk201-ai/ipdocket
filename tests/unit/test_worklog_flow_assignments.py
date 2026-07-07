from __future__ import annotations

from datetime import date, timedelta


def test_worklog_tasks_prefer_explicit_workflow_flow_assignments_over_case_staff(
    admin_client, db_session, monkeypatch, sample_matter
):
    from app.models.ip_records import MatterStaffAssignment
    from app.models.party import Party, PartyStaff
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.task_distribution_rules import DistributionDecision

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    attorney_party_id = "worklog-flow-attorney-spid"
    handler_party_id = "worklog-flow-handler-stale-spid"
    manager_party_id = "worklog-flow-manager-spid"

    for party_id, name in (
        (attorney_party_id, "Explicit Attorney"),
        (handler_party_id, "Stale Case Handler"),
        (manager_party_id, "Explicit Manager"),
    ):
        db_session.add(Party(party_id=party_id, name_display=name))
        db_session.add(PartyStaff(party_id=party_id, staff_code=party_id, active=1))

    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=attorney_party_id,
                staff_role_code="attorney",
                raw_text="Explicit Attorney",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=handler_party_id,
                staff_role_code="handler",
                raw_text="Stale Case Handler",
            ),
            MatterStaffAssignment(
                matter_id=matter_id,
                staff_party_id=manager_party_id,
                staff_role_code="manager",
                raw_text="Explicit Manager",
            ),
        ]
    )

    attorney_user = User(
        username="worklog_flow_attorney",
        email="worklog_flow_attorney@example.com",
        display_name="Explicit Attorney",
        staff_party_id=attorney_party_id,
        role="lead_attorney",
        is_active=True,
    )
    manager_user = User(
        username="worklog_flow_manager",
        email="worklog_flow_manager@example.com",
        display_name="Explicit Manager",
        staff_party_id=manager_party_id,
        role="mgmt_staff",
        is_active=True,
    )
    db_session.add_all([attorney_user, manager_user])
    db_session.flush()

    task_name = "Flow assignment task"
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="MGMT_WORK",
            due_date=date.today() + timedelta(days=3),
            attorney_assignee_id=attorney_user.id,
            inspector_id=manager_user.id,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.blueprints.worklog.routes.resolve_distribution_decision",
        lambda **kwargs: DistributionDecision(
            distribute_to="role_set",
            role_codes=("manager", "attorney", "handler"),
        ),
    )

    resp = admin_client.get(
        "/worklog/api/tasks",
        query_string={"filter": "todo", "days": "30"},
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    tasks = payload.get("tasks") or []
    target = next(
        (
            task
            for task in tasks
            if task.get("matter_id") == matter_id and task.get("task_name") == task_name
        ),
        None,
    )
    assert target is not None

    owner_names = str(target.get("owner_name") or "")
    assert "Explicit Attorney" in owner_names
    assert "Explicit Manager" in owner_names
    assert "Stale Case Handler" not in owner_names

    assert target.get("handlers") == []
    assert str(target.get("handler_names") or "") == ""

    owner_filtered = admin_client.get(
        "/worklog/api/tasks",
        query_string={"filter": "todo", "days": "30", "owner": handler_party_id},
    )
    assert owner_filtered.status_code == 200
    filtered_tasks = (owner_filtered.get_json() or {}).get("tasks") or []
    assert not any(
        task.get("matter_id") == matter_id and task.get("task_name") == task_name
        for task in filtered_tasks
    )

    handler_filtered = admin_client.get(
        "/worklog/api/tasks",
        query_string={
            "filter": "todo",
            "days": "30",
            "owner_role": "handler",
            "owner": handler_party_id,
        },
    )
    assert handler_filtered.status_code == 200
    handler_tasks = (handler_filtered.get_json() or {}).get("tasks") or []
    assert not any(
        task.get("matter_id") == matter_id and task.get("task_name") == task_name
        for task in handler_tasks
    )
