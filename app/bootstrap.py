from __future__ import annotations

import os

from flask import Flask

from app.services.billing.subsystem import initialize_billing_subsystem


def _is_testing(app: Flask) -> bool:
    return bool(app.config.get("TESTING")) or (os.environ.get("TESTING") == "1")


def bootstrap_background_services(app: Flask) -> None:
    if _is_testing(app) and not bool(app.config.get("BACKGROUND_RUN_ASYNC_IN_TESTS", False)):
        app.logger.info("Background service bootstrap skipped in testing.")
        return

    from app.services.ops.background import BackgroundService

    BackgroundService.init_app(app)


def bootstrap_deferred_sync(app: Flask) -> None:
    try:
        with app.app_context():
            from app.services.matter.matter_status_recalc_listeners import (
                init_matter_status_recalc_listeners,
            )
            from app.services.workflow.deferred_task_sync import init_deferred_docket_sync

            init_deferred_docket_sync()
            init_matter_status_recalc_listeners()
        app.config["DEFERRED_DOCKET_SYNC_ENABLED"] = True
    except Exception:
        app.logger.exception("Deferred docket sync initialization failed; feature disabled.")
        app.config["DEFERRED_DOCKET_SYNC_ENABLED"] = False


def bootstrap_db_schema(app: Flask) -> None:
    # Schema setup (development only): ensure tables and columns exist.
    # Production should use an initialized database or an explicit schema job.
    if not app.config.get("DB_SCHEMA_AUTO_CREATE", False):
        app.logger.info("DB schema auto-create disabled (DB_SCHEMA_AUTO_CREATE=0)")
        return
    env = (
        (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or os.environ.get("APP_ENV") or "")
        .strip()
        .lower()
    )
    cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
    if env in {"prod", "production"} or cfg_name in {"prod", "production"}:
        msg = "DB schema auto-create is disabled in production environments."
        app.logger.error(msg)
        if app.config.get("DB_SCHEMA_FAIL_FAST", False):
            raise RuntimeError(msg)
        return
    if not app.debug:
        msg = "DB schema auto-create is disabled outside debug mode."
        app.logger.error(msg)
        if app.config.get("DB_SCHEMA_FAIL_FAST", False):
            raise RuntimeError(msg)
        return

    with app.app_context():
        try:
            from app.utils.db_startup import create_tables

            create_tables(app)
            from legacy_billing_schema.db_migrations import init_db, migrate_db

            init_db()
            migrate_db()
        except Exception as e:
            # Log error but don't crash app if DB isn't ready
            app.logger.error("DB Startup failed: %s", e)
            if app.config.get("DB_SCHEMA_FAIL_FAST"):
                raise


def bootstrap_invoice_integration(app: Flask) -> None:
    initialize_billing_subsystem(app)


def bootstrap_local_admin(app: Flask) -> None:
    try:
        from app.services.local_auth import bootstrap_local_admin_from_env

        bootstrap_local_admin_from_env(app)
    except Exception:
        app.logger.exception("Local admin bootstrap failed.")


def bootstrap_scheduler(app: Flask) -> None:
    scheduler_enabled = bool(app.config.get("SCHEDULER_ENABLED", True))
    if not scheduler_enabled:
        app.logger.info("Scheduler disabled (SCHEDULER_ENABLED=0)")
        return

    if not app.debug:
        role = (os.environ.get("SCHEDULER_PROCESS_ROLE") or "").strip().lower()
        if role != "worker":
            app.logger.info(
                "Scheduler bootstrap skipped; set SCHEDULER_PROCESS_ROLE=worker to enable in this process."
            )
            return

    # Process-level enable flag. Prefer RUN_SCHEDULER (documented), but keep
    # legacy env fallback for compatibility.
    run_scheduler = bool(app.config.get("RUN_SCHEDULER")) or (
        os.environ.get("SCHEDULER_RUN_ANYWAY") == "1"
    )
    if not run_scheduler:
        app.logger.info(
            "Scheduler bootstrap skipped; set RUN_SCHEDULER=1 (or SCHEDULER_RUN_ANYWAY=1) to enable in this process."
        )
        return

    try:
        from app.services.ops.scheduler import init_app as _init_scheduler

        with app.app_context():
            _init_scheduler(app)
    except ImportError as e:
        app.logger.warning("APScheduler not available, daily sync disabled: %s", e)
    except Exception as e:
        app.logger.error("Failed to initialize scheduler: %s", e)


def should_run_bootstrap(app: Flask) -> bool:
    if not app.config.get("BOOTSTRAP_ENABLED", True):
        app.logger.info("Bootstrap disabled (BOOTSTRAP_ENABLED=0)")
        return False
    if os.environ.get("BOOTSTRAP_RUN_ANYWAY") == "1":
        return True
    # Avoid double-bootstrapping in the Flask dev-server *reloader parent* process.
    if (
        app.debug
        and os.environ.get("FLASK_RUN_FROM_CLI") == "true"
        and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    ):
        app.logger.info("Bootstrap skipped in reloader process.")
        return False
    return True
