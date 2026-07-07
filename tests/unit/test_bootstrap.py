from __future__ import annotations

import builtins

from flask import Flask

from app.bootstrap import (
    bootstrap_background_services,
    bootstrap_db_schema,
    bootstrap_invoice_integration,
)


def test_bootstrap_background_services_skips_test_threadpool(monkeypatch) -> None:
    from app.services.ops.background import BackgroundService

    app = Flask(__name__)
    app.config.update(TESTING=True)

    called = {"count": 0}

    def _fake_init(_app) -> None:
        called["count"] += 1

    monkeypatch.setattr(BackgroundService, "init_app", _fake_init)

    bootstrap_background_services(app)

    assert called["count"] == 0


def test_bootstrap_background_services_allows_test_opt_in(monkeypatch) -> None:
    from app.services.ops.background import BackgroundService

    app = Flask(__name__)
    app.config.update(TESTING=True, BACKGROUND_RUN_ASYNC_IN_TESTS=True)

    called = {"count": 0}

    def _fake_init(_app) -> None:
        called["count"] += 1

    monkeypatch.setattr(BackgroundService, "init_app", _fake_init)

    bootstrap_background_services(app)

    assert called["count"] == 1


def test_bootstrap_invoice_integration_skips_connectivity_check_in_testing(monkeypatch) -> None:
    app = Flask(__name__)
    app.config.update(TESTING=True, INVOICEAPP_INTEGRATED=True)

    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.blueprints.billing_invoices.db":
            raise AssertionError("testing bootstrap should not import invoice db helpers")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)

    bootstrap_invoice_integration(app)

    assert app.config["INVOICEAPP_INIT_OK"] is True


def test_bootstrap_db_schema_runs_billing_schema_for_dev_auto_create(monkeypatch) -> None:
    app = Flask(__name__)
    app.config.update(
        DEBUG=True,
        CONFIG_NAME="development",
        DB_SCHEMA_AUTO_CREATE=True,
        INVOICEAPP_INTEGRATED=True,
    )
    calls: list[str] = []

    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr("app.utils.db_startup.create_tables", lambda _app: calls.append("core"))
    monkeypatch.setattr("legacy_billing_schema.db_migrations.init_db", lambda: calls.append("init"))
    monkeypatch.setattr(
        "legacy_billing_schema.db_migrations.migrate_db",
        lambda: calls.append("migrate"),
    )

    bootstrap_db_schema(app)

    assert calls == ["core", "init", "migrate"]


def test_bootstrap_db_schema_runs_billing_schema_when_integration_disabled(monkeypatch) -> None:
    app = Flask(__name__)
    app.config.update(
        DEBUG=True,
        CONFIG_NAME="development",
        DB_SCHEMA_AUTO_CREATE=True,
        INVOICEAPP_INTEGRATED=False,
    )
    calls: list[str] = []

    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setattr("app.utils.db_startup.create_tables", lambda _app: calls.append("core"))
    monkeypatch.setattr("legacy_billing_schema.db_migrations.init_db", lambda: calls.append("init"))
    monkeypatch.setattr(
        "legacy_billing_schema.db_migrations.migrate_db",
        lambda: calls.append("migrate"),
    )

    bootstrap_db_schema(app)

    assert calls == ["core", "init", "migrate"]
