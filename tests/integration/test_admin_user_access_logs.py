from __future__ import annotations

from datetime import datetime, timedelta


def test_access_log_hook_records_authenticated_html_get(authenticated_client, sample_user, app):
    # Ensure feature is enabled in tests (default is enabled, but keep explicit).
    app.config["USER_ACCESS_LOG_ENABLED"] = True

    res = authenticated_client.get("/settings/")
    assert res.status_code == 200

    from app.models.user_access_log import UserAccessLog

    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    assert UserAccessLog.query.filter(UserAccessLog.user_id == int(user_id)).count() >= 1


def test_admin_can_view_other_users_access_logs(admin_client, db_session, sample_user):
    from app.models.user_access_log import UserAccessLog

    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    row = UserAccessLog(
        user_id=int(user_id),
        request_id="test_rid",
        method="GET",
        path="/settings/",
        endpoint="settings.index",
        blueprint="settings",
        status_code=200,
        duration_ms=12,
        remote_addr="127.0.0.1",
        user_agent="pytest",
    )
    db_session.add(row)
    db_session.commit()

    res = admin_client.get(f"/admin/usage_logsNewuser_id={int(user_id)}")
    assert res.status_code == 200
    body = res.data.decode("utf-8", errors="ignore")
    assert "/settings/" in body


def test_admin_can_purge_old_access_logs(admin_client, db_session, sample_user):
    from app.models.user_access_log import UserAccessLog

    user_id = getattr(sample_user, "_test_id", None) or sample_user.id

    old = UserAccessLog(
        user_id=int(user_id),
        method="GET",
        path="/case/old",
        status_code=200,
        created_at=datetime.utcnow() - timedelta(days=10),
    )
    recent = UserAccessLog(
        user_id=int(user_id),
        method="GET",
        path="/case/recent",
        status_code=200,
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    db_session.add_all([old, recent])
    db_session.commit()

    before = UserAccessLog.query.filter(UserAccessLog.user_id == int(user_id)).count()
    assert before >= 2

    purge_res = admin_client.post(
        "/admin/usage_logs/purge",
        data={"days": "7", "user_id": str(int(user_id))},
    )
    assert purge_res.status_code in (200, 302)

    remaining = UserAccessLog.query.filter(UserAccessLog.user_id == int(user_id)).all()
    paths = {r.path for r in remaining}
    assert "/case/old" not in paths
    assert "/case/recent" in paths
