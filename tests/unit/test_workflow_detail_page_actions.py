from __future__ import annotations

import json
import uuid


def test_workflow_detail_page_shows_status_actions_for_edit_users(
    app, authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.audit_log import AuditLog
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    docket_id = uuid.uuid4().hex

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_free="Office action response docket",
            due_date="2026-03-20",
        )
    )

    wf = Workflow(
        case_id=matter_id,
        name="Office action response task",
        status="Pending",
        business_code=f"DOCKET:{docket_id}",
        assignee_id=user_id,
        created_by_id=user_id,
    )
    db_session.add(wf)
    db_session.flush()
    db_session.add(
        AuditLog(
            actor_id=user_id,
            user_id=user_id,
            action="workflow.update",
            target_type="workflow",
            target_id=wf.id,
            meta_json=json.dumps(
                {
                    "workflow_id": wf.id,
                    "matter_id": matter_id,
                    "title": wf.name,
                    "changes": {
                        "status": {"from": "Pending", "to": "In Progress"},
                    },
                },
                ensure_ascii=False,
            ),
        )
    )
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert 'data-workflow-status-actions="1"' in html
    assert 'name="status" value="Pending"' in html
    assert 'name="status" value="In Progress"' in html
    assert 'name="status" value="Completed"' in html
    assert 'name="status" value="Abandoned"' in html
    assert ">Pending</button>" in html
    assert ">In Progress</button>" in html
    assert ">Done</button>" in html
    assert ">Task Abandoned</button>" in html
    assert "Office action response task" in html
    assert "Pending" in html
    assert "In Progress" in html


def test_workflow_detail_page_hides_status_actions_for_assign_only_users(app, client, db_session):
    from app.models.ip_records import Matter
    from app.models.role import Role
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.utils.permissions import PERM_CASE_ASSIGN_ALL, PERM_CASE_VIEW_ALL

    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26PDSTAT1US",
            right_name="Assign only matter",
            status_red="Office action pending",
            status_blue="Active",
        )
    )

    suffix = uuid.uuid4().hex[:8]
    role = Role(
        name=f"wf_detail_assign_only_{suffix}",
        description="workflow detail assign only role",
        permissions=[PERM_CASE_VIEW_ALL, PERM_CASE_ASSIGN_ALL],
    )
    actor = User(
        username=f"wf_detail_assign_only_{suffix}",
        email=f"wf_detail_assign_only_{suffix}@example.com",
        role="partner_attorney",
        is_active=True,
    )
    actor.roles = [role]
    db_session.add_all([role, actor])
    db_session.flush()

    wf = Workflow(
        case_id=mid,
        name="Assign only workflow",
        status="Pending",
        assignee_id=actor.id,
        created_by_id=actor.id,
    )
    db_session.add(wf)
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = actor.id
        session["_fresh"] = True

    resp = client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert 'data-workflow-status-actions="1"' not in html
    assert 'name="status" value="Completed"' not in html
    assert ">Done</button>" not in html
