from __future__ import annotations

import re
import uuid
from datetime import date


def test_workflow_detail_page_uses_matter_overview_client_context(
    authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.ip_records import VMatterOverview
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)

    db_session.add(
        VMatterOverview(
            matter_id=matter_id,
            our_ref=getattr(sample_matter, "our_ref", None),
            right_name="Sample invention",
            clients="Acme Corp",
            applicants="Demo Applicant",
        )
    )
    wf = Workflow(
        case_id=matter_id,
        name="Overview context task",
        status="Pending",
        assignee_id=user_id,
        attorney_assignee_id=user_id,
        created_by_id=user_id,
        business_code=f"MANUAL:{uuid.uuid4().hex[:10]}",
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert "Acme Corp" in html
    assert "Demo Applicant" in html
    assert "Sample invention" in html


def test_workflow_detail_page_shows_current_assignment_summary(
    authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.user import User
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)

    manager = User(
        username=f"manager_view_{uuid.uuid4().hex[:6]}",
        email=f"manager_view_{uuid.uuid4().hex[:6]}@example.com",
        role="mgmt_director",
        is_active=True,
    )
    manager_username = manager.username
    db_session.add(manager)
    db_session.flush()

    wf = Workflow(
        case_id=matter_id,
        name="Assignment summary task",
        status="In Progress",
        assignee_id=user_id,
        attorney_assignee_id=user_id,
        inspector_id=manager.id,
        created_by_id=user_id,
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert "Assignment summary task" in html
    assert "In Progress" in html
    assert "testuser" in html
    assert manager_username in html


def test_workflow_detail_page_prefers_workflow_title_and_due_dates_over_linked_docket(
    authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)
    docket_id = uuid.uuid4().hex

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="MGMT",
            name_ref="MGMT:STATUS_RED:LinkedDocket",
            name_free="Linked docket deadline",
            due_date="2026-06-17",
        )
    )
    wf = Workflow(
        case_id=matter_id,
        name="Workflow-owned title",
        status="Pending",
        assignee_id=user_id,
        attorney_assignee_id=user_id,
        created_by_id=user_id,
        business_code=f"DOCKET:{docket_id}",
        due_date=date(2026, 4, 8),
        legal_due_date=date(2026, 4, 8),
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert "Workflow-owned title" in html
    assert "Linked docket deadline" not in html
    assert "2026-04-08" in html
    assert "Linked deadline keeps its own task due date." in html


def test_workflow_detail_page_uses_final_and_internal_due_labels(
    authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)

    wf = Workflow(
        case_id=matter_id,
        name="Dual due date task",
        status="Pending",
        assignee_id=user_id,
        created_by_id=user_id,
        legal_due_date=date(2026, 4, 13),
        due_date=date(2026, 4, 11),
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    assert "Final Due date(Statutory deadline)" in html
    assert "Internal Due date" in html
    assert 'name="internal_due_date"' in html
    assert "Current Internal Due date" in html


def test_workflow_detail_page_keeps_internal_input_blank_when_following_final_due(
    authenticated_client, db_session, sample_user, sample_matter
):
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    user_id = int(getattr(sample_user, "_test_id", None) or sample_user.id)

    wf = Workflow(
        case_id=matter_id,
        name="Final due date task",
        status="Pending",
        assignee_id=user_id,
        created_by_id=user_id,
        legal_due_date=date(2026, 4, 13),
        due_date=date(2026, 4, 13),
    )
    db_session.add(wf)
    db_session.commit()

    resp = authenticated_client.get(f"/workflow/{wf.id}")
    assert resp.status_code == 200

    html = resp.data.decode("utf-8")
    match = re.search(r'name="internal_due_date"[^>]*value="([^"]*)"', html)
    assert match is not None
    assert match.group(1) == ""
    assert "Current final due date is being used." in html
