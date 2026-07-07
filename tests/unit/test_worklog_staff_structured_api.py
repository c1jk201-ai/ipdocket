from __future__ import annotations

from datetime import date, timedelta


def test_worklog_api_tasks_includes_structured_staff_lists(admin_client, db_session, sample_matter):
    """
    Worklog API should return structured staff lists (id + name) to enable drill-down links.
    """
    from app.models.party import Party, PartyStaff
    from app.models.ip_records import MatterStaffAssignment
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    staff_party_id = "spid-test-1"
    staff_name = "Text"

    db_session.add(Party(party_id=staff_party_id, name_display=staff_name))
    db_session.add(PartyStaff(party_id=staff_party_id, staff_code=staff_party_id, active=1))
    db_session.add(
        MatterStaffAssignment(
            matter_id=matter_id,
            staff_party_id=staff_party_id,
            staff_role_code="attorney",
            raw_text=staff_name,
        )
    )

    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Text Text",
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=1),
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (
            t
            for t in tasks
            if t.get("matter_id") == matter_id and t.get("task_name") == "Text Text"
        ),
        None,
    )
    assert target is not None

    assert isinstance(target.get("attorneys"), list)
    assert any(
        p.get("id") == staff_party_id and p.get("name") == staff_name for p in target["attorneys"]
    )
    assert staff_name in (target.get("attorney_names") or "")


def test_worklog_api_tasks_includes_applicant_name(admin_client, db_session, sample_matter):
    from app.models.ip_records import MatterPartyRole
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    applicant_name = "Text Text Text"
    task_name = "Text Text Text"

    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            role_code="APPLICANT",
            raw_text=applicant_name,
            seq=1,
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=1),
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (t for t in tasks if t.get("matter_id") == matter_id and t.get("task_name") == task_name),
        None,
    )
    assert target is not None
    assert target.get("applicant_name") == applicant_name


def test_worklog_api_tasks_includes_applicant_client_id(admin_client, db_session, sample_matter):
    from app.models.client import Client
    from app.models.ip_records import MatterPartyRole
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    party_id = "party-worklog-applicant-client-1"
    applicant_name = "Text Text"
    task_name = "Text Text Text"

    client = Client(name="Text Text", party_id=party_id, is_deleted=False)
    db_session.add(client)
    db_session.flush()
    client_id = int(client.id)

    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            role_code="applicant",
            party_id=party_id,
            raw_text=applicant_name,
            seq=1,
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="WORK",
            due_date=date.today() + timedelta(days=2),
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (t for t in tasks if t.get("matter_id") == matter_id and t.get("task_name") == task_name),
        None,
    )
    assert target is not None
    assert str(target.get("applicant_client_id") or "") == str(client_id)


def test_worklog_api_tasks_merges_legacy_split_docket_workflows(
    admin_client, db_session, sample_matter
):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = "legacy-merge-docket-1"

    manager_user = User(
        username="legacy_manager_1",
        email="legacy_manager_1@example.com",
        display_name="Text",
        staff_party_id="legacy-manager-spid-1",
        role="mgmt_staff",
        is_active=True,
    )
    handler_user = User(
        username="legacy_handler_1",
        email="legacy_handler_1@example.com",
        display_name="Text",
        staff_party_id="legacy-handler-spid-1",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([manager_user, handler_user])
    db_session.flush()

    due = date.today() + timedelta(days=1)
    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name="Text",
                status="Pending",
                category="MGMT",
                due_date=due,
                assignee_id=manager_user.id,
                business_code=f"DOCKET:{docket_id}:{manager_user.id}",
                note="Auto Create: DocketItem legacy split",
            ),
            Workflow(
                case_id=matter_id,
                name="Text",
                status="Pending",
                category="WORK",
                due_date=due,
                assignee_id=handler_user.id,
                business_code=f"DOCKET:{docket_id}:{handler_user.id}",
                note="Auto Create: DocketItem legacy split",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (
            t
            for t in tasks
            if t.get("matter_id") == matter_id and t.get("task_name") == "Text"
        ),
        None,
    )
    assert target is not None
    assert target.get("id") == docket_id
    assert target.get("category_type") == "hybrid"
    assert target.get("category_display") == "HYBRID"
    assert target.get("owner_id") in (None, "")
    owner_names = str(target.get("owner_name") or "")
    assert "Text" in owner_names
    assert "Text" in owner_names


def test_worklog_api_summary_merges_legacy_split_docket_workflows(
    admin_client, db_session, sample_matter
):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    docket_id = "legacy-merge-summary-1"

    manager_user = User(
        username="legacy_summary_manager_1",
        email="legacy_summary_manager_1@example.com",
        display_name="Text",
        staff_party_id="legacy-summary-manager-spid-1",
        role="mgmt_staff",
        is_active=True,
    )
    handler_user = User(
        username="legacy_summary_handler_1",
        email="legacy_summary_handler_1@example.com",
        display_name="Text",
        staff_party_id="legacy-summary-handler-spid-1",
        role="patent_staff",
        is_active=True,
    )
    db_session.add_all([manager_user, handler_user])
    db_session.flush()

    due = date.today() + timedelta(days=20)
    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name="Text Text Text",
                status="Pending",
                category="MGMT",
                due_date=due,
                assignee_id=manager_user.id,
                business_code=f"DOCKET:{docket_id}:{manager_user.id}",
                note="Auto Create: DocketItem legacy summary split",
            ),
            Workflow(
                case_id=matter_id,
                name="Text Text Text",
                status="Pending",
                category="WORK",
                due_date=due,
                assignee_id=handler_user.id,
                business_code=f"DOCKET:{docket_id}:{handler_user.id}",
                note="Auto Create: DocketItem legacy summary split",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/summary?days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    assert int(data.get("pending") or 0) == 1
    assert int(data.get("urgent") or 0) == 0


def test_worklog_api_tasks_marks_mgmt_work_category_as_mixed(
    admin_client, db_session, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    task_name = "Text Text"

    db_session.add(
        Workflow(
            case_id=matter_id,
            name=task_name,
            status="Pending",
            category="MGMT_WORK",
            due_date=date.today() + timedelta(days=2),
        )
    )
    db_session.commit()

    resp = admin_client.get("/worklog/api/tasksNewfilter=todo&days=30")
    assert resp.status_code == 200
    data = resp.get_json() or {}
    tasks = data.get("tasks") or []

    target = next(
        (t for t in tasks if t.get("matter_id") == matter_id and t.get("task_name") == task_name),
        None,
    )
    assert target is not None
    assert target.get("category_type") == "hybrid"
    assert target.get("category_display") == "HYBRID"
