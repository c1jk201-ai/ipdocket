"""Tests for WorkLog consolidated staff snapshot fields."""

from __future__ import annotations

import uuid
from datetime import date, timedelta


def test_ensure_worklog_for_docket_populates_staff_snapshots(app, db_session, sample_matter):
    """ensure_worklog_for_docket should copy snapshot fields from DocketItem."""
    from app.models.ip_records import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"snap-test-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="TEST:SNAP",
        name_free="Text Text",
        due_date=(date.today() + timedelta(days=5)).isoformat(),
        owner_staff_party_id="spid-owner-1",
        snapshot_attorney="Text",
        snapshot_handler="Text",
        snapshot_manager="Text",
    )
    db_session.add(di)
    db_session.flush()

    wl = ensure_worklog_for_docket(docket_item=di)

    assert wl is not None
    assert wl.docket_id == docket_id
    assert wl.snapshot_attorney == "Text"
    assert wl.snapshot_handler == "Text"
    assert wl.snapshot_manager == "Text"
    assert wl.owner_staff_party_id == "spid-owner-1"


def test_ensure_worklog_for_docket_updates_snapshots_on_existing_row(
    app, db_session, sample_matter
):
    """Re-running ensure_worklog_for_docket should sync snapshot updates."""
    from app.models.ip_records import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"snap-upd-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_free="Text Text",
        due_date=(date.today() + timedelta(days=3)).isoformat(),
        owner_staff_party_id="spid-owner-2",
        snapshot_attorney="Text",
        snapshot_handler=None,
        snapshot_manager=None,
    )
    db_session.add(di)
    db_session.flush()

    wl = ensure_worklog_for_docket(docket_item=di)
    assert wl is not None
    assert wl.snapshot_attorney == "Text"
    assert wl.snapshot_handler is None

    # Update DocketItem snapshots
    di.snapshot_attorney = "Text"
    di.snapshot_handler = "Text"
    di.snapshot_manager = "Text"
    db_session.add(di)
    db_session.flush()

    wl2 = ensure_worklog_for_docket(docket_item=di)
    assert wl2 is not None
    assert wl2.id == wl.id  # Same row
    assert wl2.snapshot_attorney == "Text"
    assert wl2.snapshot_handler == "Text"
    assert wl2.snapshot_manager == "Text"


def test_ensure_worklog_for_docket_clears_removed_due_owner_and_snapshots(
    app, db_session, sample_matter
):
    from app.models.ip_records import DocketItem
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"snap-clear-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text Text",
        due_date=(date.today() + timedelta(days=4)).isoformat(),
        owner_staff_party_id="spid-owner-clear",
        snapshot_attorney="Text",
        snapshot_handler="Text",
        snapshot_manager="Text",
    )
    db_session.add(di)
    db_session.flush()

    wl = ensure_worklog_for_docket(docket_item=di)
    assert wl is not None
    assert wl.due_date is not None
    assert wl.owner_staff_party_id == "spid-owner-clear"
    assert wl.snapshot_attorney == "Text"

    di.due_date = None
    di.extended_due_date = None
    di.owner_staff_party_id = None
    di.snapshot_attorney = None
    di.snapshot_handler = None
    di.snapshot_manager = None
    db_session.add(di)
    db_session.flush()

    wl2 = ensure_worklog_for_docket(docket_item=di)
    assert wl2 is not None
    assert wl2.id == wl.id
    assert wl2.due_date is None
    assert wl2.owner_staff_party_id is None
    assert wl2.snapshot_attorney is None
    assert wl2.snapshot_handler is None
    assert wl2.snapshot_manager is None


def test_worklog_single_row_per_docket_with_multiple_staff(app, db_session, sample_matter):
    """Even with 3 different staff roles, only 1 WorkLog row should be created per docket_id."""
    from app.models.ip_records import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"single-row-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text Text",
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        owner_staff_party_id="spid-handler-1",
        snapshot_attorney="TextA",
        snapshot_handler="TextB",
        snapshot_manager="TextC",
    )
    db_session.add(di)
    db_session.flush()

    # Call multiple times — should remain 1 row
    wl1 = ensure_worklog_for_docket(docket_item=di)
    wl2 = ensure_worklog_for_docket(docket_item=di)
    wl3 = ensure_worklog_for_docket(docket_item=di)

    assert wl1.id == wl2.id == wl3.id

    count = WorkLog.query.filter_by(docket_id=docket_id).count()
    assert count == 1

    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl.snapshot_attorney == "TextA"
    assert wl.snapshot_handler == "TextB"
    assert wl.snapshot_manager == "TextC"


def test_ensure_worklog_for_docket_backfills_from_workflow_when_docket_has_no_snapshot(
    app, db_session, sample_matter
):
    """New DocketItem rows can be missing snapshots; ensure_worklog_for_docket should backfill from Workflow."""
    from app.models.ip_records import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"snap-wf-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text Text",
        due_date=(date.today() + timedelta(days=10)).isoformat(),
        owner_staff_party_id="spid-owner-3",
        snapshot_attorney=None,
        snapshot_handler=None,
        snapshot_manager=None,
    )
    db_session.add(di)

    wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        category="WORK",
        due_date=date.today() + timedelta(days=10),
        business_code=f"DOCKET:{docket_id}",
        assignee_id=None,
        snapshot_attorney="WFText",
        snapshot_handler="WFText",
        snapshot_manager="WFText",
    )
    db_session.add(wf)

    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name="Text WorkLog",
        task_category="WORK",
        due_date=date.today() + timedelta(days=10),
        owner_staff_party_id="spid-owner-3",
        snapshot_attorney=None,
        snapshot_handler=None,
        snapshot_manager=None,
        status="pending",
        action_type="note",
    )
    db_session.add(wl)
    db_session.flush()

    wl2 = ensure_worklog_for_docket(docket_item=di)
    assert wl2 is not None
    assert wl2.id == wl.id
    assert wl2.snapshot_attorney == "WFText"
    assert wl2.snapshot_handler == "WFText"
    assert wl2.snapshot_manager == "WFText"


def test_worklog_workflow_backfill_does_not_autoflush_pending_worklog_updates(
    app, db_session, sample_matter
):
    from sqlalchemy import event

    from app.models.ip_records import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import ensure_worklog_for_docket

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"snap-no-flush-{uuid.uuid4().hex[:8]}"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text",
        due_date=(date.today() + timedelta(days=12)).isoformat(),
        owner_staff_party_id="spid-owner-no-flush",
        snapshot_attorney=None,
        snapshot_handler=None,
        snapshot_manager=None,
    )
    db_session.add(di)
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="autoflush Text Text",
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=12),
            business_code=f"DOCKET:{docket_id}",
            assignee_id=None,
            snapshot_attorney="WFText",
            snapshot_handler="WFText",
            snapshot_manager="WFText",
        )
    )
    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name="Text Text",
        task_category="WORK",
        due_date=date.today() + timedelta(days=12),
        owner_staff_party_id="spid-owner-no-flush",
        snapshot_attorney=None,
        snapshot_handler=None,
        snapshot_manager=None,
        status="pending",
        action_type="note",
    )
    db_session.add(wl)
    db_session.flush()

    flushes: list[bool] = []
    session = db_session()

    def fail_on_flush(*_args, **_kwargs):
        flushes.append(True)
        raise AssertionError("workflow snapshot backfill should not trigger autoflush")

    event.listen(session, "before_flush", fail_on_flush)
    try:
        wl2 = ensure_worklog_for_docket(docket_item=di)
    finally:
        event.remove(session, "before_flush", fail_on_flush)

    assert flushes == []
    assert wl2 is not None
    assert wl2.id == wl.id
    assert wl2.task_name == "Text Text"
    assert wl2.snapshot_attorney == "WFText"
    assert wl2.snapshot_handler == "WFText"
    assert wl2.snapshot_manager == "WFText"
