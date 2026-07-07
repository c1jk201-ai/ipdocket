from __future__ import annotations

import json
from datetime import date, datetime, timedelta


def _find_task_by_id(tasks: list[dict], wf_id: int) -> dict | None:
    needle = f"wf_{wf_id}"
    for t in tasks or []:
        if str(t.get("id") or "") == needle:
            return t
    return None


def test_worklog_api_tasks_marks_notice_send_candidate_as_completion_recommendation(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=2)
    docket_id = "notice-send-recommend-1"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:TEST",
        name_free="Text Text(3Text Text) · Text",
        due_date=due.isoformat(),
        memo=json.dumps(
            {
                "notice_send_semi_auto": {
                    "candidate": True,
                    "prompted": False,
                    "trigger_doc_name": "Text",
                }
            },
            ensure_ascii=False,
        ),
        is_deleted=False,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="MGMT",
        due_date=due,
        business_code=f"DOCKET:{docket_id}:1",
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = int(wf.id)
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = _find_task_by_id(tasks, wf_id)
    assert target is not None
    assert target.get("completion_recommendation") is True
    assert target.get("completion_recommendation_kind") == "notice_send_semi_auto"
    assert "Text" in (target.get("completion_recommendation_text") or "")


def test_worklog_api_tasks_filters_completion_recommendation_bucket(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=2)
    docket_id = "notice-send-recommend-bucket-1"

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="MGMT",
            name_ref="MGMT:NOTICE_SEND_3D:BUCKET",
            name_free="Text Text(3Text Text) · Text Text",
            due_date=due.isoformat(),
            memo=json.dumps(
                {
                    "notice_send_semi_auto": {
                        "candidate": True,
                        "prompted": False,
                        "trigger_doc_name": "Text Text Text",
                    }
                },
                ensure_ascii=False,
            ),
            is_deleted=False,
        )
    )
    db_session.flush()

    recommended_wf = Workflow(
        case_id=matter_id,
        name="Text Text Text Text",
        status="Pending",
        category="MGMT",
        due_date=due,
        business_code=f"DOCKET:{docket_id}:1",
    )
    plain_wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        category="WORK",
        due_date=due,
    )
    db_session.add_all([recommended_wf, plain_wf])
    db_session.flush()
    recommended_wf_id = int(recommended_wf.id)
    plain_wf_id = int(plain_wf.id)
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30&bucket=recommended")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    assert int(data.get("total") or 0) == 1
    assert _find_task_by_id(tasks, recommended_wf_id) is not None
    assert _find_task_by_id(tasks, plain_wf_id) is None


def test_worklog_api_tasks_keeps_recommendation_after_prompt_ack(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=3)
    docket_id = "notice-send-recommend-2"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:TEST2",
        name_free="Text Text(3Text Text) · Text",
        due_date=due.isoformat(),
        memo=json.dumps(
            {
                "notice_send_semi_auto": {
                    "candidate": True,
                    "prompted": True,
                    "decision": "no",
                    "trigger_doc_name": "Text",
                }
            },
            ensure_ascii=False,
        ),
        is_deleted=False,
    )
    db_session.add(di)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="MGMT",
        due_date=due,
        business_code=f"DOCKET:{docket_id}:1",
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = int(wf.id)
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = _find_task_by_id(tasks, wf_id)
    assert target is not None
    assert target.get("completion_recommendation") is True
    assert target.get("completion_recommendation_kind") == "notice_send_semi_auto"
    assert "Text" in (target.get("completion_recommendation_text") or "")


def test_worklog_api_tasks_fallback_recommendation_from_sent_communication(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.ip_records import Communication
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=2)
    docket_id = "notice-send-recommend-3"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:TEST3",
        name_free="Text Text(3Text Text) · Text",
        due_date=due.isoformat(),
        memo="",
        is_deleted=False,
    )
    db_session.add(di)

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="MGMT",
        due_date=due,
        business_code=f"DOCKET:{docket_id}:1",
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = int(wf.id)

    db_session.add(
        Communication(
            matter_id=matter_id,
            comm_type="M",
            sent_date=date.today().isoformat(),
            note="Text",
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = _find_task_by_id(tasks, wf_id)
    assert target is not None
    assert target.get("completion_recommendation") is True
    assert "Text" in (target.get("completion_recommendation_text") or "")


def test_worklog_api_tasks_fallback_recommendation_from_email_body(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.ip_records import Communication, EmailMessage, EmailMessageMatterLink
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=2)
    docket_id = "notice-send-recommend-4"
    comm_id = "notice-send-recommend-4-comm"
    email_id = "notice-send-recommend-4-email"

    di = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="MGMT",
        name_ref="MGMT:NOTICE_SEND_3D:TEST4",
        name_free="Text Text(3Text Text) · Text Text Text",
        due_date=due.isoformat(),
        memo="",
        is_deleted=False,
    )
    db_session.add(di)

    wf = Workflow(
        case_id=matter_id,
        name=di.name_free,
        status="Pending",
        category="MGMT",
        due_date=due,
        business_code=f"DOCKET:{docket_id}:1",
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = int(wf.id)

    db_session.add(
        Communication(
            comm_id=comm_id,
            matter_id=matter_id,
            comm_type="M",
            sent_date=date.today().isoformat(),
            note="Text Text Text Text Text",
        )
    )
    db_session.add(
        EmailMessage(
            id=email_id,
            linked_comm_id=comm_id,
            mailbox_tag="DOCKET",
            received_at=datetime.utcnow(),
            body_text="Text 6,900Text Text Text. Text Text Text Text.",
        )
    )
    db_session.add(
        EmailMessageMatterLink(
            email_id=email_id,
            matter_id=matter_id,
            comm_id=comm_id,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = _find_task_by_id(tasks, wf_id)
    assert target is not None
    assert target.get("completion_recommendation") is True
    assert "Text Text Text Text Text" in (
        target.get("completion_recommendation_text") or ""
    )


def test_worklog_api_tasks_marks_intake_case_access_candidate_as_completion_recommendation(
    admin_client, db_session, sample_matter
):
    from app.models.user import User
    from app.models.user_access_log import UserAccessLog
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=1)

    owner_user = User(
        username="intake_case_access_owner",
        email="intake_case_access_owner@example.com",
        display_name="Text",
        staff_party_id="intake-case-access-owner-spid",
        role="mgmt_staff",
        is_active=True,
    )
    db_session.add(owner_user)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        category="MGMT",
        due_date=due,
        assignee_id=owner_user.id,
        business_code=f"INTAKE:{matter_id}:{owner_user.id}",
    )
    db_session.add(wf)
    db_session.flush()
    wf_id = int(wf.id)

    db_session.add(
        UserAccessLog(
            user_id=int(owner_user.id),
            method="GET",
            path=f"/case/{matter_id}",
            endpoint="case_work.case_detail",
            blueprint="case_work",
            status_code=200,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = _find_task_by_id(tasks, wf_id)
    assert target is not None
    assert target.get("completion_recommendation") is True
    assert target.get("completion_recommendation_kind") == "intake_case_access"
    assert "Text" in (target.get("completion_recommendation_text") or "")


def test_worklog_api_tasks_filters_intake_case_access_recommendation_bucket(
    admin_client, db_session, sample_matter
):
    from app.models.user import User
    from app.models.user_access_log import UserAccessLog
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    due = date.today() + timedelta(days=1)

    owner_user = User(
        username="intake_case_access_bucket_owner",
        email="intake_case_access_bucket_owner@example.com",
        display_name="Text",
        staff_party_id="intake-case-access-bucket-owner-spid",
        role="mgmt_staff",
        is_active=True,
    )
    db_session.add(owner_user)
    db_session.flush()

    recommended_wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        category="MGMT",
        due_date=due,
        assignee_id=owner_user.id,
        business_code=f"INTAKE:{matter_id}:{owner_user.id}",
    )
    plain_wf = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        category="WORK",
        due_date=due,
    )
    db_session.add_all([recommended_wf, plain_wf])
    db_session.flush()
    recommended_wf_id = int(recommended_wf.id)
    plain_wf_id = int(plain_wf.id)

    db_session.add(
        UserAccessLog(
            user_id=int(owner_user.id),
            method="GET",
            path=f"/case/{matter_id}",
            endpoint="case_work.case_detail",
            blueprint="case_work",
            status_code=200,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30&bucket=recommended")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    assert int(data.get("total") or 0) == 1
    assert _find_task_by_id(tasks, recommended_wf_id) is not None
    assert _find_task_by_id(tasks, plain_wf_id) is None
