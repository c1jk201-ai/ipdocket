from __future__ import annotations

import json
import uuid
from datetime import date, timedelta


def test_worklog_abandon_workflow_cancels_docket_and_does_not_close_matter(
    app, db_session, authenticated_client, sample_user, sample_matter
):
    from app.models.ip_records import DocketItem, OfficeAction
    from app.models.matter import Matter, MatterEvent, MatterStaffAssignment
    from app.models.workflow import Workflow
    from app.services.workflow.task_sync import sync_from_docket_item

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    matter = Matter.query.get(matter_id)
    assert matter is not None
    matter.status_blue = "Active"
    matter.status_red = "Office action pending"
    db_session.commit()

    # Ensure staff_party_id -> User mapping exists for docket/workflow sync.
    sample_user.staff_party_id = "spid-test-oa"
    db_session.add(sample_user)
    db_session.commit()
    user_id = sample_user.id
    staff_party_id = sample_user.staff_party_id

    # Make the matter accessible under the policy engine.
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=staff_party_id,
            staff_role_code="attorney",
        )
    )
    db_session.commit()

    oa_id = uuid.uuid4().hex
    today = date.today()
    due = today + timedelta(days=5)

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=matter_id,
            doc_name="Office action",
            received_date=today.isoformat(),
            due_date=due.isoformat(),
        )
    )
    db_session.commit()

    di = DocketItem(
        docket_id=uuid.uuid4().hex,
        matter_id=matter_id,
        category="NOTICE",
        name_ref=f"NOTICE:OA:{oa_id}",
        name_free="Office action response deadline",
        due_date=due.isoformat(),
        owner_staff_party_id=staff_party_id,
        memo=json.dumps(
            {
                "auto": True,
                "trigger": "office_action_due",
                "oa_id": oa_id,
                "due_date": due.isoformat(),
            },
            ensure_ascii=False,
        ),
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    sync_from_docket_item(docket_item=di, actor_id=user_id)
    db_session.commit()

    di_id = di.docket_id
    wf = Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{di_id}%")).first()
    assert wf is not None

    resp = authenticated_client.post(
        f"/worklog/api/tasks/wf_{wf.id}/abandon",
        json={"reason": "Manual task abandonment"},
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert data.get("success") is True

    di2 = DocketItem.query.get(di_id)
    assert di2 is not None
    assert (di2.done_date or "").startswith("AUTO_CANCELLED:")
    memo = json.loads(di2.memo or "{}")
    assert memo.get("manual_abandoned") is True
    assert memo.get("lock_reason") == "manual_abandon"
    assert memo.get("locked") is True

    oa = OfficeAction.query.get(oa_id)
    assert oa is not None
    assert (oa.done_date or "").startswith("AUTO_CANCELLED:")

    assert MatterEvent.query.filter_by(
        matter_id=matter_id, event_key="Abandoned/Withdrawn"
    ).count() == 0

    matter = Matter.query.get(matter_id)
    assert matter is not None
    assert matter.status_blue == "Active"


def test_worklog_abandon_docket_item_does_not_insert_matter_abandon_event(
    app, db_session, authenticated_client, sample_user, sample_matter
):
    from app.models.ip_records import DocketItem
    from app.models.matter import MatterEvent, MatterStaffAssignment

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    sample_user.staff_party_id = "spid-test-mgmt"
    db_session.add(sample_user)
    db_session.commit()

    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=sample_user.staff_party_id,
            staff_role_code="manager",
        )
    )
    db_session.commit()

    di = DocketItem(
        docket_id=uuid.uuid4().hex,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:OfficeActionDeadline",
        name_free="Office action deadline",
        due_date=(date.today() + timedelta(days=30)).isoformat(),
        owner_staff_party_id=sample_user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    resp = authenticated_client.post(
        f"/worklog/api/tasks/{di.docket_id}/abandon",
        json={"reason": "Manual docket abandonment"},
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert data.get("success") is True

    db_session.expire_all()
    di2 = DocketItem.query.get(di.docket_id)
    assert di2 is not None
    memo = json.loads(di2.memo or "{}")
    assert memo.get("manual_abandoned") is True
    assert memo.get("lock_reason") == "manual_abandon"
    assert memo.get("locked") is True

    assert MatterEvent.query.filter_by(
        matter_id=matter_id, event_key="Abandoned/Withdrawn"
    ).count() == 0
