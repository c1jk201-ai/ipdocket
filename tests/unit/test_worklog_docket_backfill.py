from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest


def test_worklog_api_tasks_backfills_missing_workflows_from_dockets(
    app, db_session, admin_client, admin_user, sample_matter
):
    """
    Regression: some legacy/migrated DocketItem rows (e.g. V2_LIMIT) may exist without
    corresponding Workflow rows, causing /worklog (Workflow-only) to miss items visible
    on the home dashboard (DocketItem-based).
    """
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog

    our_ref = sample_matter.our_ref
    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    # Ensure owner_staff_party_id -> User mapping exists.
    admin_user.staff_party_id = "spid-test-1"
    db_session.add(admin_user)
    db_session.commit()

    due = date.today() + timedelta(days=3)
    di = DocketItem(
        docket_id="docket-test-1",
        matter_id=matter_id,
        category="V2_LIMIT",
        name_free="Text",
        due_date=due.isoformat(),
        owner_staff_party_id=admin_user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    assert Workflow.query.filter_by(case_id=matter_id).count() == 0

    # The Worklog API must be read-only: it should NOT auto-backfill workflows (DB write) on GET.
    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    assert not any(
        t.get("our_ref") == our_ref and t.get("task_name") == "Text" for t in tasks
    )
    assert Workflow.query.filter_by(case_id=matter_id).count() == 0

    # Instead, the background backfill job should create the missing Workflow rows.
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    assert Workflow.query.filter_by(case_id=matter_id).count() >= 1
    assert WorkLog.query.filter_by(docket_id="docket-test-1").count() >= 1

    resp2 = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp2.status_code == 200
    data2 = resp2.get_json() or {}
    tasks2 = data2.get("tasks") or []

    assert any(
        t.get("our_ref") == our_ref and t.get("task_name") == "Text" for t in tasks2
    )


def test_docket_backfill_includes_visible_from_gate_outside_due_window(
    app, db_session, admin_user, sample_matter
):
    """
    A docket can explicitly set visible_from_date so it enters Text/Text earlier than
    the default due-date lookahead window. The backfill job should pick it up once it becomes visible
    even if its due_date is still outside the lookahead end_date.
    """
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    admin_user.staff_party_id = "spid-test-visible-from-1"
    db_session.add(admin_user)
    db_session.commit()

    due = date.today() + timedelta(days=60)
    di = DocketItem(
        docket_id="docket-visible-from-1",
        matter_id=matter_id,
        category="MGMT",
        name_free="Text",
        due_date=due.isoformat(),
        visible_from_date=date.today().isoformat(),
        owner_staff_party_id=admin_user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    assert Workflow.query.filter_by(case_id=matter_id).count() == 0

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    assert Workflow.query.filter_by(case_id=matter_id).count() >= 1


def test_docket_backfill_includes_active_overdue_items_older_than_one_year_by_default(
    app, db_session, admin_user, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    admin_user.staff_party_id = admin_user.staff_party_id or "spid-test-legacy-overdue-1"
    user_id = getattr(admin_user, "_test_id", None)
    db_session.add(admin_user)
    db_session.commit()

    due = date.today() - timedelta(days=400)
    docket_id = "docket-legacy-overdue-1"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date=due.isoformat(),
        owner_staff_party_id=admin_user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    assert Workflow.query.filter_by(case_id=matter_id).count() == 0

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    workflows = Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{docket_id}%")).all()
    assert workflows
    assert any(wf.assignee_id == user_id or wf.assignee_id is None for wf in workflows)


def test_docket_backfill_reconciles_pending_workflow_for_closed_docket(
    app, db_session, sample_matter, admin_user
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    admin_user.staff_party_id = admin_user.staff_party_id or "spid-test-closed-1"
    user_id = getattr(admin_user, "_test_id", None)
    assert user_id is not None
    db_session.add(admin_user)
    db_session.commit()

    docket_id = "docket-closed-sync-1"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="USPTO_OA:CLOSED_SYNC",
        name_free="Text Text",
        due_date=(date.today() + timedelta(days=10)).isoformat(),
        done_date=f"AUTO_CANCELLED:{date.today().isoformat()}",
        owner_staff_party_id=admin_user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        assignee_id=user_id,
        business_code=f"DOCKET:{docket_id}:{user_id}",
    )
    db_session.add(wf)
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed = Workflow.query.get(wf.id)
    assert refreshed is not None
    assert refreshed.status == "Abandoned"


def test_docket_backfill_does_not_overwrite_custom_workflow_due_fields_outside_due_window(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = "docket-field-drift-sync-1"
    legal_due = date.today() + timedelta(days=90)
    internal_due = legal_due + timedelta(days=7)

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text/Text",
        due_date=legal_due.isoformat(),
        extended_due_date=internal_due.isoformat(),
        done_date=None,
        is_deleted=False,
    )
    wf = Workflow(
        case_id=matter_id,
        name="Text",
        status="Pending",
        business_code=f"DOCKET:{docket_id}",
        legal_due_date=date.today() + timedelta(days=1),
        due_date=date.today() + timedelta(days=1),
    )
    db_session.add_all([di, wf])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed = Workflow.query.get(wf.id)
    assert refreshed is not None
    assert refreshed.name == "Text"
    assert refreshed.legal_due_date == date.today() + timedelta(days=1)
    assert refreshed.due_date == date.today() + timedelta(days=1)


def test_docket_backfill_reconciles_pending_worklog_for_closed_docket(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = "docket-worklog-closed-sync-1"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date=(date.today() + timedelta(days=5)).isoformat(),
        done_date=f"AUTO_CANCELLED:{date.today().isoformat()}",
        is_deleted=False,
    )
    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name="Text",
        task_category="MGMT",
        status="pending",
        action_type="note",
    )
    db_session.add_all([di, wl])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed = WorkLog.query.get(wl.id)
    assert refreshed is not None
    assert refreshed.status == "abandoned"
    assert refreshed.action_type == "abandoned"
    assert refreshed.completed_at is not None


def test_docket_backfill_reconciles_closed_worklog_for_reopened_docket(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.worklog import WorkLog
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = "docket-worklog-open-sync-1"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="NOTICE:REOPENED",
        name_free="Text worklog Text",
        due_date=(date.today() + timedelta(days=9)).isoformat(),
        done_date=None,
        is_deleted=False,
    )
    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name="Text worklog Text",
        task_category="WORK",
        status="abandoned",
        action_type="abandoned",
        completed_at=datetime.utcnow(),
    )
    db_session.add_all([di, wl])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed = WorkLog.query.get(wl.id)
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.action_type == "note"
    assert refreshed.completed_at is None


@pytest.mark.parametrize(
    ("workflow_status", "completed_on", "expected_done_prefix", "expected_worklog_status"),
    [
        ("Completed", date.today() - timedelta(days=2), "", "completed"),
        ("Abandoned", date.today() - timedelta(days=1), "AUTO_CANCELLED:", "abandoned"),
    ],
)
def test_docket_backfill_reconciles_open_docket_for_terminal_workflow(
    app,
    db_session,
    sample_matter,
    workflow_status,
    completed_on,
    expected_done_prefix,
    expected_worklog_status,
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = f"docket-terminal-workflow-sync-{workflow_status.lower()}"
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date=(date.today() + timedelta(days=5)).isoformat(),
        done_date=None,
        is_deleted=False,
    )
    wl = WorkLog(
        docket_id=docket_id,
        matter_id=matter_id,
        task_name="Text",
        task_category="MGMT",
        status="pending",
        action_type="note",
    )
    wf = Workflow(
        case_id=matter_id,
        name="Text",
        status=workflow_status,
        completed_date=completed_on,
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add_all([di, wl, wf])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed_docket = DocketItem.query.get(docket_id)
    refreshed_worklog = WorkLog.query.get(wl.id)

    assert refreshed_docket is not None
    assert refreshed_worklog is not None
    if expected_done_prefix:
        assert (refreshed_docket.done_date or "").startswith(expected_done_prefix)
        assert (refreshed_docket.done_date or "").endswith(completed_on.isoformat())
    else:
        assert refreshed_docket.done_date == completed_on.isoformat()
    assert refreshed_worklog.status == expected_worklog_status
    assert refreshed_worklog.completed_at is not None


def test_docket_backfill_closes_stale_status_red_without_creating_new_workflow(app, db_session):
    import uuid

    from app.models.docket import DocketItem
    from app.models.matter import Matter
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
        right_name="stale status red backfill guard",
        status_red="PCTDomesticDeadline",
        status_red_related_date=(date.today() + timedelta(days=240)).isoformat(),
        status_blue="Filing In Progress",
        is_deleted=False,
    )
    docket = DocketItem(
        docket_id="stale-status-red-backfill-1",
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:DomesticDeadline19itemsDeadline",
        name_free="DomesticDeadline19itemsDeadline",
        due_date=(date.today() - timedelta(days=90)).isoformat(),
        done_date=None,
        is_deleted=False,
    )
    db_session.add_all([matter, docket])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed_docket = db_session.get(DocketItem, docket.docket_id)
    assert refreshed_docket is not None
    assert refreshed_docket.done_date == date.today().isoformat()
    assert (
        Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{docket.docket_id}%")).count()
        == 0
    )


def test_docket_backfill_closes_pct_advisory_when_national_phase_deadline_exists(app, db_session):
    import uuid

    from app.models.docket import DocketItem
    from app.models.matter import Matter
    from app.models.workflow import Workflow
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    app.config["WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED"] = True

    matter_id = uuid.uuid4().hex
    matter = Matter(
        matter_id=matter_id,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
        right_name="pct advisory superseded",
        status_red="PCTDomesticDeadline",
        status_red_related_date=(date.today() - timedelta(days=90)).isoformat(),
        status_blue="Filing In Progress",
        is_deleted=False,
    )
    advisory = DocketItem(
        docket_id="pct-advisory-stale-1",
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:DomesticDeadline19Deadline",
        name_free="DomesticDeadline19Deadline",
        due_date=(date.today() - timedelta(days=90)).isoformat(),
        done_date=None,
        is_deleted=False,
    )
    national_phase = DocketItem(
        docket_id="pct-national-phase-1",
        matter_id=matter_id,
        category="MGMT_WORK",
        name_ref="MGMT:STATUS_RED:PCTDomesticDeadline",
        name_free="PCTDomesticDeadline",
        due_date=(date.today() - timedelta(days=1)).isoformat(),
        done_date=f"AUTO_EXPIRED:{(date.today() - timedelta(days=1)).isoformat()}",
        is_deleted=False,
    )
    db_session.add_all([matter, advisory, national_phase])
    db_session.commit()

    with app.app_context():
        backfill_workflows_from_open_dockets(
            today=date.today(),
            end_date=date.today() + timedelta(days=30),
            limit=200,
            commit=True,
        )

    db_session.expire_all()
    refreshed_advisory = db_session.get(DocketItem, advisory.docket_id)
    assert refreshed_advisory is not None
    assert refreshed_advisory.done_date == national_phase.done_date
    assert (
        Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{advisory.docket_id}%")).count()
        == 0
    )


def test_system_pct_advisory_docket_is_not_superseded_by_national_phase_deadline():
    from app.models.docket import DocketItem
    from app.services.workflow.task_sync import _pct_advisory_done_value_if_superseded

    item = DocketItem(
        docket_id="pct-advisory-system-1",
        matter_id="pct-advisory-system-matter",
        category="MGMT",
        name_ref="MGMT:PCT_ADVISORY_19M",
        name_free="Text Text 1Text Text Text",
        due_date="2026-05-18",
        done_date=None,
        memo=json.dumps(
            {
                "auto": True,
                "trigger": "deadline_code",
                "template_id": "PCT_ADVISORY_19M",
                "deadline_code": "PCT_ADVISORY_19M",
            },
            ensure_ascii=False,
        ),
    )

    assert _pct_advisory_done_value_if_superseded(item) is None


def test_system_pct_advisory_docket_is_not_closed_by_legacy_done_peer(app, db_session):
    import uuid

    from app.models.docket import DocketItem
    from app.models.matter import Matter
    from app.services.workflow.task_sync import sync_from_docket_item

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
            right_name="pct advisory system peer guard",
            status_blue="Text Text Text",
            is_deleted=False,
        )
    )
    legacy = DocketItem(
        docket_id="pct-advisory-legacy-done",
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text Text 1Text Text Text",
        name_free="Text Text 1Text Text Text",
        due_date="2026-05-18",
        done_date="2026-06-27",
        is_deleted=False,
    )
    system = DocketItem(
        docket_id="pct-advisory-system-open",
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:PCT_ADVISORY_19M",
        name_free="Text Text 1Text Text Text",
        due_date="2026-05-18",
        done_date=None,
        memo=json.dumps(
            {
                "auto": True,
                "trigger": "deadline_code",
                "template_id": "PCT_ADVISORY_19M",
                "deadline_code": "PCT_ADVISORY_19M",
            },
            ensure_ascii=False,
        ),
        is_deleted=False,
    )
    db_session.add_all([legacy, system])
    db_session.commit()

    with app.app_context():
        sync_from_docket_item(docket_item=system, actor_id=None)
        db_session.flush()

    assert system.done_date is None
