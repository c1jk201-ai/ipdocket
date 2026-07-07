from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta


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
        "app.blueprints.worklog.routes._recalc_matter_status",
        lambda *args, **kwargs: None,
    )


def test_worklog_complete_via_workflow_id_creates_or_updates_worklog(
    app, db_session, authenticated_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    _stub_side_effects(monkeypatch)

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    assert staff_party_id

    docket_id = f"wf-complete-{uuid.uuid4().hex[:10]}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:WF:COMPLETE",
        name_free="WF Text Text Text",
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        owner_staff_party_id=staff_party_id,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=7),
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.post(
        f"/worklog/api/tasks/wf_{wf.id}/complete",
        json={"evidence_type": "memo", "description": "wf Text Text"},
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True
    assert payload.get("worklog_id")

    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl is not None
    assert wl.status == "completed"
    assert wl.action_type == "completed"
    assert "wf Text Text" in (wl.description or "")
    assert wl.completed_by_id == user_id
    assert wl.completed_at is not None


def test_worklog_reopen_via_workflow_id_clears_completion_metadata(
    app, db_session, authenticated_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    _stub_side_effects(monkeypatch)

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    assert staff_party_id

    docket_id = f"wf-reopen-{uuid.uuid4().hex[:10]}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:WF:REOPEN",
        name_free="WF Text Text Text",
        due_date=(date.today() + timedelta(days=3)).isoformat(),
        done_date=date.today().isoformat(),
        memo=json.dumps(
            {
                "manual_abandoned": True,
                "manual_abandoned_at": date.today().isoformat(),
                "manual_abandon_reason": "legacy abandon",
                "locked": True,
                "lock_reason": "manual_abandon",
                "source": "workflow_test",
            },
            ensure_ascii=False,
        ),
        owner_staff_party_id=staff_party_id,
    )
    db_session.add(di)

    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name=di.name_free,
        task_category=di.category,
        due_date=date.today() + timedelta(days=3),
        owner_staff_party_id=staff_party_id,
        status="completed",
        action_type="completed",
        completed_by_id=user_id,
        completed_at=datetime.utcnow(),
    )
    db_session.add(wl)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Completed",
        completed_date=date.today(),
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.post(f"/worklog/api/tasks/wf_{wf.id}/reopen", json={})
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True
    assert payload.get("worklog_id") == wl.id

    db_session.expire_all()
    wl2 = WorkLog.query.get(wl.id)
    assert wl2 is not None
    assert wl2.status == "pending"
    assert wl2.action_type == "reopened"
    assert wl2.completed_at is None
    assert wl2.completed_by_id is None

    di2 = DocketItem.query.get(docket_id)
    assert di2 is not None
    memo = json.loads(di2.memo or "{}")
    assert memo.get("source") == "workflow_test"
    assert "manual_abandoned" not in memo
    assert "manual_abandon_reason" not in memo
    assert "locked" not in memo
    assert "lock_reason" not in memo


def test_worklog_note_on_cancelled_docket_keeps_abandoned_status(
    app, db_session, admin_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.worklog import WorkLog

    _stub_side_effects(monkeypatch)

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    assert staff_party_id

    docket_id = f"note-cancel-{uuid.uuid4().hex[:10]}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text Text Text Text",
        due_date=(date.today() + timedelta(days=14)).isoformat(),
        done_date=f"AUTO_CANCELLED:{date.today().isoformat()}",
        owner_staff_party_id=staff_party_id,
    )
    db_session.add(di)
    db_session.commit()

    resp = admin_client.post(
        f"/worklog/api/tasks/{docket_id}/note",
        json={"description": "Text Text Text Text"},
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True
    assert payload.get("worklog_id")

    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl is not None
    assert wl.status == "abandoned"
    assert wl.action_type == "abandoned"
    assert "Text Text Text Text" in (wl.description or "")


def test_worklog_note_on_expired_docket_keeps_expired_action(
    app, db_session, admin_client, sample_user, sample_matter, monkeypatch
):
    from app.models.ip_records import DocketItem
    from app.models.user import User
    from app.models.worklog import WorkLog

    _stub_side_effects(monkeypatch)

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    user = db_session.get(User, user_id)
    assert user is not None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    assert staff_party_id

    docket_id = f"note-expired-{uuid.uuid4().hex[:10]}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text Text Text Text",
        due_date=(date.today() + timedelta(days=14)).isoformat(),
        done_date=f"AUTO_EXPIRED:{date.today().isoformat()}",
        owner_staff_party_id=staff_party_id,
    )
    db_session.add(di)
    db_session.commit()

    resp = admin_client.post(
        f"/worklog/api/tasks/{docket_id}/note",
        json={"description": "Text Text Text Text"},
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload.get("success") is True
    assert payload.get("worklog_id")

    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl is not None
    assert wl.status == "abandoned"
    assert wl.action_type == "expired"
    assert "Text Text Text Text" in (wl.description or "")
