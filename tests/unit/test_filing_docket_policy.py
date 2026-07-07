from __future__ import annotations


def test_upsert_filing_docket_uses_single_consolidated_workflow(
    db_session, sample_matter, monkeypatch
) -> None:
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.deadlines import docket_service

    monkeypatch.setattr(
        docket_service,
        "enqueue_docket_sync_for_item",
        lambda *, docket_item: None,
    )

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    legacy_handler = DocketItem(
        docket_id="legacy-filing-handler-docket",
        matter_id=matter_id,
        category="FILING",
        name_ref="Filing (Process)",
        name_free="Filing Deadline (Process)",
        extended_due_date="2026-05-21",
        is_deleted=False,
    )
    legacy_workflow = Workflow(
        case_id=matter_id,
        name="Filing Deadline (Process)",
        status="Pending",
        business_code=f"DOCKET:{legacy_handler.docket_id}",
    )
    legacy_worklog = WorkLog(
        matter_id=matter_id,
        docket_id=legacy_handler.docket_id,
        task_name="Filing Deadline (Process)",
        status="pending",
    )
    db_session.add_all([legacy_handler, legacy_workflow, legacy_worklog])
    db_session.commit()

    docket_service.upsert_filing_docket(
        matter_id,
        "2026-05-21",
        deadline_type="INTERNAL",
        commit=True,
    )

    main = DocketItem.query.filter_by(matter_id=matter_id, name_ref="Filing").first()
    assert main is not None
    assert main.name_free == "Filing Deadline"
    assert main.extended_due_date == "2026-05-21"
    assert not (main.due_date or "").strip()

    main_workflow = Workflow.query.filter_by(
        case_id=matter_id,
        business_code=f"DOCKET:{main.docket_id}",
        name="Filing Deadline",
    ).first()
    assert main_workflow is not None
    assert main_workflow.status == "Pending"

    db_session.refresh(legacy_handler)
    assert legacy_handler.is_deleted is True
    assert legacy_handler.delete_reason == "retire_filing_handler_helper"
    assert legacy_handler.deleted_at is not None

    active_handler = (
        DocketItem.query.filter_by(matter_id=matter_id, name_ref="Filing (Process)")
        .filter(DocketItem.is_deleted.is_(False))
        .first()
    )
    assert active_handler is None
    assert Workflow.query.filter_by(id=legacy_workflow.id).first() is None
    assert WorkLog.query.filter_by(docket_id=legacy_handler.docket_id).first() is None


def test_deferred_sync_calendar_failures_do_not_retry_operational_job() -> None:
    from app.services.workflow.deferred_task_sync import _critical_deferred_failures

    failures = [
        "workflow:7569:OperationalError",
        "docket:abc:OperationalError",
        "workflow_task:123:OperationalError",
    ]

    assert _critical_deferred_failures(failures) == [
        "docket:abc:OperationalError",
        "workflow_task:123:OperationalError",
    ]


def test_sync_from_docket_item_retires_orphaned_workflow_generated_docket(
    db_session, sample_matter
) -> None:
    from app.models.docket import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import sync_from_docket_item

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket = DocketItem(
        docket_id="WF-999999-LEG",
        matter_id=matter_id,
        category="WORK",
        name_ref="Text Text [Text]",
        name_free="Text Text [Text]",
        due_date="2026-05-21",
        is_deleted=False,
    )
    worklog = WorkLog(
        matter_id=matter_id,
        docket_id=docket.docket_id,
        task_name=docket.name_free,
        status="pending",
    )
    db_session.add_all([docket, worklog])
    db_session.commit()

    sync_from_docket_item(docket_item=docket, actor_id=None)
    db_session.commit()

    db_session.refresh(docket)
    assert docket.is_deleted is True
    assert docket.delete_reason == "orphaned_workflow_generated_docket"
    assert WorkLog.query.filter_by(docket_id=docket.docket_id).first() is None
