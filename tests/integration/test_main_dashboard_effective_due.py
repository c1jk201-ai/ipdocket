from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import text

from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.workflow import Workflow


def test_main_dashboard_uses_workflow_due_date_for_buckets(admin_client, db_session):
    today = date.today()
    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="TEST-EXTDUE",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
        )
    )

    name = "EXT_DUE_BUCKET_TEST"
    db_session.add(
        Workflow(
            case_id=mid,
            name=name,
            category="WORK",
            status="Pending",
            due_date=(today + timedelta(days=2)),
        )
    )
    db_session.commit()

    resp = admin_client.get("/")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert name in html
    assert (today + timedelta(days=2)).strftime("%m/%d/%Y") in html
    assert "Ref TEST-EXTDUE" in html
    assert "Text Text Text Text." not in html


def test_main_dashboard_excludes_soft_deleted_dockets(admin_client, db_session):
    today = date.today()
    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="TEST-SOFT-DELETE",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
        )
    )

    hidden_name = "SOFT_DELETED_DOCKET_SHOULD_HIDE"
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=mid,
            category="WORK",
            name_free=hidden_name,
            due_date=(today + timedelta(days=1)).isoformat(),
            done_date=None,
            is_deleted=True,
        )
    )
    db_session.commit()

    resp = admin_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert hidden_name not in html


def test_main_dashboard_uses_workflow_overdue_bucket(admin_client, db_session):
    today = date.today()
    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="TEST-OVERDUE-WF",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
        )
    )

    name = "OVERDUE_WORKFLOW_TASK"
    db_session.add(
        Workflow(
            case_id=mid,
            name=name,
            category="WORK",
            status="Pending",
            due_date=(today - timedelta(days=1)),
        )
    )
    db_session.commit()

    resp = admin_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert name in html
    assert "Text Text Text." not in html


def test_main_dashboard_treats_null_workflow_status_as_open(admin_client, db_session):
    today = date.today()
    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="TEST-NULL-WF",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
        )
    )

    name = "NULL_STATUS_WORKFLOW_TASK"
    wf = Workflow(
        case_id=mid,
        name=name,
        category="WORK",
        status="Pending",
        due_date=(today + timedelta(days=1)),
    )
    db_session.add(wf)
    db_session.flush()
    db_session.execute(
        text("UPDATE workflows SET status = NULL WHERE id = :workflow_id"),
        {"workflow_id": wf.id},
    )
    db_session.commit()

    resp = admin_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert name in html


def test_main_dashboard_excludes_legacy_terminal_workflow_status(admin_client, db_session):
    today = date.today()
    mid = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="TEST-LEGACY-TERM-WF",
            matter_type="PATENT",
            status_red="",
            status_blue="Text Text Text",
        )
    )

    name = "LEGACY_TERMINAL_STATUS_WORKFLOW_TASK"
    wf = Workflow(
        case_id=mid,
        name=name,
        category="WORK",
        status="Pending",
        due_date=(today + timedelta(days=1)),
    )
    db_session.add(wf)
    db_session.flush()
    db_session.execute(
        text("UPDATE workflows SET status = :status WHERE id = :workflow_id"),
        {"status": " done ", "workflow_id": wf.id},
    )
    db_session.commit()

    resp = admin_client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")
    assert name not in html
