from __future__ import annotations

import uuid
from datetime import date, timedelta


def _make_user(db_session, *, username: str, email: str, role: str = "patent_staff"):
    from app.models.user import User

    user = User(username=username, email=email, role=role, is_active=True)
    db_session.add(user)
    db_session.flush()
    return user


def _make_workflow(
    db_session, sample_matter, *, assignee_id=None, attorney_id=None, manager_id=None
):
    from app.models.workflow import Workflow

    wf = Workflow(
        case_id=str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id),
        name=f"Text Text Text {uuid.uuid4().hex[:8]}",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=7),
        assignee_id=assignee_id,
        attorney_assignee_id=attorney_id,
        inspector_id=manager_id,
        business_code=f"ASSIGN-REQ:{uuid.uuid4().hex}",
    )
    db_session.add(wf)
    db_session.flush()
    return wf


def test_sync_assignment_requests_creates_pending_for_three_roles(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target_handler = _make_user(db_session, username="target-handler", email="th@example.com")
    target_attorney = _make_user(db_session, username="target-attorney", email="ta@example.com")
    target_manager = _make_user(db_session, username="target-manager", email="tm@example.com")
    wf = _make_workflow(db_session, sample_matter)

    before = workflow_assignment_state(wf)
    wf.assignee_id = target_handler.id
    wf.attorney_assignee_id = target_attorney.id
    wf.inspector_id = target_manager.id
    requests = sync_assignment_requests_for_changed_roles(
        wf,
        before,
        requested_by_id=sample_user.id,
        source="unit_test",
    )
    db_session.commit()

    assert len(requests) == 3
    rows = WorkflowAssignmentRequest.query.order_by(WorkflowAssignmentRequest.role_code).all()
    assert {row.role_code for row in rows} == {"attorney", "handler", "manager"}
    assert {row.status for row in rows} == {"pending"}


def test_reassignment_cancels_existing_pending_request(app, db_session, sample_user, sample_matter):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    first_target = _make_user(db_session, username="first-target", email="first@example.com")
    second_target = _make_user(db_session, username="second-target", email="second@example.com")
    wf = _make_workflow(db_session, sample_matter)

    before = workflow_assignment_state(wf)
    wf.assignee_id = first_target.id
    sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")
    db_session.commit()

    before = workflow_assignment_state(wf)
    wf.assignee_id = second_target.id
    sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")
    db_session.commit()

    rows = WorkflowAssignmentRequest.query.order_by(WorkflowAssignmentRequest.id.asc()).all()
    assert [row.status for row in rows] == ["cancelled", "pending"]
    assert rows[0].target_user_id == first_target.id
    assert rows[1].target_user_id == second_target.id


def test_accept_keeps_assignment_and_marks_request_accepted(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        respond_assignment_request,
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target = _make_user(db_session, username="accept-target", email="accept@example.com")
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    result = respond_assignment_request(req.id, target.id, "accept")
    db_session.commit()

    refreshed_wf = db_session.get(type(wf), wf.id)
    refreshed_req = db_session.get(WorkflowAssignmentRequest, req.id)
    assert result.workflow_changed is False
    assert refreshed_wf.assignee_id == target.id
    assert refreshed_req.status == "accepted"


def test_reject_reverts_assignment_to_previous_user_and_stores_reason(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        respond_assignment_request,
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    previous = sample_user
    target = _make_user(db_session, username="reject-target", email="reject@example.com")
    wf = _make_workflow(db_session, sample_matter, assignee_id=previous.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    result = respond_assignment_request(req.id, target.id, "reject", reason="Text Text")
    db_session.commit()

    refreshed_wf = db_session.get(type(wf), wf.id)
    refreshed_req = db_session.get(WorkflowAssignmentRequest, req.id)
    assert result.workflow_changed is True
    assert refreshed_wf.assignee_id == previous.id
    assert refreshed_req.status == "rejected"
    assert refreshed_req.response_note == "Text Text"


def test_self_assignment_is_auto_accepted(app, db_session, sample_user, sample_matter):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    wf = _make_workflow(db_session, sample_matter)
    before = workflow_assignment_state(wf)
    wf.assignee_id = sample_user.id
    sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")
    db_session.commit()

    row = WorkflowAssignmentRequest.query.one()
    assert row.status == "accepted"
    assert row.responded_at is not None


def test_completed_workflow_auto_accepts_pending_assignment_requests(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target = _make_user(
        db_session,
        username="completed-target",
        email="completed-target@example.com",
    )
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    assert db_session.get(WorkflowAssignmentRequest, req.id).status == "pending"

    wf.status = "Completed"
    wf.completed_date = date.today()
    db_session.commit()

    refreshed_req = db_session.get(WorkflowAssignmentRequest, req.id)
    assert refreshed_req.status == "accepted"
    assert refreshed_req.response_note == "workflow-completed"
    assert refreshed_req.responded_at is not None


def test_abandoned_workflow_auto_cancels_pending_assignment_requests(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target = _make_user(
        db_session,
        username="abandoned-target",
        email="abandoned-target@example.com",
    )
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    wf.status = "Abandoned"
    wf.completed_date = date.today()
    db_session.commit()

    refreshed_req = db_session.get(WorkflowAssignmentRequest, req.id)
    assert refreshed_req.status == "cancelled"
    assert refreshed_req.response_note == "workflow-abandoned"
    assert refreshed_req.responded_at is not None


def test_assignment_change_without_sync_cancels_stale_pending_request(
    app, db_session, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    first_target = _make_user(
        db_session,
        username="stale-first-target",
        email="stale-first@example.com",
    )
    second_target = _make_user(
        db_session,
        username="stale-second-target",
        email="stale-second@example.com",
    )
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = first_target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    wf.assignee_id = second_target.id
    db_session.commit()

    refreshed_req = db_session.get(WorkflowAssignmentRequest, req.id)
    assert refreshed_req.status == "cancelled"
    assert refreshed_req.response_note == "workflow-assignment-changed"
    assert refreshed_req.responded_at is not None


def test_assignment_request_api_restricts_response_to_target_user(
    app, db_session, client, sample_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target = _make_user(db_session, username="api-target", email="api-target@example.com")
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True
    forbidden = client.post(f"/worklog/api/assignment-requests/{req.id}/accept", json={})
    assert forbidden.status_code == 403

    with client.session_transaction() as session:
        session["_user_id"] = target.id
        session["_fresh"] = True
    accepted = client.post(f"/worklog/api/assignment-requests/{req.id}/accept", json={})
    assert accepted.status_code == 200
    assert (accepted.get_json() or {}).get("request", {}).get("status") == "accepted"
    assert db_session.get(WorkflowAssignmentRequest, req.id).status == "accepted"


def test_assignment_request_inbox_and_sent_are_scoped_to_current_user(
    app, db_session, client, sample_user, sample_matter
):
    from app.services.workflow.assignment_requests import (
        sync_assignment_requests_for_changed_roles,
        workflow_assignment_state,
    )

    target = _make_user(db_session, username="scope-target", email="scope-target@example.com")
    other = _make_user(db_session, username="scope-other", email="scope-other@example.com")
    wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(wf)
    wf.assignee_id = target.id
    req = sync_assignment_requests_for_changed_roles(wf, before, sample_user.id, "unit_test")[0]

    other_wf = _make_workflow(db_session, sample_matter, assignee_id=sample_user.id)
    before = workflow_assignment_state(other_wf)
    other_wf.assignee_id = other.id
    sync_assignment_requests_for_changed_roles(other_wf, before, sample_user.id, "unit_test")
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = target.id
        session["_fresh"] = True
    inbox = client.get("/worklog/api/assignment-requestsNewscope=inbox").get_json() or {}
    assert [row["id"] for row in inbox.get("requests", [])] == [req.id]
    assert inbox.get("counts", {}).get("inbox_pending") == 1
    assert inbox.get("counts", {}).get("sent_pending") == 0

    with client.session_transaction() as session:
        session["_user_id"] = sample_user.id
        session["_fresh"] = True
    sent = client.get("/worklog/api/assignment-requestsNewscope=sent").get_json() or {}
    assert {row["target_user_id"] for row in sent.get("requests", [])} == {target.id, other.id}
    assert sent.get("counts", {}).get("inbox_pending") == 0
    assert sent.get("counts", {}).get("sent_pending") == 2


def test_quick_add_workflow_creates_assignment_requests(
    app, db_session, admin_client, admin_user, sample_matter
):
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest

    target_handler = _make_user(
        db_session,
        username="quickadd-handler",
        email="quickadd-handler@example.com",
    )
    target_manager = _make_user(
        db_session,
        username="quickadd-manager",
        email="quickadd-manager@example.com",
    )
    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    actor_id = int(getattr(admin_user, "_test_id", None) or admin_user.id)

    response = admin_client.post(
        "/api/quickadd/workflow",
        json={
            "matter_id": matter_id,
            "title": "Quick Add Text Text",
            "assignee_id": str(target_handler.id),
            "manager_assignee_id": str(target_manager.id),
        },
    )

    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload.get("ok") is True
    workflow_id = int(payload.get("id") or 0)
    rows = (
        WorkflowAssignmentRequest.query.filter_by(workflow_id=workflow_id)
        .order_by(WorkflowAssignmentRequest.role_code.asc())
        .all()
    )
    assert [(row.role_code, row.status) for row in rows] == [
        ("handler", "pending"),
        ("manager", "pending"),
    ]
    assert {row.target_user_id for row in rows} == {target_handler.id, target_manager.id}
    assert {row.requested_by_id for row in rows} == {actor_id}


