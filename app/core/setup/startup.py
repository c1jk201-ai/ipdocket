from __future__ import annotations

import os

from flask import Flask


def runtime_env_name() -> str:
    return (
        (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or os.environ.get("APP_ENV") or "")
        .strip()
        .lower()
    )


def guard_config_environment(config_name: str) -> None:
    env = runtime_env_name()
    if env in {"prod", "production"} and config_name in {"default", "development"}:
        raise RuntimeError("Refusing to start with a development config in production environment.")


def guard_scheduler_env(app: Flask) -> None:
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    env = runtime_env_name()
    cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
    is_prod = (env in {"prod", "production"}) or (cfg_name in {"prod", "production"})
    if not is_prod:
        return
    if "SCHEDULER_ENABLED" not in os.environ:
        raise RuntimeError("SCHEDULER_ENABLED must be explicitly set in production (1 or 0).")


def warn_housekeeping_without_scheduler(app: Flask, *, enable_scheduler: bool) -> None:
    if not app.config.get("HOUSEKEEPING_ENABLED", True):
        return
    if not app.config.get("HOUSEKEEPING_WARN_WITHOUT_SCHEDULER", True):
        return
    if enable_scheduler:
        return
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    env = runtime_env_name()
    cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
    is_prod = (env in {"prod", "production"}) or (cfg_name in {"prod", "production"})
    if not is_prod:
        return
    app.logger.warning(
        "HOUSEKEEPING_ENABLED=1 but scheduler is disabled for this process. "
        "Ensure a scheduler worker is running (run_scheduler.py) or set HOUSEKEEPING_RUN_ON_STARTUP=1."
    )


def maybe_run_startup_housekeeping(app: Flask) -> None:
    if not app.config.get("HOUSEKEEPING_ENABLED", True):
        return
    if not app.config.get("HOUSEKEEPING_RUN_ON_STARTUP", False):
        return
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    try:
        with app.app_context():
            from app.services.ops.housekeeping import run_housekeeping

            result = run_housekeeping()
        app.logger.info("Startup housekeeping completed: %s", result)
    except Exception:
        app.logger.exception("Startup housekeeping failed.")


def warn_missing_legacy_agency_assets(app: Flask) -> None:
    return
