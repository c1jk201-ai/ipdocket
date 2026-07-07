from __future__ import annotations

import re

from flask import Flask

from app.bootstrap import (
    bootstrap_background_services,
    bootstrap_db_schema,
    bootstrap_deferred_sync,
    bootstrap_invoice_integration,
    bootstrap_local_admin,
    bootstrap_scheduler,
    should_run_bootstrap,
)
from app.core.setup.blueprint_registry import register_blueprints
from app.core.setup.db_guards import (
    _enforce_migrations_on_boot,
    _enforce_model_mappers_on_boot,
    _enforce_required_db_objects_on_boot,
    _validate_db_config,
)
from app.core.setup.extensions import configure_db_engine, init_extensions, validate_upload_limits
from app.core.setup.logging_setup import (
    _sanitize_config_value,
    configure_logging,
    log_config_snapshot,
)
from app.core.setup.readiness import register_health_endpoints
from app.core.setup.request_hooks import register_request_hooks
from app.core.setup.security_guards import _apply_production_safety_defaults, configure_security
from app.core.setup.startup import guard_config_environment as _guard_config_environment
from app.core.setup.startup import guard_scheduler_env as _guard_scheduler_env
from app.core.setup.startup import maybe_run_startup_housekeeping as _maybe_run_startup_housekeeping
from app.core.setup.startup import runtime_env_name as _runtime_env_name
from app.core.setup.startup import (
    warn_housekeeping_without_scheduler as _warn_housekeeping_without_scheduler,
)
from app.core.setup.startup import warn_missing_legacy_agency_assets
from app.core.setup.template_filters import register_template_filters
from app.security import init_security
from app.utils.timezone import apply_process_timezone, normalize_timezone_name
from config import config


class _LegacyQueryMarkerMiddleware:
    _markers = ("New", "Legacy")
    _query_start_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_.%-]*=")

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        path = str(environ.get("PATH_INFO") or "")
        for marker in self._markers:
            if marker not in path:
                continue
            rewritten = self._rewrite(path, str(environ.get("QUERY_STRING") or ""), marker)
            if rewritten:
                new_path, new_query = rewritten
                environ["PATH_INFO"] = new_path
                environ["QUERY_STRING"] = new_query
                break
        return self.app(environ, start_response)

    @classmethod
    def _rewrite(cls, path: str, query_string: str, marker: str) -> tuple[str, str] | None:
        start = 0
        while True:
            idx = path.find(marker, start)
            if idx < 0:
                return None
            suffix = path[idx + len(marker) :]
            if cls._query_start_re.match(suffix):
                prefix = path[:idx]
                new_query = suffix
                if query_string:
                    new_query = f"{new_query}&{query_string}"
                return prefix or "/", new_query
            start = idx + len(marker)


def create_app(
    config_name: str = "default",
    *,
    enable_bootstrap: bool = True,
    enable_scheduler: bool = False,
) -> Flask:
    _guard_config_environment(config_name)
    app = Flask(__name__)
    app.wsgi_app = _LegacyQueryMarkerMiddleware(app.wsgi_app)
    config_class = config[config_name]
    app.config.from_object(config_class)
    app.config["TIMEZONE"] = normalize_timezone_name(app.config.get("TIMEZONE"))
    apply_process_timezone(app.config["TIMEZONE"])
    app.config["CONFIG_NAME"] = config_name
    runtime_env = _runtime_env_name()
    app.config["ENV"] = runtime_env or config_name
    app.config["FLASK_ENV"] = runtime_env or config_name
    init_app_fn = getattr(config_class, "init_app", None)
    if callable(init_app_fn):
        init_app_fn(app)
    _guard_scheduler_env(app)

    # Security bootstrap (CSP/HSTS/CIDR guards).
    # ProxyFix is applied when SECURITY_TRUST_PROXY_HEADERS is enabled.
    init_security(app)

    _validate_db_config(app)
    configure_logging(app)
    _apply_production_safety_defaults(app)
    configure_security(app)
    configure_db_engine(app)
    init_extensions(app)

    try:
        from app.utils.error_logging import install_error_report_logging_hook

        install_error_report_logging_hook(app)
    except Exception:
        app.logger.warning("Failed to install error-report logging hook", exc_info=True)

    try:
        with app.app_context():
            from app.extensions import db
            from app.security.policy_engine import install_raw_sql_guard
            from app.services.deletion_manager import init_deletion_listeners

            install_raw_sql_guard(app, db.engine)
            init_deletion_listeners()
    except Exception:
        app.logger.warning("Failed to install raw SQL guard or deletion listeners", exc_info=True)

    register_request_hooks(app)
    register_template_filters(app)
    register_blueprints(app)
    register_error_handlers(app)
    register_health_endpoints(app)
    validate_upload_limits(app)
    log_config_snapshot(app)
    warn_missing_legacy_agency_assets(app)
    run_startup_checks(app)
    run_bootstrap(app, enable_bootstrap=enable_bootstrap)
    run_scheduler_bootstrap(app, enable_scheduler=enable_scheduler)
    return app


def register_error_handlers(app: Flask) -> None:
    from app.core.setup.error_handlers import register_error_handlers as _register_error_handlers

    _register_error_handlers(app)


def run_startup_checks(app: Flask) -> None:
    try:
        from app.utils.db_startup import run_startup_checks as _run_startup_checks

        _run_startup_checks(app)
    except Exception:
        app.logger.exception("Startup checks failed.")
        if app.config.get("STARTUP_CHECKS_FAIL_FAST"):
            raise
    _enforce_model_mappers_on_boot(app)
    # Fail fast on migration drift to avoid silent feature break.
    _enforce_migrations_on_boot(app)
    # Fail fast if critical views are missing even when Alembic version matches.
    _enforce_required_db_objects_on_boot(app)


def run_bootstrap(app: Flask, *, enable_bootstrap: bool) -> None:
    if enable_bootstrap and should_run_bootstrap(app):
        bootstrap_background_services(app)
        bootstrap_deferred_sync(app)
        bootstrap_db_schema(app)
        bootstrap_local_admin(app)
        bootstrap_invoice_integration(app)


def run_scheduler_bootstrap(app: Flask, *, enable_scheduler: bool) -> None:
    # Scheduler enable is a process-level concern; support both the explicit
    # create_app(enable_scheduler=True) flag and the documented RUN_SCHEDULER env/config.
    run_scheduler = bool(enable_scheduler) or bool(app.config.get("RUN_SCHEDULER"))
    if run_scheduler:
        # Ensure readiness checks see the effective intent to run the scheduler.
        app.config["RUN_SCHEDULER"] = True
        bootstrap_scheduler(app)

    _warn_housekeeping_without_scheduler(app, enable_scheduler=run_scheduler)
    _maybe_run_startup_housekeeping(app)
