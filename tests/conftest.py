"""
Pytest configuration and fixtures for new_IPM tests.

This module provides:
- Test application factory
- Database fixtures (SQLite by default; supports PostgreSQL)
- Authentication fixtures
- Common test utilities
"""

import os
from urllib.parse import urlparse

import pytest
from sqlalchemy.pool import StaticPool

os.environ.setdefault("TESTING", "1")
if os.environ.get("TESTING") == "1":
    # The repository's .env may set FLASK_ENV=production on the host.
    # Force a non-production env so create_app("default") is allowed in tests.
    os.environ["FLASK_ENV"] = "testing"
    os.environ["ENV"] = "testing"
    os.environ["APP_ENV"] = "testing"
    os.environ["LOCAL_ADMIN_BOOTSTRAP_ENABLED"] = "0"
    # The repository's .env may point rate limiting to Redis; unit tests should not require redis-py.
    os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
    os.environ["RATELIMIT_REQUIRE_SHARED_STORAGE"] = "0"
    # Keep CIDR/real-IP guards deterministic in tests regardless of host .env.
    os.environ["CIDR_GUARD_ENABLED"] = "0"
    os.environ["SECURITY_TRUST_PROXY_HEADERS"] = "0"
    os.environ["ADMIN_CIDR_ALLOWLIST"] = ""
    os.environ["INTERNAL_API_CIDR_ALLOWLIST"] = ""
    # The repository's .env may enforce raw SQL guard for safety in production.
    # Tests legitimately use raw SQL for setup and assertions, so keep the guard off.
    os.environ["POLICY_RAW_SQL_GUARD_MODE"] = "off"
    # Keep accounting routes enabled unless an individual test toggles them at runtime.
    os.environ["INVOICEAPP_DISABLE_ACCOUNTING_FEATURES"] = "0"
_TEST_DB_URI = (os.environ.get("TEST_DATABASE_URI") or "").strip()
if not _TEST_DB_URI:
    _TEST_DB_URI = "sqlite:///:memory:"
    os.environ["TEST_DATABASE_URI"] = _TEST_DB_URI
if os.environ.get("TESTING") == "1":
    os.environ["DATABASE_URL"] = _TEST_DB_URI

_LEGACY_INVOICE_RESET_TABLES = (
    "journal_lines",
    "journal_entries",
    "invoice_payments",
    "external_invoice_case_map",
    "invoice_case_map",
    "invoice_revisions",
    "invoice_attachments",
    "tax_invoice_drafts",
    "client_deposit_ledger",
    "bank_transactions",
    "bank_import_jobs",
    "expenses",
    "line_items",
    "invoices",
    "client_attachments",
    "client_merge_log",
    "invoice_integrations",
    "fx_rates_cache",
    "accounting_periods",
    "tax_invoice_profiles",
    "invoice_number_counters",
    "invoice_templates",
    "template_items",
    "audit_log",
    "clients",
    "business_profile",
)


# --- Application Fixtures (lazy import to avoid startup issues) ---


def _is_sqlite_memory(uri: str) -> bool:
    return uri.strip().lower().startswith("sqlite:///:memory:")


def _is_postgres(uri: str) -> bool:
    return uri.strip().lower().startswith("postgresql")


def _is_safe_destructive_test_db(uri: str) -> bool:
    """
    Guardrail: tests may create/drop tables. Refuse obviously-dangerous DB URIs by default.

    Allowed by default:
    - sqlite:// (memory or file)
    - any DB where database name contains 'test'/'ci'/'pytest'
    - explicit override via TEST_DATABASE_DESTRUCTIVE_OK=1
    """
    if os.environ.get("TEST_DATABASE_DESTRUCTIVE_OK") == "1":
        return True

    normalized = uri.strip().lower()
    if normalized.startswith("sqlite://"):
        return True

    try:
        parsed = urlparse(uri)
    except Exception:
        return False

    db_name = (parsed.path or "").lstrip("/").lower()
    return any(token in db_name for token in ("test", "ci", "pytest"))


def _reset_legacy_invoice_tables() -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    init_db()
    conn = get_db()
    try:
        for table in _LEGACY_INVOICE_RESET_TABLES:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                conn.rollback()
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            _ = None


@pytest.fixture
def clean_legacy_invoice_db(app):
    with app.app_context():
        _reset_legacy_invoice_tables()

    yield

    with app.app_context():
        _reset_legacy_invoice_tables()


@pytest.fixture(scope="session")
def app():
    """Create and configure a test application instance."""
    # Lazy import to avoid issues during collection
    db_uri = (os.environ.get("TEST_DATABASE_URI") or "").strip()
    if not db_uri:
        db_uri = "sqlite:///:memory:"
        os.environ["TEST_DATABASE_URI"] = db_uri
    if os.environ.get("TESTING") == "1":
        os.environ["DATABASE_URL"] = db_uri

    from app import create_app
    from app.extensions import db

    test_app = create_app("testing")

    test_app.config.update(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key-do-not-use-in-production",
            "SQLALCHEMY_DATABASE_URI": db_uri,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "WTF_CSRF_ENABLED": False,
            "LOGIN_DISABLED": False,
            "SERVER_NAME": "localhost.localdomain",
        }
    )

    if not _is_safe_destructive_test_db(db_uri):
        raise RuntimeError(
            "Refusing to run tests with potentially destructive TEST_DATABASE_URI. "
            "Use sqlite://..., or point to a database with 'test'/'ci'/'pytest' in its name, "
            "or set TEST_DATABASE_DESTRUCTIVE_OK=1.\n"
            f"TEST_DATABASE_URI={db_uri}"
        )

    if _is_sqlite_memory(db_uri):
        engine_opts = dict(test_app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
        connect_args = dict(engine_opts.get("connect_args") or {})
        connect_args.setdefault("check_same_thread", False)
        engine_opts["connect_args"] = connect_args
        engine_opts["poolclass"] = StaticPool
        test_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts

    # Create tables up-front (avoid holding a global app context across the entire session).
    with test_app.app_context():
        db.create_all()

    yield test_app

    with test_app.app_context():
        if not _is_postgres(db_uri):
            # Disable FK checks for SQLite to allow dropping tables with circular deps
            if "sqlite" in db_uri:
                from app.utils.policy_sql import policy_text as text

                with db.engine.connect() as conn:
                    conn.execute(text("PRAGMA foreign_keys=OFF"))
                    conn.commit()
            db.drop_all()
        db.session.remove()


@pytest.fixture(scope="function")
def client(app):
    """Create a test client for making requests."""
    return app.test_client()


@pytest.fixture(scope="function")
def runner(app):
    """Create a test CLI runner."""
    return app.test_cli_runner()


@pytest.fixture(scope="function")
def db_session(app):
    """Create a fresh database session for each test."""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    with app.app_context():
        # Always start from a clean session/connection state (important for nested transactions on SQLite).
        try:
            db.session.rollback()
        except Exception:
            _ = None
        try:
            db.session.remove()
        except Exception:
            _ = None

        uri = (app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip().lower()
        if uri.startswith("sqlite"):
            try:
                db.session.execute(text("PRAGMA foreign_keys=OFF"))
                db.session.commit()
            except Exception:
                db.session.rollback()
            db.drop_all()
        elif uri.startswith("postgresql"):
            try:
                db.session.execute(
                    text(
                        """
                        DO $$
                        DECLARE
                            obj record;
                        BEGIN
                            FOR obj IN
                                SELECT schemaname, viewname
                                FROM pg_views
                                WHERE schemaname = 'public'
                            LOOP
                                EXECUTE format(
                                    'DROP VIEW IF EXISTS %I.%I CASCADE',
                                    obj.schemaname,
                                    obj.viewname
                                );
                            END LOOP;

                            FOR obj IN
                                SELECT schemaname, matviewname
                                FROM pg_matviews
                                WHERE schemaname = 'public'
                            LOOP
                                EXECUTE format(
                                    'DROP MATERIALIZED VIEW IF EXISTS %I.%I CASCADE',
                                    obj.schemaname,
                                    obj.matviewname
                                );
                            END LOOP;

                            FOR obj IN
                                SELECT schemaname, tablename
                                FROM pg_tables
                                WHERE schemaname = 'public'
                            LOOP
                                EXECUTE format(
                                    'DROP TABLE IF EXISTS %I.%I CASCADE',
                                    obj.schemaname,
                                    obj.tablename
                                );
                            END LOOP;
                        END $$;
                        """
                    )
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
        else:
            db.drop_all()
        db.create_all()

        yield db.session
        db.session.rollback()
        db.session.remove()


# --- User Fixtures ---


@pytest.fixture
def sample_user(app, db_session):
    """Create a sample user for testing."""
    from app.models.user import User

    user = User.query.filter_by(email="test@example.com").first()
    if not user:
        user = User(
            username="testuser",
            email="test@example.com",
            role="patent_staff",
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
    db_session.refresh(user)
    # Store ID to avoid DetachedInstanceError when accessed outside session
    user._test_id = user.id
    return user


@pytest.fixture
def limited_user(app, db_session):
    """Create a limited (login-only) user for testing."""
    from app.models.user import User

    user = User.query.filter_by(email="limited@example.com").first()
    if not user:
        user = User(
            username="limiteduser",
            email="limited@example.com",
            role="user",
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
    db_session.refresh(user)
    user._test_id = user.id
    return user


@pytest.fixture
def admin_user(app, db_session):
    """Create an admin user for testing."""
    from app.models.user import User

    user = User.query.filter_by(email="admin@example.com").first()
    if not user:
        user = User(
            username="admin",
            email="admin@example.com",
            role="admin",
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
    db_session.refresh(user)
    # Store ID to avoid DetachedInstanceError when accessed outside session
    user._test_id = user.id
    return user


@pytest.fixture
def authenticated_client(client, sample_user):
    """Create a client logged in as the sample user."""
    # Use stored ID to avoid DetachedInstanceError
    user_id = getattr(sample_user, "_test_id", None) or sample_user.id
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


@pytest.fixture
def limited_client(client, limited_user):
    """Create a client logged in as a limited (role=user) user."""
    user_id = getattr(limited_user, "_test_id", None) or limited_user.id
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


@pytest.fixture
def admin_client(client, admin_user):
    """Create a client logged in as an admin user."""
    # Use stored ID to avoid DetachedInstanceError
    user_id = getattr(admin_user, "_test_id", None) or admin_user.id
    with client.session_transaction() as session:
        session["_user_id"] = user_id
        session["_fresh"] = True
    return client


# --- Matter/Case Fixtures ---


@pytest.fixture
def sample_matter(app, db_session, sample_user):
    """Create a sample matter for testing."""
    import uuid

    from app.models.ip_records import Matter, MatterStaffAssignment

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"TEST-{uuid.uuid4().hex[:8]}",
        right_name="Text Text",
        status_red="Text",
        status_blue="Text",
    )

    user = db_session.merge(sample_user)
    staff_pid = (getattr(user, "staff_party_id", None) or "").strip()
    if not staff_pid:
        staff_pid = f"TEST-STAFF-{uuid.uuid4().hex[:8]}"
        user.staff_party_id = staff_pid
        db_session.add(user)

    db_session.add(matter)
    db_session.flush()
    db_session.add(
        MatterStaffAssignment(
            matter_id=str(matter.matter_id),
            staff_party_id=staff_pid,
            staff_role_code="attorney",
        )
    )
    db_session.commit()
    # Store ID to avoid DetachedInstanceError when accessed after request teardowns.
    matter._test_matter_id = str(matter.matter_id)
    return matter


# --- Utility Functions ---


def assert_json_response(response, status_code=200):
    """Assert that response is JSON with expected status code."""
    assert response.status_code == status_code
    assert response.content_type == "application/json"
    return response.get_json()


def assert_html_response(response, status_code=200):
    """Assert that response is HTML with expected status code."""
    assert response.status_code == status_code
    assert "text/html" in response.content_type
    return response.data.decode("utf-8")
