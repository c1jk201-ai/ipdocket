from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest

from app.models.email_automation import EmailMessage
from app.models.ip_records import DocketItem
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog


@pytest.fixture
def mgmt_staff_client(client, db_session):
    user = User(
        username="mgmt_dashboard_user",
        email="mgmt_dashboard_user@example.com",
        role="mgmt_staff",
        is_active=True,
        staff_party_id="sp-mgmt-dashboard",
    )
    db_session.add(user)
    db_session.commit()
    user._test_id = user.id
    with client.session_transaction() as session:
        session["_user_id"] = user.id
        session["_fresh"] = True
    return client, user


def test_mgmt_dashboard_renders_aggregated_metrics(mgmt_staff_client, db_session, sample_matter):
    client, user = mgmt_staff_client
    today = date.today()

    db_session.add(
        Workflow(
            case_id=str(sample_matter.matter_id),
            name="Open Workflow",
            status="Pending",
            assignee_id=user.id,
            due_date=today - timedelta(days=1),
        )
    )
    db_session.add(
        Workflow(
            case_id=str(sample_matter.matter_id),
            name="Done Workflow",
            status="Completed",
            assignee_id=user.id,
            completed_date=today - timedelta(days=2),
        )
    )
    db_session.add(
        WorkLog(
            docket_id=uuid.uuid4().hex,
            matter_id=str(sample_matter.matter_id),
            task_name="Completed Task",
            status="completed",
            completed_by_id=user.id,
            completed_at=datetime.utcnow() - timedelta(days=1),
            owner_staff_party_id=user.staff_party_id,
        )
    )
    db_session.add(
        DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(sample_matter.matter_id),
            category="WORK",
            name_ref="MGMT:DASHBOARD:TEST",
            due_date=(today - timedelta(days=2)).isoformat(),
            owner_staff_party_id=user.staff_party_id,
        )
    )
    db_session.add(
        EmailMessage(
            id=uuid.uuid4().hex,
            provider_message_id=f"provider-{uuid.uuid4().hex}",
            subject="Mgmt queue review",
            processing_status="REVIEW",
        )
    )
    db_session.add(
        EmailMessage(
            id=uuid.uuid4().hex,
            provider_message_id=f"provider-{uuid.uuid4().hex}",
            subject="Mgmt queue ready",
            processing_status="READY",
        )
    )
    db_session.commit()

    res = client.get("/mgmt/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Management Dashboard" in html
    assert "Processed in the last 7 days" in html
    assert "Delayed tasks" in html
    assert "Automation backlog" in html
    assert "User KPI Board" in html
    assert user.username in html


def test_mgmt_dashboard_requires_management_role(authenticated_client):
    res = authenticated_client.get("/mgmt/")
    assert res.status_code == 403
