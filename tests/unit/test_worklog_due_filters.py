from __future__ import annotations

import uuid
from datetime import date, timedelta


def _task_names(payload: dict) -> set[str]:
    tasks = (payload or {}).get("tasks") or []
    return {str(task.get("task_name") or "") for task in tasks}


def test_worklog_api_tasks_hides_workflow_deadline_suffix(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    base_name = f"display-title-{uuid.uuid4().hex}"

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=f"{base_name} [Deadline]",
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=3),
            legal_due_date=date.today() + timedelta(days=3),
        )
    )
    db_session.commit()

    resp = admin_client.get(f"/worklog/api/tasksNewfilter=todo&days=365&search={base_name}")
    assert resp.status_code == 200
    names = _task_names(resp.get_json() or {})
    assert base_name in names
    assert f"{base_name} [Deadline]" not in names


def test_worklog_api_tasks_filters_by_due_range(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    suffix = uuid.uuid4().hex[:8]
    early_name = f"due-range-early-{suffix}"
    in_range_name = f"due-range-target-{suffix}"
    late_name = f"due-range-late-{suffix}"

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=early_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=5),
            ),
            Workflow(
                case_id=matter_id,
                name=in_range_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=12),
            ),
            Workflow(
                case_id=matter_id,
                name=late_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=28),
            ),
        ]
    )
    db_session.commit()

    due_from = (today + timedelta(days=10)).isoformat()
    due_to = (today + timedelta(days=20)).isoformat()

    resp = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=365&due_from={due_from}&due_to={due_to}"
    )
    assert resp.status_code == 200
    names = _task_names(resp.get_json() or {})
    assert in_range_name in names
    assert early_name not in names
    assert late_name not in names

    resp_swapped = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=365&due_from={due_to}&due_to={due_from}"
    )
    assert resp_swapped.status_code == 200
    swapped_names = _task_names(resp_swapped.get_json() or {})
    assert swapped_names == names


def test_worklog_api_summary_filters_by_due_range(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=f"summary-range-target-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=12),
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-range-late-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=40),
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-range-completed-target-{uuid.uuid4().hex[:8]}",
                status="Completed",
                category="WORK",
                due_date=today + timedelta(days=14),
                completed_date=today - timedelta(days=1),
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-range-completed-late-{uuid.uuid4().hex[:8]}",
                status="Completed",
                category="WORK",
                due_date=today + timedelta(days=60),
                completed_date=today - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    due_from = (today + timedelta(days=10)).isoformat()
    due_to = (today + timedelta(days=20)).isoformat()

    resp = admin_client.get(f"/worklog/api/summaryNewdays=365&due_from={due_from}&due_to={due_to}")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1
    assert int(data.get("urgent") or 0) == 0
    assert int(data.get("overdue") or 0) == 0
    assert int(data.get("completed_week") or 0) == 1


def test_worklog_api_tasks_filters_by_selected_final_due_axis(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    internal_due = today + timedelta(days=5)
    final_due = today + timedelta(days=20)
    target_name = f"final-axis-target-{uuid.uuid4().hex[:8]}"

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=target_name,
            status="Pending",
            category="WORK",
            due_date=internal_due,
            legal_due_date=final_due,
        )
    )
    db_session.commit()

    due_from = (today + timedelta(days=18)).isoformat()
    due_to = (today + timedelta(days=22)).isoformat()

    resp_final = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=365&due_axis=final&due_from={due_from}&due_to={due_to}"
    )
    assert resp_final.status_code == 200
    final_names = _task_names(resp_final.get_json() or {})
    assert target_name in final_names

    tasks = (resp_final.get_json() or {}).get("tasks") or []
    task_row = next(task for task in tasks if str(task.get("task_name") or "") == target_name)
    assert task_row["due_date"] == final_due.isoformat()
    assert task_row["final_due_date"] == final_due.isoformat()
    assert task_row["internal_due_date"] == internal_due.isoformat()

    resp_internal = admin_client.get(
        f"/worklog/api/tasksNewfilter=todo&days=365&due_axis=internal&due_from={due_from}&due_to={due_to}"
    )
    assert resp_internal.status_code == 200
    internal_names = _task_names(resp_internal.get_json() or {})
    assert target_name not in internal_names


def test_worklog_api_tasks_hides_duplicate_internal_due_when_same_as_final(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    same_due = today + timedelta(days=9)
    split_internal = today + timedelta(days=4)
    split_final = today + timedelta(days=12)

    suffix = uuid.uuid4().hex[:8]
    same_name = f"same-due-{suffix}"
    split_name = f"split-due-{suffix}"

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=same_name,
                status="Pending",
                category="WORK",
                due_date=same_due,
                legal_due_date=same_due,
            ),
            Workflow(
                case_id=matter_id,
                name=split_name,
                status="Pending",
                category="WORK",
                due_date=split_internal,
                legal_due_date=split_final,
            ),
        ]
    )
    db_session.commit()

    resp_all = admin_client.get("/worklog/api/tasksNewfilter=todo&days=365")
    assert resp_all.status_code == 200
    tasks = (resp_all.get_json() or {}).get("tasks") or []
    by_name = {str(task.get("task_name") or ""): task for task in tasks}
    assert by_name[same_name]["final_due_date"] == same_due.isoformat()
    assert by_name[same_name]["internal_due_date"] in (None, "")
    assert by_name[split_name]["final_due_date"] == split_final.isoformat()
    assert by_name[split_name]["internal_due_date"] == split_internal.isoformat()

    resp_internal = admin_client.get("/worklog/api/tasksNewfilter=todo&days=365&due_axis=internal")
    assert resp_internal.status_code == 200
    names = _task_names(resp_internal.get_json() or {})
    assert split_name in names
    assert same_name not in names


def test_worklog_api_tasks_prefers_workflow_due_dates_over_linked_docket_dates(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = uuid.uuid4().hex
    today = date.today()
    workflow_internal_due = today + timedelta(days=3)
    workflow_final_due = today + timedelta(days=10)
    docket_final_due = today + timedelta(days=25)

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_free="workflow source docket",
            due_date=docket_final_due.isoformat(),
            extended_due_date=(today + timedelta(days=21)).isoformat(),
            is_deleted=False,
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="workflow source task",
            status="Pending",
            category="WORK",
            business_code=f"DOCKET:{docket_id}",
            due_date=workflow_internal_due,
            legal_due_date=workflow_final_due,
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=365")
    assert resp.status_code == 200
    tasks = (resp.get_json() or {}).get("tasks") or []
    row = next(task for task in tasks if str(task.get("task_name") or "") == "workflow source task")
    assert row["final_due_date"] == workflow_final_due.isoformat()
    assert row["internal_due_date"] == workflow_internal_due.isoformat()


def test_worklog_api_calendar_events_uses_workflow_due_dates_for_linked_workflows(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = uuid.uuid4().hex
    today = date.today()
    workflow_internal_due = today + timedelta(days=2)
    workflow_final_due = today + timedelta(days=6)

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_free="calendar linked docket",
            due_date=(today + timedelta(days=20)).isoformat(),
            extended_due_date=(today + timedelta(days=18)).isoformat(),
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=matter_id,
        name="calendar linked workflow",
        status="Pending",
        category="WORK",
        business_code=f"DOCKET:{docket_id}",
        due_date=workflow_internal_due,
        legal_due_date=workflow_final_due,
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = str(wf.id)

    start = (today - timedelta(days=1)).isoformat()
    end = (today + timedelta(days=10)).isoformat()
    resp = admin_client.get(f"/worklog/api/calendar-eventsNewstart={start}&end={end}")
    assert resp.status_code == 200
    events = resp.get_json() or []
    by_axis = {
        str(event.get("due_axis") or ""): event
        for event in events
        if str(event.get("workflow_id") or "") == wf_id
    }
    assert by_axis["final"]["start"] == workflow_final_due.isoformat()
    assert by_axis["internal"]["start"] == workflow_internal_due.isoformat()


def test_worklog_api_calendar_events_in_all_axis_keeps_final_and_internal_range_matches(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    final_only = Workflow(
        case_id=matter_id,
        name=f"calendar-final-only-{uuid.uuid4().hex[:8]}",
        status="Pending",
        category="WORK",
        due_date=today - timedelta(days=10),
        legal_due_date=today + timedelta(days=4),
    )
    internal_only = Workflow(
        case_id=matter_id,
        name=f"calendar-internal-only-{uuid.uuid4().hex[:8]}",
        status="Pending",
        category="WORK",
        due_date=today + timedelta(days=5),
        legal_due_date=today + timedelta(days=20),
    )
    db_session.add_all([final_only, internal_only])
    db_session.commit()
    final_only_id = str(final_only.id)
    internal_only_id = str(internal_only.id)
    final_only_due = final_only.legal_due_date.isoformat()
    internal_only_due = internal_only.due_date.isoformat()

    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=7)).isoformat()
    resp = admin_client.get(f"/worklog/api/calendar-eventsNewstart={start}&end={end}")
    assert resp.status_code == 200
    events = resp.get_json() or []
    by_workflow_and_axis = {
        (str(event.get("workflow_id") or ""), str(event.get("due_axis") or "")): event
        for event in events
    }

    assert by_workflow_and_axis[(final_only_id, "final")]["start"] == final_only_due
    assert (final_only_id, "internal") not in by_workflow_and_axis
    assert by_workflow_and_axis[(internal_only_id, "internal")]["start"] == internal_only_due
    assert (internal_only_id, "final") not in by_workflow_and_axis


def test_worklog_api_calendar_events_hide_duplicate_internal_due_when_same_as_final(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    same_due = today + timedelta(days=10)
    split_internal = today + timedelta(days=8)
    split_final = today + timedelta(days=12)

    same = Workflow(
        case_id=matter_id,
        name=f"calendar-same-due-{uuid.uuid4().hex[:8]}",
        status="Pending",
        category="WORK",
        due_date=same_due,
        legal_due_date=same_due,
    )
    split = Workflow(
        case_id=matter_id,
        name=f"calendar-split-due-{uuid.uuid4().hex[:8]}",
        status="Pending",
        category="WORK",
        due_date=split_internal,
        legal_due_date=split_final,
    )
    db_session.add_all([same, split])
    db_session.commit()
    same_key = str(same.id)
    split_key = str(split.id)

    start = (today + timedelta(days=1)).isoformat()
    end = (today + timedelta(days=20)).isoformat()
    resp = admin_client.get(f"/worklog/api/calendar-eventsNewstart={start}&end={end}")
    assert resp.status_code == 200
    events = resp.get_json() or []
    by_workflow_and_axis = {
        (str(event.get("workflow_id") or ""), str(event.get("due_axis") or "")): event
        for event in events
    }

    assert (same_key, "final") in by_workflow_and_axis
    assert (same_key, "internal") not in by_workflow_and_axis
    assert (split_key, "final") in by_workflow_and_axis
    assert (split_key, "internal") in by_workflow_and_axis

    same_final = by_workflow_and_axis[(same_key, "final")]
    assert same_final.get("axis_short_label") == "Final"
    assert "deadline-calendar-event--axis-final" in (same_final.get("classNames") or [])
    assert same_final.get("backgroundColor") == "#eff6ff"


def test_worklog_api_calendar_events_falls_back_to_linked_docket_due_dates_when_workflow_blank(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = uuid.uuid4().hex
    today = date.today()
    docket_internal_due = today + timedelta(days=4)
    docket_final_due = today + timedelta(days=7)

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="WORK",
            name_free="calendar fallback docket",
            due_date=docket_final_due.isoformat(),
            extended_due_date=docket_internal_due.isoformat(),
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=matter_id,
        name="calendar fallback workflow",
        status="Pending",
        category="WORK",
        business_code=f"DOCKET:{docket_id}",
        due_date=None,
        legal_due_date=None,
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = str(wf.id)

    start = (today - timedelta(days=1)).isoformat()
    end = (today + timedelta(days=10)).isoformat()
    resp = admin_client.get(f"/worklog/api/calendar-eventsNewstart={start}&end={end}")
    assert resp.status_code == 200
    events = resp.get_json() or []
    by_axis = {
        str(event.get("due_axis") or ""): event
        for event in events
        if str(event.get("workflow_id") or "") == wf_id
    }
    assert by_axis["final"]["start"] == docket_final_due.isoformat()
    assert by_axis["internal"]["start"] == docket_internal_due.isoformat()


def test_worklog_api_summary_uses_selected_due_axis_for_overdue_bucket(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=f"summary-internal-overdue-{uuid.uuid4().hex[:8]}",
            status="Pending",
            category="WORK",
            due_date=today - timedelta(days=3),
            legal_due_date=today + timedelta(days=20),
        )
    )
    db_session.commit()

    resp_internal = admin_client.get(
        "/worklog/api/summary",
        query_string={"days": "365", "due_axis": "internal"},
    )
    assert resp_internal.status_code == 200
    internal_data = resp_internal.get_json() or {}
    assert int(internal_data.get("overdue") or 0) == 1

    resp_final = admin_client.get(
        "/worklog/api/summary",
        query_string={"days": "365", "due_axis": "final"},
    )
    assert resp_final.status_code == 200
    final_data = resp_final.get_json() or {}
    assert int(final_data.get("overdue") or 0) == 0


def test_worklog_api_summary_filters_by_search(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    search_suffix = uuid.uuid4().hex[:8]
    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=f"summary-search-match-{search_suffix}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-search-other-{search_suffix}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(
        f"/worklog/api/summaryNewdays=365&search=summary-search-match-{search_suffix}"
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1
    assert int(data.get("urgent") or 0) == 0
    assert int(data.get("overdue") or 0) == 0


def test_worklog_api_summary_search_matches_compact_case_ref(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    our_ref = str(getattr(sample_matter, "our_ref", "") or "")
    today = date.today()

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=f"compact-ref-task-{uuid.uuid4().hex[:8]}",
            status="Pending",
            category="WORK",
            due_date=today + timedelta(days=10),
        )
    )
    db_session.commit()

    compact_query = our_ref.replace("-", "").replace(" ", "").lower()
    resp = admin_client.get(f"/worklog/api/summaryNewdays=365&search={compact_query}")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1


def test_worklog_api_tasks_supports_search_expression_fields_and_negation(
    admin_client, db_session, sample_matter
):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    owner_name = f"expr-owner-{uuid.uuid4().hex[:6]}"

    owner = User(
        username=f"worklog_expr_owner_{uuid.uuid4().hex[:8]}",
        email=f"worklog_expr_owner_{uuid.uuid4().hex[:8]}@example.com",
        display_name=owner_name,
        staff_party_id=f"spid-expr-owner-{uuid.uuid4().hex[:8]}",
        role="patent_staff",
        is_active=True,
    )
    db_session.add(owner)
    db_session.flush()

    search_suffix = uuid.uuid4().hex[:8]
    keep_task_name = f"expr-keep-{search_suffix}"
    exclude_task_name = f"expr-blocked-{search_suffix}"
    other_task_name = f"expr-other-owner-{search_suffix}"

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=keep_task_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=7),
                assignee_id=owner.id,
                note="alpha memo",
            ),
            Workflow(
                case_id=matter_id,
                name=exclude_task_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=7),
                assignee_id=owner.id,
                note="alpha memo blocked",
            ),
            Workflow(
                case_id=matter_id,
                name=other_task_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=7),
                note="alpha memo",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(
        "/worklog/api/tasks",
        query_string={
            "filter": "todo",
            "days": "365",
            "search": f'owner:"{owner_name}" memo:"alpha memo" -blocked',
        },
    )
    assert resp.status_code == 200
    names = _task_names(resp.get_json() or {})
    assert keep_task_name in names
    assert exclude_task_name not in names
    assert other_task_name not in names


def test_worklog_api_summary_supports_memo_field_search(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    memo_suffix = uuid.uuid4().hex[:8]
    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=f"memo-search-match-{memo_suffix}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
                note=f"memo-token-{memo_suffix}",
            ),
            Workflow(
                case_id=matter_id,
                name=f"memo-search-other-{memo_suffix}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
                note="unrelated memo",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(
        "/worklog/api/summary",
        query_string={"days": "365", "search": f'memo:"memo-token-{memo_suffix}"'},
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1


def test_worklog_api_summary_supports_or_search_expression(admin_client, db_session, sample_matter):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name="OR Text A Text",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
            ),
            Workflow(
                case_id=matter_id,
                name="OR Text B Text",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
            ),
            Workflow(
                case_id=matter_id,
                name="OR Text Text Text",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=10),
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(
        "/worklog/api/summary",
        query_string={
            "days": "365",
            "search": 'task:"OR Text A Text" OR task:"OR Text B Text"',
        },
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 2


def test_worklog_api_tasks_filters_by_flow_owner_search(admin_client, db_session, sample_matter):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    owner_name = f"flow-owner-{uuid.uuid4().hex[:6]}"

    matched_owner = User(
        username=f"worklog_owner_match_{uuid.uuid4().hex[:8]}",
        email=f"worklog_owner_match_{uuid.uuid4().hex[:8]}@example.com",
        display_name=owner_name,
        staff_party_id=f"spid-owner-match-{uuid.uuid4().hex[:8]}",
        role="patent_staff",
        is_active=True,
    )
    other_owner = User(
        username=f"worklog_owner_other_{uuid.uuid4().hex[:8]}",
        email=f"worklog_owner_other_{uuid.uuid4().hex[:8]}@example.com",
        display_name=f"other-owner-{uuid.uuid4().hex[:6]}",
        staff_party_id=f"spid-owner-other-{uuid.uuid4().hex[:8]}",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([matched_owner, other_owner])
    db_session.flush()

    owner_suffix = uuid.uuid4().hex[:8]
    matched_task_name = f"owner-match-task-{owner_suffix}"
    other_task_name = f"owner-other-task-{owner_suffix}"
    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=matched_task_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=7),
                assignee_id=matched_owner.id,
            ),
            Workflow(
                case_id=matter_id,
                name=other_task_name,
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=7),
                assignee_id=other_owner.id,
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(f"/worklog/api/tasksNewfilter=todo&days=365&search={owner_name}")
    assert resp.status_code == 200
    names = _task_names(resp.get_json() or {})
    assert matched_task_name in names
    assert other_task_name not in names


def test_worklog_api_summary_filters_by_flow_owner_search(admin_client, db_session, sample_matter):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    today = date.today()
    owner_name = f"Text-{uuid.uuid4().hex[:6]}"

    matched_owner = User(
        username=f"worklog_summary_owner_{uuid.uuid4().hex[:8]}",
        email=f"worklog_summary_owner_{uuid.uuid4().hex[:8]}@example.com",
        display_name=owner_name,
        staff_party_id=f"spid-summary-owner-{uuid.uuid4().hex[:8]}",
        role="patent_staff",
        is_active=True,
    )
    other_owner = User(
        username=f"worklog_summary_other_{uuid.uuid4().hex[:8]}",
        email=f"worklog_summary_other_{uuid.uuid4().hex[:8]}@example.com",
        display_name=f"summary-other-owner-{uuid.uuid4().hex[:6]}",
        staff_party_id=f"spid-summary-other-{uuid.uuid4().hex[:8]}",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([matched_owner, other_owner])
    db_session.flush()

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name=f"summary-owner-match-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=9),
                assignee_id=matched_owner.id,
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-owner-other-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=today + timedelta(days=9),
                assignee_id=other_owner.id,
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(f"/worklog/api/summaryNewdays=365&search={owner_name}")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1
    assert int(data.get("urgent") or 0) == 0
    assert int(data.get("overdue") or 0) == 0


def test_worklog_api_tasks_excludes_hidden_docket_workflow_by_visible_from(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    target_due = date.today() + timedelta(days=1500)
    hidden_docket_id = f"wf-hidden-{uuid.uuid4().hex[:12]}"
    visible_docket_id = f"wf-visible-{uuid.uuid4().hex[:12]}"
    visible_suffix = uuid.uuid4().hex[:8]
    hidden_name = f"hidden-visible-from-task-{visible_suffix}"
    visible_name = f"visible-visible-from-task-{visible_suffix}"

    db_session.add_all(
        [
            DocketItem(
                docket_id=hidden_docket_id,
                matter_id=matter_id,
                category="MGMT",
                name_ref=f"MGMT:STATUS_RED:HiddenVisibleFrom{visible_suffix}",
                name_free=hidden_name,
                due_date=target_due.isoformat(),
                visible_from_date=(target_due - timedelta(days=60)).isoformat(),
                is_deleted=False,
            ),
            DocketItem(
                docket_id=visible_docket_id,
                matter_id=matter_id,
                category="MGMT",
                name_ref=f"MGMT:STATUS_RED:VisibleFrom{visible_suffix}",
                name_free=visible_name,
                due_date=target_due.isoformat(),
                visible_from_date=(date.today() - timedelta(days=3)).isoformat(),
                is_deleted=False,
            ),
            Workflow(
                case_id=matter_id,
                name=hidden_name,
                status="Pending",
                category="MGMT",
                due_date=target_due,
                business_code=f"DOCKET:{hidden_docket_id}",
            ),
            Workflow(
                case_id=matter_id,
                name=visible_name,
                status="Pending",
                category="MGMT",
                due_date=target_due,
                business_code=f"DOCKET:{visible_docket_id}",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(
        "/worklog/api/tasks",
        query_string={"filter": "todo", "days": "all"},
    )
    assert resp.status_code == 200
    names = _task_names(resp.get_json() or {})
    assert visible_name in names
    assert hidden_name not in names


def test_worklog_api_summary_excludes_hidden_docket_workflow_by_visible_from(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    target_due = date.today() + timedelta(days=1499)
    hidden_docket_id = f"wf-hidden-sum-{uuid.uuid4().hex[:12]}"
    visible_docket_id = f"wf-visible-sum-{uuid.uuid4().hex[:12]}"

    db_session.add_all(
        [
            DocketItem(
                docket_id=hidden_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:OA:VISIBLE:HIDDEN",
                name_free=f"summary-hidden-visible-from-{uuid.uuid4().hex[:8]}",
                due_date=target_due.isoformat(),
                visible_from_date=(target_due - timedelta(days=30)).isoformat(),
                is_deleted=False,
            ),
            DocketItem(
                docket_id=visible_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:OA:VISIBLE:OPEN",
                name_free=f"summary-visible-visible-from-{uuid.uuid4().hex[:8]}",
                due_date=target_due.isoformat(),
                visible_from_date=(date.today() - timedelta(days=3)).isoformat(),
                is_deleted=False,
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-hidden-workflow-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=target_due,
                business_code=f"DOCKET:{hidden_docket_id}",
            ),
            Workflow(
                case_id=matter_id,
                name=f"summary-visible-workflow-{uuid.uuid4().hex[:8]}",
                status="Pending",
                category="WORK",
                due_date=target_due,
                business_code=f"DOCKET:{visible_docket_id}",
            ),
        ]
    )
    db_session.commit()

    due_token = target_due.isoformat()
    resp = admin_client.get(
        "/worklog/api/summary",
        query_string={"days": "all", "due_from": due_token, "due_to": due_token},
    )
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1
    assert int(data.get("urgent") or 0) == 0
    assert int(data.get("overdue") or 0) == 0


def test_worklog_api_owners_excludes_hidden_docket_workflow_by_visible_from(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    target_due = date.today() + timedelta(days=1498)
    hidden_docket_id = f"wf-hidden-owner-{uuid.uuid4().hex[:12]}"
    visible_docket_id = f"wf-visible-owner-{uuid.uuid4().hex[:12]}"

    owner_suffix = uuid.uuid4().hex[:8]
    hidden_staff_party_id = f"spid-hidden-owner-{owner_suffix}"
    visible_staff_party_id = f"spid-visible-owner-{owner_suffix}"
    hidden_user = User(
        username=f"hidden_owner_user_{owner_suffix}",
        email=f"hidden_owner_user_{owner_suffix}@example.com",
        display_name=f"Hidden owner {owner_suffix}",
        staff_party_id=hidden_staff_party_id,
        role="patent_staff",
        is_active=True,
    )
    visible_user = User(
        username=f"visible_owner_user_{owner_suffix}",
        email=f"visible_owner_user_{owner_suffix}@example.com",
        display_name=f"Visible owner {owner_suffix}",
        staff_party_id=visible_staff_party_id,
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([hidden_user, visible_user])
    db_session.flush()

    db_session.add_all(
        [
            DocketItem(
                docket_id=hidden_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:OA:OWNER:HIDDEN",
                name_free=f"owner-hidden-visible-from-{owner_suffix}",
                due_date=target_due.isoformat(),
                visible_from_date=(target_due - timedelta(days=30)).isoformat(),
                is_deleted=False,
            ),
            DocketItem(
                docket_id=visible_docket_id,
                matter_id=matter_id,
                category="WORK",
                name_ref="NOTICE:OA:OWNER:OPEN",
                name_free=f"owner-visible-visible-from-{owner_suffix}",
                due_date=target_due.isoformat(),
                visible_from_date=(date.today() - timedelta(days=3)).isoformat(),
                is_deleted=False,
            ),
            Workflow(
                case_id=matter_id,
                name=f"owner-hidden-workflow-{owner_suffix}",
                status="Pending",
                category="WORK",
                due_date=target_due,
                assignee_id=hidden_user.id,
                business_code=f"DOCKET:{hidden_docket_id}",
            ),
            Workflow(
                case_id=matter_id,
                name=f"owner-visible-workflow-{owner_suffix}",
                status="Pending",
                category="WORK",
                due_date=target_due,
                assignee_id=visible_user.id,
                business_code=f"DOCKET:{visible_docket_id}",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/owners", query_string={"owner_role": "owner"})
    assert resp.status_code == 200
    owners = (resp.get_json() or {}).get("owners") or []
    owner_ids = {str(row.get("id") or "").strip() for row in owners}
    assert visible_staff_party_id in owner_ids
    assert hidden_staff_party_id not in owner_ids
