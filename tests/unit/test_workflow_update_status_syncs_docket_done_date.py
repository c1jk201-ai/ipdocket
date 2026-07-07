from __future__ import annotations

import json
import uuid
from datetime import date, timedelta


def test_workflow_update_status_syncs_done_date_for_docket_backed_workflow(
    app, db_session, admin_client, admin_user
):
    from app.models.ip_records import DocketItem, Matter
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex
    user_id = getattr(admin_user, "_test_id", None)
    assert user_id is not None

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0001",
            right_name="Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=mid,
            category="WORK",
            name_ref="Text",
            name_free="Text",
            due_date=(date.today() + timedelta(days=7)).isoformat(),
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Text",
        status="Pending",
        business_code=f"DOCKET:{docket_id}:{user_id}",
        created_by_id=user_id,
        assignee_id=user_id,
    )
    db_session.add(wf)
    db_session.commit()

    wf_id = wf.id

    resp = admin_client.post(
        f"/workflow/{wf_id}/update_status",
        data={"matter_id": mid, "status": "Completed"},
    )
    assert resp.status_code == 302

    di = db_session.get(DocketItem, docket_id)
    assert di is not None
    assert (di.done_date or "").strip()
    assert not (di.done_date or "").startswith("AUTO_CANCELLED:")

    resp2 = admin_client.post(
        f"/workflow/{wf_id}/update_status",
        data={"matter_id": mid, "status": "Pending"},
    )
    assert resp2.status_code == 302

    di2 = db_session.get(DocketItem, docket_id)
    assert di2 is not None
    assert not (di2.done_date or "").strip()

    resp3 = admin_client.post(
        f"/workflow/{wf_id}/update_status",
        data={"matter_id": mid, "status": "Abandoned"},
    )
    assert resp3.status_code == 302

    di3 = db_session.get(DocketItem, docket_id)
    assert di3 is not None
    assert (di3.done_date or "").startswith("AUTO_CANCELLED:")
    memo3 = json.loads(di3.memo or "{}")
    assert memo3.get("manual_abandoned") is True
    assert memo3.get("lock_reason") == "manual_abandon"
    assert memo3.get("locked") is True

    resp4 = admin_client.post(
        f"/workflow/{wf_id}/update_status",
        data={"matter_id": mid, "status": "Pending"},
    )
    assert resp4.status_code == 302

    di4 = db_session.get(DocketItem, docket_id)
    assert di4 is not None
    assert not (di4.done_date or "").strip()
    memo4 = json.loads(di4.memo or "{}")
    assert "manual_abandoned" not in memo4
    assert "manual_abandon_reason" not in memo4
    assert "locked" not in memo4
    assert "lock_reason" not in memo4


def test_workflow_update_status_maps_internal_due_date_and_clears_legacy_deadlines(
    app, db_session, admin_client, admin_user
):
    from app.models.ip_records import Matter
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    user_id = getattr(admin_user, "_test_id", None)
    assert user_id is not None

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0002",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Text Text Text Text",
        status="Pending",
        created_by_id=user_id,
        assignee_id=user_id,
        legal_due_date=date(2026, 4, 13),
        due_date=date(2026, 4, 13),
        draft_due_date=date(2026, 4, 10),
        draft_due_date2=date(2026, 4, 11),
        submit_due_date=date(2026, 4, 12),
    )
    db_session.add(wf)
    db_session.commit()

    resp = admin_client.post(
        f"/workflow/{wf.id}/update_status",
        data={
            "case_id": mid,
            "legal_due_date": "2026-04-13",
            "internal_due_date": "2026-04-11",
        },
    )
    assert resp.status_code == 302

    refreshed = db_session.get(Workflow, wf.id)
    assert refreshed is not None
    assert refreshed.legal_due_date == date(2026, 4, 13)
    assert refreshed.due_date == date(2026, 4, 11)
    assert refreshed.draft_due_date is None
    assert refreshed.draft_due_date2 is None
    assert refreshed.submit_due_date is None

    resp2 = admin_client.post(
        f"/workflow/{wf.id}/update_status",
        data={
            "case_id": mid,
            "legal_due_date": "2026-04-15",
            "internal_due_date": "",
        },
    )
    assert resp2.status_code == 302

    refreshed2 = db_session.get(Workflow, wf.id)
    assert refreshed2 is not None
    assert refreshed2.legal_due_date == date(2026, 4, 15)
    assert refreshed2.due_date == date(2026, 4, 15)


def test_workflow_update_status_allows_due_date_edits_for_docket_backed_workflow(
    app, db_session, admin_client, admin_user
):
    from app.models.ip_records import DocketItem, Matter
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex
    user_id = getattr(admin_user, "_test_id", None)
    assert user_id is not None

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0003",
            right_name="Text Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=mid,
            category="WORK",
            name_ref="Text",
            name_free="Text Text",
            due_date="2026-05-06",
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Text Text [Text]",
        status="Pending",
        created_by_id=user_id,
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}",
        legal_due_date=date(2026, 5, 6),
        due_date=date(2026, 5, 1),
        source_docket_legal_due_date=date(2026, 5, 6),
        source_docket_due_date=date(2026, 5, 6),
    )
    db_session.add(wf)
    db_session.commit()

    resp = admin_client.post(
        f"/workflow/{wf.id}/update_status",
        data={
            "case_id": mid,
            "note": "Text Text",
            "legal_due_date": "2026-05-08",
            "internal_due_date": "2026-05-02",
        },
    )
    assert resp.status_code == 302

    refreshed = db_session.get(Workflow, wf.id)
    assert refreshed is not None
    assert refreshed.legal_due_date == date(2026, 5, 8)
    assert refreshed.due_date == date(2026, 5, 2)
    assert refreshed.note is not None
    assert refreshed.note.startswith("Text Text")

    refreshed_docket = db_session.get(DocketItem, docket_id)
    assert refreshed_docket is not None
    assert refreshed_docket.due_date == "2026-05-06"
    assert not (refreshed_docket.extended_due_date or "").strip()


def test_docket_sync_updates_linked_workflow_due_dates(
    app, db_session, admin_user, sample_matter, monkeypatch
):
    from types import SimpleNamespace

    from app.models.ip_records import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.task_sync import sync_from_docket_item

    monkeypatch.setattr(
        "app.services.workflow.task_sync.resolve_assignees_for_docket",
        lambda _docket_item, return_decision=False: (
            [],
            SimpleNamespace(distribute_to="owner", rule_id="test"),
        ),
    )
    monkeypatch.setattr(
        "app.services.workflow.task_sync._resolve_owner_assignee_id_for_docket",
        lambda _docket_item: getattr(admin_user, "_test_id", admin_user.id),
    )
    monkeypatch.setattr(
        "app.services.workflow.sync_requests.enqueue_workflow_sync",
        lambda **_kwargs: None,
    )

    mid = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = uuid.uuid4().hex
    user_id = getattr(admin_user, "_test_id", admin_user.id)

    docket = DocketItem(
        docket_id=docket_id,
        matter_id=mid,
        category="WORK",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-05-06",
        extended_due_date="2026-05-01",
        is_deleted=False,
    )
    workflow = Workflow(
        case_id=mid,
        name="Text Text",
        status="Pending",
        created_by_id=user_id,
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}",
        legal_due_date=date(2026, 5, 6),
        due_date=date(2026, 5, 1),
        source_docket_legal_due_date=date(2026, 5, 6),
        source_docket_due_date=date(2026, 5, 1),
    )
    db_session.add_all([docket, workflow])
    db_session.commit()

    docket.due_date = "2026-06-10"
    docket.extended_due_date = "2026-06-01"
    sync_from_docket_item(docket_item=docket, actor_id=user_id)
    db_session.commit()

    refreshed = db_session.get(Workflow, workflow.id)
    assert refreshed is not None
    assert refreshed.legal_due_date == date(2026, 6, 10)
    assert refreshed.due_date == date(2026, 6, 1)


def test_quick_workflow_bulk_status_syncs_linked_docket_and_worklog(
    app, db_session, admin_client, admin_user
):
    from app.models.ip_records import DocketItem, Matter
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    mid = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex
    user_id = getattr(admin_user, "_test_id", None)
    assert user_id is not None

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0004",
            right_name="Bulk status sync matter",
            status_red="Office action pending",
            status_blue="Active",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=mid,
            category="WORK",
            name_ref="Workflow task",
            name_free="Workflow task",
            due_date=(date.today() + timedelta(days=14)).isoformat(),
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Workflow task",
        status="Pending",
        business_code=f"DOCKET:{docket_id}:{user_id}",
        created_by_id=user_id,
        assignee_id=user_id,
    )
    db_session.add(wf)
    db_session.commit()

    resp = admin_client.patch(
        "/case/api/workflows/bulk",
        json={"ids": [wf.id], "patch": {"status": "Abandoned"}},
    )
    assert resp.status_code == 200

    refreshed_wf = db_session.get(Workflow, wf.id)
    assert refreshed_wf is not None
    assert refreshed_wf.status == "Abandoned"
    assert refreshed_wf.completed_date is not None

    di = db_session.get(DocketItem, docket_id)
    assert di is not None
    assert (di.done_date or "").startswith("AUTO_CANCELLED:")
    memo = json.loads(di.memo or "{}")
    assert memo.get("manual_abandoned") is True
    assert memo.get("lock_reason") == "manual_abandon"

    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl is not None
    assert wl.status == "abandoned"

    resp2 = admin_client.patch(
        "/case/api/workflows/bulk",
        json={"ids": [wf.id], "patch": {"status": "Pending"}},
    )
    assert resp2.status_code == 200

    reopened_wf = db_session.get(Workflow, wf.id)
    assert reopened_wf is not None
    assert reopened_wf.status == "Pending"
    assert reopened_wf.completed_date is None

    di2 = db_session.get(DocketItem, docket_id)
    assert di2 is not None
    assert not (di2.done_date or "").strip()
    memo2 = json.loads(di2.memo or "{}")
    assert "manual_abandoned" not in memo2

    wl2 = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl2 is not None
    assert wl2.status == "pending"
