from __future__ import annotations

import json
from unittest.mock import patch

from flask_login import UserMixin


class _MockUserNoUsername(UserMixin):
    id = 1
    role = "admin"
    is_active = True

    def get_id(self):  # pragma: no cover - flask-login API
        return "1"


class _MockUserEmailOnly(UserMixin):
    id = 2
    email = "user@example.com"
    role = "staff"
    is_active = True

    def get_id(self):  # pragma: no cover - flask-login API
        return "2"


class _MockUserNamed(UserMixin):
    id = 3
    username = "worklog-user"
    email = "worklog-user@example.com"
    display_name = "Text"
    role = "staff"
    is_active = True

    def get_id(self):  # pragma: no cover - flask-login API
        return "3"


def test_get_current_user_returns_none_when_unauthenticated(app):
    from app.blueprints.billing_invoices.auth import get_current_user

    with app.test_request_context("/"):
        assert get_current_user() is None


def test_get_current_user_is_defensive_without_username(app):
    from app.blueprints.billing_invoices.auth import get_current_user

    with app.test_request_context("/"):
        with patch("flask_login.utils._get_user", return_value=_MockUserNoUsername()):
            user = get_current_user()
            assert user is not None
            assert user["id"] == 1
            assert user["username"] == "1"
            assert user["role"] == "admin"


def test_get_current_user_falls_back_to_email_when_username_missing(app):
    from app.blueprints.billing_invoices.auth import get_current_user

    with app.test_request_context("/"):
        with patch("flask_login.utils._get_user", return_value=_MockUserEmailOnly()):
            user = get_current_user()
            assert user is not None
            assert user["id"] == 2
            assert user["username"] == "user@example.com"
            assert user["role"] == "staff"


def test_log_audit_is_defensive_when_current_user_is_none(app, monkeypatch):
    from app.blueprints.billing_invoices import auth as invoice_auth

    class _Conn:
        def __init__(self):
            self.execute_called = False
            self.commit_called = False
            self.close_called = False

        def execute(self, *_args, **_kwargs):
            self.execute_called = True

        def commit(self):
            self.commit_called = True

        def close(self):
            self.close_called = True

    conn = _Conn()
    monkeypatch.setattr(invoice_auth, "get_db", lambda: conn)

    with app.test_request_context("/"):
        with patch("flask_login.utils._get_user", return_value=None):
            invoice_auth.log_audit("invoice.tax_issued", "invoice", 1, "{}")

    assert conn.execute_called is False
    assert conn.commit_called is False
    assert conn.close_called is False


def test_log_audit_embeds_actor_identity_in_meta(app, monkeypatch):
    from app.blueprints.billing_invoices import auth as invoice_auth

    class _Conn:
        def __init__(self):
            self.calls = []
            self.committed = False
            self.closed = False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    conn = _Conn()
    monkeypatch.setattr(invoice_auth, "get_db", lambda: conn)

    with app.test_request_context("/worklog"):
        with patch("flask_login.utils._get_user", return_value=_MockUserNamed()):
            invoice_auth.log_audit(
                "worklog.complete",
                "workflow",
                123,
                json.dumps({"workflow_id": 123}, ensure_ascii=False),
            )

    assert conn.calls
    _sql, params = conn.calls[0]
    assert params[1] == 3  # actor_id
    assert params[2] == 3  # user_id
    payload = json.loads(params[-1])
    assert payload["workflow_id"] == 123
    assert payload["actor_user_id"] == 3
    assert payload["actor_username"] == "worklog-user"
    assert payload["actor_display_name"] == "Text"
    assert conn.committed is True
    assert conn.closed is True


def test_log_audit_overwrites_spoofed_actor_identity(app, monkeypatch):
    from app.blueprints.billing_invoices import auth as invoice_auth

    class _Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def commit(self):
            pass

        def close(self):
            pass

    conn = _Conn()
    monkeypatch.setattr(invoice_auth, "get_db", lambda: conn)

    with app.test_request_context("/worklog"):
        with patch("flask_login.utils._get_user", return_value=_MockUserNamed()):
            invoice_auth.log_audit(
                "worklog.complete",
                "workflow",
                123,
                json.dumps(
                    {
                        "workflow_id": 123,
                        "actor_user_id": 999,
                        "actor_username": "spoofed",
                    },
                    ensure_ascii=False,
                ),
            )

    assert conn.calls
    _sql, params = conn.calls[0]
    payload = json.loads(params[-1])
    assert payload["workflow_id"] == 123
    assert payload["actor_user_id"] == 3
    assert payload["actor_username"] == "worklog-user"


def test_log_audit_closes_db_on_exception(app, monkeypatch):
    """conn.close() must be called even when the DB insert raises."""
    from app.blueprints.billing_invoices import auth as invoice_auth

    class _BrokenConn:
        def __init__(self):
            self.closed = False

        def execute(self, *_args, **_kwargs):
            raise RuntimeError("simulated DB failure")

        def commit(self):
            pass

        def close(self):
            self.closed = True

    conn = _BrokenConn()
    monkeypatch.setattr(invoice_auth, "get_db", lambda: conn)

    with app.test_request_context("/worklog"):
        with patch("flask_login.utils._get_user", return_value=_MockUserNamed()):
            # Should not propagate the exception
            invoice_auth.log_audit("test.action", "workflow", 1, None)

    # Connection must be closed even though execute raised
    assert conn.closed is True


def test_email_masking():
    """_mask_email should mask local and domain parts for PII protection."""
    from app.blueprints.billing_invoices.auth import _mask_email

    assert _mask_email("user@example.com") == "u***@e***.com"
    assert _mask_email("a@b.org") == "a***@b***.org"
    assert _mask_email("noat") == "noat"


def test_actor_audit_meta_masks_email_when_no_username(app):
    """When username is absent, email fallback should be masked."""
    from app.blueprints.billing_invoices.auth import _actor_audit_meta

    meta = _actor_audit_meta(_MockUserEmailOnly(), 2)
    assert meta["actor_username"] == "u***@e***.com"
    assert meta["actor_display_name"] == "u***@e***.com"
