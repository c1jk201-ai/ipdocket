from __future__ import annotations

from datetime import date, timedelta


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_should_skip_workflow_for_annuity_status_red_docket(db_session, sample_matter):
    from app.models.docket import DocketItem
    from app.services.workflow.task_sync import _should_skip_workflow_for_docket

    mid = _matter_id(sample_matter)
    due = (date.today() + timedelta(days=5)).isoformat()
    di = DocketItem(
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:4RenewalDeadline",
        name_free="4RenewalDeadline",
        due_date=due,
        done_date=None,
    )
    db_session.add(di)
    db_session.flush()

    assert _should_skip_workflow_for_docket(di) is True


def test_sync_from_docket_item_skipped_annuity_status_red_does_not_create_worklog(
    app, db_session, sample_matter
):
    import uuid

    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import sync_from_docket_item

    mid = _matter_id(sample_matter)
    docket_id = f"annuity-skip-{uuid.uuid4().hex[:8]}"
    due = (date.today() + timedelta(days=5)).isoformat()
    di = DocketItem(
        docket_id=docket_id,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:4RenewalDeadline",
        name_free="4RenewalDeadline",
        due_date=due,
        done_date=None,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    sync_from_docket_item(docket_item=di, actor_id=None)
    db_session.commit()

    assert WorkLog.query.filter_by(docket_id=docket_id).count() == 0
    assert Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{docket_id}%")).count() == 0
