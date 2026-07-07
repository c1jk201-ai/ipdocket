from __future__ import annotations

import uuid
from datetime import date, timedelta


def test_terminal_case_status_value_ignores_future_term_expiry_status_red(
    db_session, sample_matter
):
    from app.services.case.status_task_cleanup import terminal_case_status_value

    matter = db_session.merge(sample_matter)
    matter.status_blue = ""
    matter.status_red = "Term expired"
    matter.status_red_related_date = (date.today() + timedelta(days=3650)).isoformat()
    db_session.add(matter)
    db_session.commit()

    assert terminal_case_status_value(matter) is None


def test_terminal_case_status_value_uses_explicit_red_related_date_override():
    from app.services.case.status_task_cleanup import terminal_case_status_value

    future_date = (date.today() + timedelta(days=3650)).isoformat()

    assert (
        terminal_case_status_value(
            None,
            status_blue="",
            status_red="Term expired",
            status_red_related_date=future_date,
        )
        is None
    )


def test_cleanup_case_related_tasks_if_terminal_ignores_future_term_expiry(
    db_session, sample_matter
):
    from app.models.workflow import Workflow
    from app.services.case.status_task_cleanup import cleanup_case_related_tasks_if_terminal

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    wf = Workflow(
        case_id=matter_id,
        name="Text 4Text",
        status="Pending",
        business_code=f"ANNUITY:{matter_id}:4",
    )
    db_session.add(wf)
    db_session.commit()

    future_date = (date.today() + timedelta(days=3650)).isoformat()
    result = cleanup_case_related_tasks_if_terminal(
        matter_id=matter_id,
        old_status="",
        new_status="Term expired",
        status_date=future_date,
        commit=True,
    )

    db_session.expire_all()
    refreshed = Workflow.query.get(wf.id)

    assert result.applied is False
    assert refreshed is not None
    assert refreshed.status == "Pending"


def test_case_status_update_closes_related_tasks_for_terminal_status(
    app, db_session, authenticated_client, sample_user, sample_matter
):
    from app.models.matter import Matter, MatterStatusHistory
    from app.models.ip_records import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.workflow.task_sync import sync_from_docket_item

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    user = db_session.merge(sample_user)
    user.staff_party_id = user.staff_party_id or "spid-test-case-status"
    db_session.add(user)
    db_session.commit()

    due = date.today() + timedelta(days=10)
    docket_id = uuid.uuid4().hex
    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date=due.isoformat(),
        owner_staff_party_id=user.staff_party_id,
        is_deleted=False,
    )
    db_session.add(di)
    db_session.commit()

    sync_from_docket_item(docket_item=di, actor_id=user.id)
    db_session.commit()

    wf = Workflow.query.filter(Workflow.business_code.like(f"DOCKET:{docket_id}%")).first()
    assert wf is not None
    wl = WorkLog.query.filter_by(docket_id=docket_id).first()
    assert wl is not None
    assert wl.status == "pending"

    resp = authenticated_client.post(
        f"/case/{matter_id}/status/update",
        data={
            "new_status": "Abandoned",
            "status_date": "2026-04-01",
            "status_note": "Text Text Text",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    db_session.expire_all()
    matter = Matter.query.get(matter_id)
    assert matter is not None
    assert matter.inhouse_status == "Abandoned"

    history = (
        MatterStatusHistory.query.filter_by(matter_id=matter_id)
        .order_by(MatterStatusHistory.id.desc())
        .first()
    )
    assert history is not None
    assert history.status == "Abandoned"
    assert history.status_date == "2026-04-01"

    refreshed_di = DocketItem.query.get(docket_id)
    refreshed_wf = Workflow.query.get(wf.id)
    refreshed_wl = WorkLog.query.get(wl.id)

    assert refreshed_di is not None
    assert refreshed_wf is not None
    assert refreshed_wl is not None
    assert refreshed_di.done_date == "AUTO_CANCELLED:2026-04-01"
    assert refreshed_wf.status == "Abandoned"
    assert refreshed_wf.completed_date.isoformat() == "2026-04-01"
    assert refreshed_wl.status == "abandoned"
    assert refreshed_wl.action_type == "abandoned"
    assert "Abandoned" in (refreshed_wl.description or "")


def test_terminal_case_cleanup_preserves_registration_certificate_followups(
    app, db_session, sample_matter
):
    from app.models.ip_records import DocketItem
    from app.models.workflow import Workflow
    from app.models.worklog import WorkLog
    from app.services.case.status_task_cleanup import cleanup_case_related_tasks_if_terminal

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    cert_docket_id = uuid.uuid4().hex
    send_docket_id = uuid.uuid4().hex
    normal_docket_id = uuid.uuid4().hex

    cert_docket = DocketItem(
        docket_id=cert_docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:REG_CERT:RECEIPT",
        name_free="Text Text Text",
        due_date="2026-06-05",
        is_deleted=False,
    )
    send_docket = DocketItem(
        docket_id=send_docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:REG_CERT:SEND_3D",
        name_free="Text PDF Text(3Text Text)",
        due_date="2026-06-08",
        is_deleted=False,
    )
    normal_docket = DocketItem(
        docket_id=normal_docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Text",
        name_free="Text",
        due_date="2026-06-05",
        is_deleted=False,
    )
    cert_workflow = Workflow(
        case_id=matter_id,
        name="Text Text Text",
        status="Pending",
        business_code=f"DOCKET:{cert_docket_id}",
        category="MGMT",
    )
    send_workflow = Workflow(
        case_id=matter_id,
        name="Text PDF Text(3Text Text)",
        status="Pending",
        business_code=f"DOCKET:{send_docket_id}",
        category="MGMT",
    )
    normal_workflow = Workflow(
        case_id=matter_id,
        name="Text",
        status="Pending",
        business_code=f"DOCKET:{normal_docket_id}",
        category="MGMT",
    )
    cert_worklog = WorkLog(
        docket_id=cert_docket_id,
        matter_id=matter_id,
        task_name="Text Text Text",
        task_category="MGMT",
        status="pending",
        action_type="note",
    )
    send_worklog = WorkLog(
        docket_id=send_docket_id,
        matter_id=matter_id,
        task_name="Text PDF Text(3Text Text)",
        task_category="MGMT",
        status="pending",
        action_type="note",
    )
    db_session.add_all(
        [
            cert_docket,
            send_docket,
            normal_docket,
            cert_workflow,
            send_workflow,
            normal_workflow,
            cert_worklog,
            send_worklog,
        ]
    )
    db_session.commit()

    result = cleanup_case_related_tasks_if_terminal(
        matter_id=matter_id,
        old_status="",
        new_status="Abandoned",
        status_date="2026-05-15",
        commit=True,
    )

    db_session.expire_all()
    refreshed_cert_docket = DocketItem.query.get(cert_docket_id)
    refreshed_send_docket = DocketItem.query.get(send_docket_id)
    refreshed_normal_docket = DocketItem.query.get(normal_docket_id)
    refreshed_cert_workflow = Workflow.query.get(cert_workflow.id)
    refreshed_send_workflow = Workflow.query.get(send_workflow.id)
    refreshed_normal_workflow = Workflow.query.get(normal_workflow.id)
    refreshed_cert_worklog = WorkLog.query.get(cert_worklog.id)
    refreshed_send_worklog = WorkLog.query.get(send_worklog.id)

    assert result.docket_closed == 1
    assert result.workflow_closed == 1
    assert refreshed_cert_docket is not None
    assert (refreshed_cert_docket.done_date or "") == ""
    assert refreshed_cert_workflow is not None
    assert refreshed_cert_workflow.status == "Pending"
    assert refreshed_cert_workflow.completed_date is None
    assert refreshed_cert_worklog is not None
    assert refreshed_cert_worklog.status == "pending"
    assert refreshed_send_docket is not None
    assert (refreshed_send_docket.done_date or "") == ""
    assert refreshed_send_workflow is not None
    assert refreshed_send_workflow.status == "Pending"
    assert refreshed_send_workflow.completed_date is None
    assert refreshed_send_worklog is not None
    assert refreshed_send_worklog.status == "pending"
    assert refreshed_normal_docket is not None
    assert refreshed_normal_docket.done_date == "AUTO_CANCELLED:2026-05-15"
    assert refreshed_normal_workflow is not None
    assert refreshed_normal_workflow.status == "Abandoned"
