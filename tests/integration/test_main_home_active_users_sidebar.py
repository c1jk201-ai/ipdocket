from __future__ import annotations

from datetime import datetime, timedelta

from app.models.user import User
from app.models.user_access_log import UserAccessLog


def test_home_secondary_sidebar_shows_active_users(admin_client, db_session):
    teammate = User(
        username="sidebar-user",
        email="sidebar-user@example.com",
        display_name="Text Text",
        department="Text",
        position="Staff",
        role="patent_staff",
        is_active=True,
    )
    db_session.add(teammate)
    db_session.flush()
    db_session.add(
        UserAccessLog(
            user_id=teammate.id,
            created_at=datetime.utcnow() - timedelta(minutes=1),
            method="GET",
            path="/case/list",
            endpoint="case_work.case_list",
            blueprint="case_work",
            status_code=200,
        )
    )
    db_session.commit()

    response = admin_client.get("/")

    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'id="homeActiveUsersPanel"' in html
    assert "Active users" in html
    assert "Text Text" in html
    assert "Text · Staff" in html
    assert "m ago" in html


def test_non_home_secondary_sidebar_hides_active_users(admin_client):
    response = admin_client.get("/worklog/")

    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert 'id="homeActiveUsersPanel"' not in html
