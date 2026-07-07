from __future__ import annotations

from unittest.mock import patch


def test_login_page_shows_local_password_form(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "true")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "false")

    res = client.get("/auth/login")

    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert 'name="username"' in body
    assert 'name="password"' in body
    assert "Google" not in body
    assert "OAuth" not in body


def test_login_page_requires_password_login_when_disabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "true")

    res = client.get("/auth/login")

    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert "Password sign-in is disabled" in body
    assert 'name="username"' not in body
    assert "Google" not in body
    assert "OAuth" not in body


def test_login_failure_message_is_generic(client, db_session, monkeypatch):
    from app.models.user import User

    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "true")

    u = User(username="pw_user", email="pw_user@example.com", is_active=True)
    u.set_password("correct")
    db_session.add(u)
    db_session.commit()

    res = client.post(
        "/auth/login",
        data={"username": "pw_user", "password": "wrong"},
        follow_redirects=True,
    )

    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert "Login Failed." in body
    assert "Invalid username or password" not in body


def test_login_post_does_not_fall_open_when_password_login_disabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "false")

    with patch("app.blueprints.auth.routes.login_user") as login_user_mock:
        res = client.post(
            "/auth/login",
            data={"username": "someone", "password": "irrelevant"},
            follow_redirects=True,
        )

    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert "Password sign-in is disabled" in body
    assert login_user_mock.call_count == 0


def test_password_login_uses_non_persistent_session_by_default(client, db_session, monkeypatch):
    from app.models.user import User

    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "true")
    monkeypatch.setenv("AUTH_REMEMBER_ENABLED", "false")
    client.application.config["AUTH_REMEMBER_ENABLED"] = False

    u = User(username="pw_default", email="pw_default@example.com", is_active=True)
    u.set_password("correct")
    db_session.add(u)
    db_session.commit()

    with patch("app.blueprints.auth.routes.login_user") as login_user_mock:
        res = client.post(
            "/auth/login",
            data={"username": "pw_default", "password": "correct"},
            follow_redirects=False,
        )

    assert res.status_code == 302
    assert login_user_mock.call_count == 1
    _, kwargs = login_user_mock.call_args
    assert kwargs.get("remember") is False


def test_password_login_can_enable_persistent_session_with_config(client, db_session, monkeypatch):
    from app.models.user import User

    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "true")
    monkeypatch.setenv("AUTH_REMEMBER_ENABLED", "true")
    client.application.config["AUTH_REMEMBER_ENABLED"] = True

    u = User(username="pw_remember", email="pw_remember@example.com", is_active=True)
    u.set_password("correct")
    db_session.add(u)
    db_session.commit()

    with patch("app.blueprints.auth.routes.login_user") as login_user_mock:
        res = client.post(
            "/auth/login",
            data={"username": "pw_remember", "password": "correct"},
            follow_redirects=False,
        )

    assert res.status_code == 302
    assert login_user_mock.call_count == 1
    _, kwargs = login_user_mock.call_args
    assert kwargs.get("remember") is True


def test_test_login_page_is_available_when_test_accounts_enabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "true")

    res = client.get("/auth/test-login")

    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert "Test account sign-in" in body
    assert "test_*" in body


def test_test_login_page_redirects_when_test_accounts_disabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "false")

    res = client.get("/auth/test-login", follow_redirects=False)

    assert res.status_code == 302
    assert "/auth/login" in (res.headers.get("Location") or "")


def test_test_login_allows_only_test_prefix_users(client, db_session, monkeypatch):
    from app.models.user import User

    monkeypatch.setenv("ALLOW_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("ENABLE_TEST_ACCOUNTS", "true")

    ok_user = User(username="test_demo_login", email="test_demo_login@example.com", is_active=True)
    ok_user.set_password("correct")
    db_session.add(ok_user)

    blocked_user = User(
        username="normal_demo_login", email="normal_demo_login@example.com", is_active=True
    )
    blocked_user.set_password("correct")
    db_session.add(blocked_user)
    db_session.commit()

    with patch("app.blueprints.auth.routes.login_user") as login_user_mock:
        success_res = client.post(
            "/auth/test-login",
            data={"username": "test_demo_login", "password": "correct"},
            follow_redirects=False,
        )

    assert success_res.status_code == 302
    assert login_user_mock.call_count == 1

    with patch("app.blueprints.auth.routes.login_user") as login_user_mock:
        blocked_res = client.post(
            "/auth/test-login",
            data={"username": "normal_demo_login", "password": "correct"},
            follow_redirects=False,
        )

    assert blocked_res.status_code == 302
    assert "/auth/test-login" in (blocked_res.headers.get("Location") or "")
    assert login_user_mock.call_count == 0
