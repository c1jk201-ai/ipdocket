from __future__ import annotations

import json
import uuid
from datetime import date, timedelta


def test_case_quick_workflow_bulk_patch_persists_manual_docket_override(
    app, db_session, admin_client, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    target_staff_party_id = f"quick-bulk-staff-{uuid.uuid4().hex[:8]}"
    target_user = User(
        username=f"quick_bulk_target_{uuid.uuid4().hex[:8]}",
        email=f"quick_bulk_target_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Text",
        role="patent_staff",
        is_active=True,
        staff_party_id=target_staff_party_id,
    )
    db_session.add(target_user)
    db_session.flush()
    target_user_id = int(target_user.id)

    docket_id = f"quick-bulk-docket-{uuid.uuid4().hex[:8]}"
    source = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:QUICK:BULK",
        name_free="Text Text Text",
        due_date=(date.today() + timedelta(days=8)).isoformat(),
    )
    wf = Workflow(
        case_id=matter_id,
        name="Text Text Text",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=8),
        assignee_id=None,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add_all([source, wf])
    db_session.commit()

    resp = admin_client.patch(
        "/case/api/workflows/bulk",
        json={"ids": [wf.id], "patch": {"assignee_id": target_user_id}},
    )
    assert resp.status_code == 200
    assert (resp.get_json() or {}).get("updated") == 1

    db_session.expire_all()
    refreshed_wf = db_session.get(Workflow, int(wf.id))
    refreshed_docket = db_session.get(DocketItem, docket_id)
    assert refreshed_wf is not None
    assert refreshed_docket is not None
    assert refreshed_wf.assignee_id == target_user_id
    assert refreshed_docket.owner_staff_party_id == target_staff_party_id
    memo = json.loads(refreshed_docket.memo or "{}")
    override = memo.get("manual_workflow_assignment") or {}
    assert override.get("handler_id") == target_user_id


def test_case_quick_docket_bulk_patch_persists_manual_workflow_override(
    app, db_session, admin_client, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    target_staff_party_id = f"quick-docket-bulk-staff-{uuid.uuid4().hex[:8]}"
    target_user = User(
        username=f"quick_docket_bulk_target_{uuid.uuid4().hex[:8]}",
        email=f"quick_docket_bulk_target_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Text",
        role="patent_staff",
        is_active=True,
        staff_party_id=target_staff_party_id,
    )
    db_session.add(target_user)
    db_session.flush()
    target_user_id = int(target_user.id)

    docket_id = f"quick-docket-bulk-{uuid.uuid4().hex[:8]}"
    source = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:QUICK:DOCKET:BULK",
        name_free="Text Text Text Text",
        due_date=(date.today() + timedelta(days=9)).isoformat(),
    )
    wf = Workflow(
        case_id=matter_id,
        name="Text Text Text Text",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=9),
        assignee_id=None,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add_all([source, wf])
    db_session.commit()

    resp = admin_client.patch(
        "/case/api/dockets/bulk",
        json={"ids": [docket_id], "patch": {"assignee_id": str(target_user_id)}},
    )
    assert resp.status_code == 200
    assert (resp.get_json() or {}).get("updated") == 1

    db_session.expire_all()
    refreshed_wf = db_session.get(Workflow, int(wf.id))
    refreshed_docket = db_session.get(DocketItem, docket_id)
    assert refreshed_wf is not None
    assert refreshed_docket is not None
    assert refreshed_wf.assignee_id == target_user_id
    assert refreshed_docket.owner_staff_party_id == target_staff_party_id
    memo = json.loads(refreshed_docket.memo or "{}")
    override = memo.get("manual_workflow_assignment") or {}
    assert override.get("handler_id") == target_user_id
