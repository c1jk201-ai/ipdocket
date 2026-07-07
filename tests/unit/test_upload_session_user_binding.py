from __future__ import annotations

import uuid
from datetime import datetime, timedelta


def _ensure_upload_session_table(db_session):
    # upload_session is created by schema initialization; tests use create_all() so we create it here.
    from app.utils.policy_sql import policy_text as text

    db_session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS upload_session (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                purpose TEXT,
                payload_json TEXT,
                expires_at TIMESTAMP,
                created_at TIMESTAMP
            )
            """
        )
    )
    db_session.commit()


def test_upload_session_is_bound_to_authenticated_user(app, db_session, sample_user, limited_user):
    from flask import g, session

    from app.services.uploads.upload_session_service import get_upload_session_service

    _ensure_upload_session_table(db_session)
    svc = get_upload_session_service()

    # Create a session as sample_user.
    with app.test_request_context("/"):
        session["_user_id"] = str(sample_user.id)
        session["_fresh"] = True
        # Flask-Login caches current_user on `g` (app-context scoped). In tests we keep a long-lived
        # app context, so clear it to ensure each request context reloads from session.
        if "_login_user" in g:
            del g._login_user
        sid = svc.create(purpose="unit_test", staged_files=[], form_data={"k": "v"})

    # Another authenticated user must not be able to retrieve or delete it.
    with app.test_request_context("/"):
        session["_user_id"] = str(limited_user.id)
        session["_fresh"] = True
        if "_login_user" in g:
            del g._login_user
        assert svc.retrieve(sid) is None
        svc.delete(sid)

    # Owner can retrieve; delete must work only for the owner.
    with app.test_request_context("/"):
        session["_user_id"] = str(sample_user.id)
        session["_fresh"] = True
        if "_login_user" in g:
            del g._login_user
        assert svc.retrieve(sid) is not None
        svc.delete(sid)
        assert svc.retrieve(sid) is None


def test_upload_session_retrieve_denies_unauthenticated_request_context(
    app, db_session, sample_user
):
    from flask import g, session

    from app.services.uploads.upload_session_service import get_upload_session_service

    _ensure_upload_session_table(db_session)
    svc = get_upload_session_service()

    # Create session as authenticated user.
    with app.test_request_context("/"):
        session["_user_id"] = str(sample_user.id)
        session["_fresh"] = True
        if "_login_user" in g:
            del g._login_user
        sid = svc.create(purpose="unit_test", staged_files=[], form_data={"k": "v"})

    # In a request context but without auth, retrieve should be denied.
    with app.test_request_context("/"):
        session.pop("_user_id", None)
        session.pop("_fresh", None)
        if "_login_user" in g:
            del g._login_user
        assert svc.retrieve(sid) is None


def test_upload_session_retrieve_allows_non_request_context(app, db_session, sample_user):
    """
    Outside a request context (e.g. ops/CLI), session_id access is allowed.
    This is useful for admin tooling and housekeeping scripts.
    """
    from flask import g, session

    _ensure_upload_session_table(db_session)

    from app.services.uploads.upload_session_service import get_upload_session_service

    svc = get_upload_session_service()

    with app.test_request_context("/"):
        session["_user_id"] = str(sample_user.id)
        session["_fresh"] = True
        if "_login_user" in g:
            del g._login_user
        sid = svc.create(purpose="unit_test", staged_files=[], form_data={"k": "v"})

    assert svc.retrieve(sid) is not None
