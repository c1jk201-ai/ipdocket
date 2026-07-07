import json
import uuid
from datetime import date, timedelta


def test_workflow_delete_requires_matter_edit_access(
    app, authenticated_client, db_session, sample_user
):
    from app.models.ip_records import Matter
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0001US",
            right_name="Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.commit()

    # Sample user is authenticated but not assigned to the matter (no staff_party_id assignment),
    # so require_matter_access(edit_case) must block deletes even if the workflow is assigned to them.
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    wf = Workflow(case_id=mid, name="Text", assignee_id=int(user_id), created_by_id=int(user_id))
    db_session.add(wf)
    db_session.commit()
    # The Flask request may remove the scoped session; don't rely on a possibly-detached ORM instance.
    wf_id = wf.id

    resp = authenticated_client.post(f"/workflow/{wf_id}/delete")
    assert resp.status_code == 403

    assert db_session.get(Workflow, wf_id) is not None


def test_workflow_delete_cleans_workflow_fk_dependents(app, admin_client, db_session):
    from app.models.ip_records import Matter
    from app.models.workflow import Workflow
    from app.models.workflow_checklist import WorkflowChecklistItem, WorkflowReminderSent

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0002US",
            right_name="Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="FK cleanup test",
        status="Pending",
    )
    db_session.add(wf)
    db_session.flush()

    due = date(2026, 3, 1)
    db_session.add(WorkflowChecklistItem(workflow_id=wf.id, title="check item"))
    db_session.add(
        WorkflowReminderSent(
            workflow_id=wf.id,
            kind="D7",
            due_date=due,
            remind_on=due - timedelta(days=7),
        )
    )
    db_session.commit()

    wf_id = wf.id
    resp = admin_client.post(f"/workflow/{wf_id}/delete")
    assert resp.status_code in (302, 303)

    assert db_session.get(Workflow, wf_id) is None
    assert db_session.query(WorkflowChecklistItem).filter_by(workflow_id=wf_id).count() == 0
    assert db_session.query(WorkflowReminderSent).filter_by(workflow_id=wf_id).count() == 0


def test_workflow_create_allows_assign_staff_permission_without_edit(app, client, db_session):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0003US",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    role = Role(
        name="lead_assign_only",
        description="assign only role for workflow create test",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    user = User(
        username="lead_assign_only",
        email="lead_assign_only@example.com",
        role="lead_attorney",
        is_active=True,
    )
    user.roles = [role]
    db_session.add_all([role, user])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = user.id
        session["_fresh"] = True

    resp = client.post(
        "/workflow/create",
        data={
            "matter_id": mid,
            "name": "Text Text Text",
        },
    )
    assert resp.status_code in (302, 303)

    created = Workflow.query.filter_by(case_id=mid, name="Text Text Text").first()
    assert created is not None

    from app.models.audit_log import AuditLog

    audit = (
        AuditLog.query.filter_by(
            action="workflow.create",
            target_type="workflow",
            target_id=created.id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    meta = json.loads(audit.meta_json)
    assert meta["workflow_id"] == created.id
    assert meta["matter_id"] == mid
    assert meta["title"] == "Text Text Text"


def test_workflow_create_self_mode_forces_current_user_only_assignment(app, client, db_session):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    ref_suffix = uuid.uuid4().hex[:6].upper()
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref=f"26PD{ref_suffix}US",
            right_name="Text Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"lead_assign_self_{suffix}",
        description="self-assignment create test role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    actor = User(
        username=f"lead_assign_self_{suffix}",
        email=f"lead_assign_self_{suffix}@example.com",
        role="lead_attorney",
        is_active=True,
    )
    actor.roles = [role]
    target = User(
        username=f"target_self_{suffix}",
        email=f"target_self_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([role, actor, target])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        "/workflow/create",
        data={
            "matter_id": mid,
            "name": "Text Text Text Text",
            "assignment_mode": "self",
            "assignee_id": str(target.id),
            "attorney_assignee_id": str(target.id),
            "inspector_id": str(target.id),
        },
    )
    assert resp.status_code in (302, 303)

    created = Workflow.query.filter_by(case_id=mid, name="Text Text Text Text").first()
    assert created is not None
    assert created.assignee_id == actor.id
    assert created.attorney_assignee_id is None
    assert created.inspector_id is None


def test_workflow_create_self_mode_allows_edit_permission_without_assign(app, client, db_session):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_EDIT_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    ref_suffix = uuid.uuid4().hex[:6].upper()
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref=f"26PD{ref_suffix}US",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"lead_edit_only_{suffix}",
        description="edit-only self assignment create test role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_EDIT_ALL],
    )
    actor = User(
        username=f"lead_edit_only_{suffix}",
        email=f"lead_edit_only_{suffix}@example.com",
        role="lead_attorney",
        is_active=True,
    )
    actor.roles = [role]
    target = User(
        username=f"target_edit_{suffix}",
        email=f"target_edit_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([role, actor, target])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        "/workflow/create",
        data={
            "case_id": mid,
            "name": "Text Text Text",
            "assignment_mode": "self",
            "assignee_id": str(target.id),
            "attorney_assignee_id": str(target.id),
            "inspector_id": str(target.id),
        },
    )
    assert resp.status_code in (302, 303)

    created = Workflow.query.filter_by(case_id=mid, name="Text Text Text").first()
    assert created is not None
    assert created.assignee_id == actor.id
    assert created.attorney_assignee_id is None
    assert created.inspector_id is None


def test_workflow_create_distribution_mode_does_not_autofill_other_roles(app, client, db_session):
    from app.models.ip_records import Matter, MatterStaffAssignment
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    ref_suffix = uuid.uuid4().hex[:6].upper()
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref=f"26PD{ref_suffix}US",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"lead_assign_distribution_{suffix}",
        description="distribution create should not autofill attorney/manager defaults",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    actor = User(
        username=f"lead_assign_distribution_{suffix}",
        email=f"lead_assign_distribution_{suffix}@example.com",
        role="lead_attorney",
        is_active=True,
    )
    actor.roles = [role]

    default_attorney = User(
        username=f"default_attorney_{suffix}",
        email=f"default_attorney_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
        staff_party_id=f"PID-ATT-{suffix}",
    )
    default_manager = User(
        username=f"default_manager_{suffix}",
        email=f"default_manager_{suffix}@example.com",
        role="mgmt_staff",
        is_active=True,
        staff_party_id=f"PID-MGR-{suffix}",
    )

    db_session.add_all([role, actor, default_attorney, default_manager])
    db_session.flush()
    db_session.add_all(
        [
            MatterStaffAssignment(
                matter_id=mid,
                staff_party_id=default_attorney.staff_party_id,
                staff_role_code="attorney",
                seq=1,
                raw_text="",
            ),
            MatterStaffAssignment(
                matter_id=mid,
                staff_party_id=default_manager.staff_party_id,
                staff_role_code="manager",
                seq=1,
                raw_text="",
            ),
        ]
    )
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        "/workflow/create",
        data={
            "case_id": mid,
            "name": "Text Text",
            "assignment_mode": "distribution",
            "assignee_id": "",
            "attorney_assignee_id": "",
            "inspector_id": str(default_manager.id),
        },
    )
    assert resp.status_code in (302, 303)

    created = Workflow.query.filter_by(case_id=mid, name="Text Text").first()
    assert created is not None
    assert created.assignee_id is None
    assert created.attorney_assignee_id is None
    assert created.inspector_id == default_manager.id


def test_workflow_create_distribution_mode_requires_assign_permission(app, client, db_session):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.utils.permissions import PERM_CASE_EDIT_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    ref_suffix = uuid.uuid4().hex[:6].upper()
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref=f"26PD{ref_suffix}US",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"edit_only_distribution_{suffix}",
        description="distribution mode should require assign permission",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_EDIT_ALL],
    )
    actor = User(
        username=f"edit_only_distribution_{suffix}",
        email=f"edit_only_distribution_{suffix}@example.com",
        role="lead_attorney",
        is_active=True,
    )
    target = User(
        username=f"target_distribution_{suffix}",
        email=f"target_distribution_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    actor.roles = [role]
    db_session.add_all([role, actor, target])
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        "/workflow/create",
        data={
            "case_id": mid,
            "name": "Text Text Text",
            "assignment_mode": "distribution",
            "assignee_id": str(target.id),
        },
    )
    assert resp.status_code == 403


def test_workflow_update_category_allows_assign_staff_permission_without_edit(
    app, client, db_session
):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref=f"26PD{uuid.uuid4().hex[:6].upper()}US",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"assign_only_update_{suffix}",
        description="assign-only role for workflow category update",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    actor = User(
        username=f"assign_only_update_{suffix}",
        email=f"assign_only_update_{suffix}@example.com",
        role="lead_attorney",
        is_active=True,
    )
    actor.roles = [role]
    db_session.add_all([role, actor])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Text Text",
        status="Pending",
        category="WORK",
        assignee_id=actor.id,
        created_by_id=actor.id,
    )
    db_session.add(wf)
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        f"/workflow/{wf.id}/update_status",
        data={
            "case_id": mid,
            "category": "MGMT",
        },
    )
    assert resp.status_code in (302, 303)

    refreshed = db_session.get(Workflow, wf.id)
    assert refreshed is not None
    assert refreshed.category == "MGMT"


def test_workflow_update_allows_assign_only_user_to_change_assignees_but_not_note(
    app, client, db_session
):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0004US",
            right_name="Text Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"partner_assign_only_{suffix}",
        description="assign-only update test role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    actor = User(
        username=f"partner_assign_only_{suffix}",
        email=f"partner_assign_only_{suffix}@example.com",
        role="partner_attorney",
        is_active=True,
    )
    actor.roles = [role]
    target = User(
        username=f"target_{suffix}",
        email=f"target_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([role, actor, target])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Text Text Text",
        note="Text Text",
        assignee_id=actor.id,
        created_by_id=actor.id,
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = wf.id

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp_assign = client.post(
        f"/workflow/{wf_id}/update_status",
        data={
            "case_id": mid,
            "assignee_id": str(target.id),
            "attorney_assignee_id": "",
            "inspector_id": "",
        },
    )
    assert resp_assign.status_code in (302, 303)
    updated = db_session.get(Workflow, wf_id)
    assert updated is not None
    assert updated.assignee_id == target.id

    resp_note = client.post(
        f"/workflow/{wf_id}/update_status",
        data={
            "case_id": mid,
            "note": "Text Text Text Text",
        },
    )
    assert resp_note.status_code == 403
    denied_state = db_session.get(Workflow, wf_id)
    assert denied_state is not None
    assert denied_state.note == "Text Text"


def test_workflow_update_allows_edit_only_user_without_assignment_permission(
    app, client, db_session
):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_EDIT_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0005US",
            right_name="Text Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"partner_edit_only_{suffix}",
        description="edit-only update test role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_EDIT_ALL],
    )
    actor = User(
        username=f"partner_edit_only_{suffix}",
        email=f"partner_edit_only_{suffix}@example.com",
        role="partner_attorney",
        is_active=True,
    )
    owner = User(
        username=f"workflow_owner_{suffix}",
        email=f"workflow_owner_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    actor.roles = [role]
    db_session.add_all([role, actor, owner])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Text Text Text",
        status="Pending",
        note="Text Text",
        assignee_id=owner.id,
        created_by_id=owner.id,
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = wf.id

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(
        f"/workflow/{wf_id}/update_status",
        data={
            "case_id": mid,
            "next": f"/workflow/{wf_id}",
            "note": "Text Text Text Text",
            "status": "Completed",
            "legal_due_date": "2026-03-22",
            "work_hours": "1.5",
        },
    )
    assert resp.status_code in (302, 303)

    updated = db_session.get(Workflow, wf_id)
    assert updated is not None
    assert updated.note == "Text Text Text Text"
    assert updated.status == "Completed"
    assert updated.legal_due_date == date(2026, 3, 22)
    assert updated.work_hours == 1.5
    assert updated.completed_date == date.today()

    from app.models.audit_log import AuditLog

    audit = (
        AuditLog.query.filter_by(
            action="workflow.update",
            target_type="workflow",
            target_id=wf_id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.actor_id == actor.id
    meta = json.loads(audit.meta_json)
    changes = meta.get("changes") or {}
    assert changes["note"]["from"] == "Text Text"
    assert changes["note"]["to"] == "Text Text Text Text"
    assert changes["status"]["from"] == "Pending"
    assert changes["status"]["to"] == "Completed"
    assert changes["work_hours"]["to"] == 1.5


def test_workflow_delete_allows_edit_only_user_without_assignment_permission(
    app, client, db_session
):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_EDIT_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PD0006US",
            right_name="Text Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"partner_delete_edit_{suffix}",
        description="edit-only delete test role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_EDIT_ALL],
    )
    actor = User(
        username=f"partner_delete_edit_{suffix}",
        email=f"partner_delete_edit_{suffix}@example.com",
        role="partner_attorney",
        is_active=True,
    )
    owner = User(
        username=f"workflow_delete_owner_{suffix}",
        email=f"workflow_delete_owner_{suffix}@example.com",
        role="patent_staff",
        is_active=True,
    )
    actor.roles = [role]
    db_session.add_all([role, actor, owner])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Text Text Text",
        status="Pending",
        assignee_id=owner.id,
        created_by_id=owner.id,
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = wf.id

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.post(f"/workflow/{wf_id}/delete")
    assert resp.status_code in (302, 303)
    assert db_session.get(Workflow, wf_id) is None

    from app.models.audit_log import AuditLog

    audit = (
        AuditLog.query.filter_by(
            action="workflow.delete",
            target_type="workflow",
            target_id=wf_id,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.actor_id == actor.id
    meta = json.loads(audit.meta_json)
    assert meta["workflow_id"] == wf_id
    assert meta["snapshot"]["name"] == "Text Text Text"
