from __future__ import annotations

import uuid
from datetime import date

from app.models.docket import DocketItem
from app.models.workflow import Workflow
from app.services.workflow.task_sync import _docket_business_code, sync_from_docket_item


def test_sync_from_docket_item_reuses_existing_fallback_workflow(
    app, db_session, sample_matter, monkeypatch
):
    monkeypatch.setattr(
        "app.services.workflow.deferred_task_sync.enqueue_workflow_sync",
        lambda **_kwargs: None,
    )

    matter_id = str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)
    docket_id = uuid.uuid4().hex
    docket_item = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Test Task",
        due_date=date.today().isoformat(),
        memo='{"auto": true}',
    )
    db_session.add(docket_item)
    db_session.commit()

    fallback_workflow = Workflow(
        case_id=matter_id,
        name="Test Task (Fallback)",
        category="WORK",
        status="Pending",
        business_code=_docket_business_code(docket_id, None),
        due_date=date.today(),
    )
    db_session.add(fallback_workflow)
    db_session.commit()

    sync_from_docket_item(docket_item=docket_item)
    db_session.commit()

    db_session.expire_all()
    workflows = Workflow.query.filter_by(case_id=matter_id).all()
    assert len(workflows) == 1

    workflow = workflows[0]
    assert workflow.id == fallback_workflow.id
    assert workflow.business_code == _docket_business_code(docket_id, None)
    assert workflow.name == "Test Task"
