from __future__ import annotations

import uuid
from datetime import date, timedelta


def test_worklog_complete_docket_item_syncs_linked_workflow_immediately(
    app, db_session, admin_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    user = db_session.get(User, int(user_id))
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    assert staff_party_id

    docket_id = uuid.uuid4().hex
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:sync-test",
        name_free="Text Text(3Text Text) · Text",
        due_date=(date.today() + timedelta(days=3)).isoformat(),
        owner_staff_party_id=staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="MGMT",
        due_date=date.today() + timedelta(days=3),
        business_code=f"DOCKET:{docket_id}:{int(user_id)}",
        assignee_id=int(user_id),
        created_by_id=int(user_id),
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = int(wf.id)

    # Simulate delayed async worker by disabling deferred enqueue path.
    monkeypatch.setattr(
        "app.blueprints.worklog.routes.enqueue_docket_sync_for_item",
        lambda *args, **kwargs: None,
    )

    resp = admin_client.post(
        f"/worklog/api/tasks/{docket_id}/complete",
        json={
            "evidence_type": "memo",
            "description": "Text Text Text",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True

    db_session.expire_all()
    di2 = db_session.get(DocketItem, docket_id)
    assert di2 is not None
    assert (di2.done_date or "").strip()

    wf2 = db_session.get(Workflow, wf_id)
    assert wf2 is not None
    assert wf2.status == "Completed"
    assert wf2.completed_date is not None


def test_worklog_complete_workflow_syncs_generated_docket_immediately(
    app, db_session, authenticated_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    user = db_session.get(User, int(user_id))
    assert user is not None

    wf = Workflow(
        case_id=matter_id,
        name="Text Text Text Text [Text]",
        status="Pending",
        category="WORK",
        due_date=date(2026, 4, 24),
        legal_due_date=date(2026, 4, 24),
        assignee_id=int(user_id),
        created_by_id=int(user_id),
    )
    db_session.add(wf)
    db_session.flush()

    docket_id = f"WF-{wf.id}-LEG"
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_ref=wf.name,
            name_free=wf.name,
            due_date="2026-04-24",
            done_date=None,
            is_deleted=False,
        )
    )
    db_session.commit()
    wf_id = int(wf.id)

    # Simulate delayed async worker; generated dockets should still sync before response.
    monkeypatch.setattr(
        "app.services.worklog.workflow_task_sync.enqueue_workflow_task_sync",
        lambda *args, **kwargs: None,
    )

    resp = authenticated_client.post(
        f"/worklog/api/tasks/wf_{wf_id}/complete",
        json={
            "evidence_type": "memo",
            "description": "Text",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True

    db_session.expire_all()
    wf2 = db_session.get(Workflow, wf_id)
    assert wf2 is not None
    assert wf2.status == "Completed"
    assert wf2.completed_date is not None

    di2 = db_session.get(DocketItem, docket_id)
    assert di2 is not None
    assert (di2.done_date or "").strip() == wf2.completed_date.isoformat()
