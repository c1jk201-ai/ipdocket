from __future__ import annotations

import json
import uuid
from datetime import date, timedelta


def _stub_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.blueprints.worklog.routes._sync_docket_task_immediately",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.blueprints.worklog.routes.enqueue_workflow_sync",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.blueprints.worklog.routes.enqueue_docket_sync_for_item",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.blueprints.worklog.routes.enqueue_workflow_task_sync",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.blueprints.worklog.routes._recalc_matter_status",
        lambda *args, **kwargs: None,
    )


def test_worklog_complete_via_workflow_id_logs_audit(
    app, db_session, authenticated_client, sample_user, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    _stub_side_effects(monkeypatch)

    audit_calls: list[dict] = []
    monkeypatch.setattr(
        "app.blueprints.worklog.routes._log_worklog_audit",
        lambda action, target_type, target_id=None, meta=None: audit_calls.append(
            {
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "meta": dict(meta or {}),
            }
        ),
    )

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None

    docket_id = f"wf-audit-{uuid.uuid4().hex[:10]}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:WF:AUDIT",
        name_free="WF Text Text Text",
        due_date=(date.today() + timedelta(days=5)).isoformat(),
        owner_staff_party_id=(getattr(user, "staff_party_id", None) or "").strip() or None,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=5),
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.post(
        f"/worklog/api/tasks/wf_{wf.id}/complete",
        json={"evidence_type": "memo", "description": "Text Text Text"},
    )
    assert resp.status_code == 200
    assert any(
        call["action"] == "worklog.complete"
        and call["target_type"] == "workflow"
        and int(call["meta"].get("workflow_id") or 0) == int(wf.id)
        for call in audit_calls
    )


def test_worklog_complete_rejects_invalid_evidence_type(
    app, db_session, authenticated_client, sample_matter
):
    from app.models.docket import DocketItem

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"invalid-evidence-{uuid.uuid4().hex[:10]}"
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_ref="NOTICE:INVALID:EVIDENCE",
            name_free="Text Text",
            due_date=(date.today() + timedelta(days=3)).isoformat(),
        )
    )
    db_session.commit()

    resp = authenticated_client.post(
        f"/worklog/api/tasks/{docket_id}/complete",
        json={"evidence_type": "not-allowed", "description": "Text Text"},
    )
    assert resp.status_code == 400
    assert (resp.get_json() or {}).get("error") == "invalid_evidence_type"


def test_worklog_bulk_complete_updates_tasks_and_logs_bulk_audit(
    app, db_session, authenticated_client, sample_user, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    _stub_side_effects(monkeypatch)

    audit_calls: list[dict] = []
    monkeypatch.setattr(
        "app.blueprints.worklog.routes._log_worklog_audit",
        lambda action, target_type, target_id=None, meta=None: audit_calls.append(
            {
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "meta": dict(meta or {}),
            }
        ),
    )

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip() or None

    workflow_docket_id = f"bulk-wf-{uuid.uuid4().hex[:10]}"
    direct_docket_id = f"bulk-di-{uuid.uuid4().hex[:10]}"

    db_session.add_all(
        [
            DocketItem(
                docket_id=workflow_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:BULK:WF",
                name_free="Text Text WF",
                due_date=(date.today() + timedelta(days=4)).isoformat(),
                owner_staff_party_id=staff_party_id,
            ),
            DocketItem(
                docket_id=direct_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:BULK:DI",
                name_free="Text Text DI",
                due_date=(date.today() + timedelta(days=4)).isoformat(),
                owner_staff_party_id=staff_party_id,
            ),
        ]
    )
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name="Text Text WF",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=4),
        assignee_id=user_id,
        business_code=f"DOCKET:{workflow_docket_id}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.post(
        "/worklog/api/tasks/bulk-complete",
        json={
            "task_ids": [f"wf_{wf.id}", direct_docket_id],
            "description": "Text Text Text",
            "evidence_type": "memo",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert int(payload.get("processed_count") or 0) == 2
    assert int(payload.get("missing_count") or 0) == 0

    db_session.expire_all()
    wf2 = db_session.get(Workflow, int(wf.id))
    assert wf2 is not None
    assert wf2.status == "Completed"
    assert wf2.completed_date is not None

    di2 = db_session.get(DocketItem, direct_docket_id)
    assert di2 is not None
    assert (di2.done_date or "").strip()

    wl_workflow = WorkLog.query.filter_by(docket_id=workflow_docket_id).first()
    wl_direct = WorkLog.query.filter_by(docket_id=direct_docket_id).first()
    assert wl_workflow is not None
    assert wl_direct is not None
    assert wl_workflow.status == "completed"
    assert wl_direct.status == "completed"

    assert any(call["action"] == "worklog.bulk_complete" for call in audit_calls)


def test_worklog_bulk_transfer_logs_audit(
    app, db_session, admin_client, admin_user, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.models.party import Party, PartyStaff
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest

    _stub_side_effects(monkeypatch)

    audit_calls: list[dict] = []
    monkeypatch.setattr(
        "app.blueprints.worklog.routes._log_worklog_audit",
        lambda action, target_type, target_id=None, meta=None: audit_calls.append(
            {
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "meta": dict(meta or {}),
            }
        ),
    )

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    admin_id = int(getattr(admin_user, "_test_id", None) or admin_user.id)
    target_staff_party_id = f"bulk-transfer-staff-{uuid.uuid4().hex[:8]}"
    target_user = User(
        username=f"bulk_transfer_target_{uuid.uuid4().hex[:8]}",
        email=f"bulk_transfer_target_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Text",
        role="patent_staff",
        is_active=True,
        staff_party_id=target_staff_party_id,
    )
    db_session.add_all(
        [
            Party(
                party_id=target_staff_party_id,
                name_display="Text",
                party_kind="staff",
            ),
            PartyStaff(
                party_id=target_staff_party_id,
                staff_code=target_user.username,
                active=1,
            ),
            target_user,
        ]
    )
    db_session.flush()

    docket_id = f"bulk-transfer-docket-{uuid.uuid4().hex[:8]}"
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_ref="NOTICE:BULK:TRANSFER",
            name_free="Text Text Text",
            due_date=(date.today() + timedelta(days=6)).isoformat(),
        )
    )
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name="Text Text Text",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=6),
        assignee_id=admin_id,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = admin_client.post(
        "/worklog/api/tasks/bulk-transfer",
        json={"task_ids": [f"wf_{wf.id}"], "target_user_id": target_user.id},
    )
    assert resp.status_code == 200
    assert any(
        call["action"] == "worklog.bulk_transfer"
        and int((call["meta"] or {}).get("target_user_id") or 0) == int(target_user.id)
        for call in audit_calls
    )

    db_session.expire_all()
    refreshed_wf = db_session.get(Workflow, int(wf.id))
    refreshed_docket = db_session.get(DocketItem, docket_id)
    assert refreshed_wf is not None
    assert refreshed_docket is not None
    assert refreshed_wf.assignee_id == target_user.id
    assert refreshed_docket.owner_staff_party_id == target_staff_party_id
    memo = json.loads(refreshed_docket.memo or "{}")
    override = memo.get("manual_workflow_assignment") or {}
    assert override.get("handler_id") == target_user.id
    assignment_request = WorkflowAssignmentRequest.query.filter_by(
        workflow_id=int(wf.id),
        role_code="handler",
    ).one()
    assert assignment_request.status == "pending"
    assert assignment_request.target_user_id == target_user.id
    assert assignment_request.requested_by_id == admin_id
