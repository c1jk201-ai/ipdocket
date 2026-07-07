import json
import logging
import os

from flask import Flask

logger = logging.getLogger(__name__)


def configure_logging(app: Flask) -> None:
    log_level = logging.DEBUG if app.debug else logging.INFO
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_handler = logging.StreamHandler()
        root_handler.setLevel(log_level)
        root_handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
        )
        root_logger.addHandler(root_handler)
    else:
        for handler in root_logger.handlers:
            handler.setLevel(log_level)
    root_logger.setLevel(log_level)

    app.logger.setLevel(log_level)
    app.logger.propagate = False
    if not app.logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(log_level)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
        )
        app.logger.addHandler(handler)
    else:
        for handler in app.logger.handlers:
            handler.setLevel(log_level)


def _log_swallowed(context: str, exc: Exception) -> None:
    has_ctx = False
    try:
        from flask import has_app_context

        has_ctx = bool(has_app_context())
    except Exception:
        has_ctx = False

    if has_ctx:
        try:
            from app.utils.error_logging import report_swallowed_exception

            report_swallowed_exception(
                exc,
                context=context,
                log_key=context,
                log_window_seconds=300,
            )
            return
        except Exception:
            has_ctx = False

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Swallowed exception in %s: %s", context, exc, exc_info=True)


_SNAPSHOT_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "DATABASE_URL",
        "SQLALCHEMY_DATABASE_URI",
        "RATELIMIT_STORAGE_URI",
    }
)
_SNAPSHOT_SENSITIVE_MARKERS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "WEBHOOK",
    "API_KEY",
    "CLIENT_SECRET",
)

_PRODUCTION_CONFIG_SNAPSHOT_KEYS = (
    "CONFIG_NAME",
    "DEBUG",
    "TESTING",
    "TIMEZONE",
    "PREFERRED_URL_SCHEME",
    "SESSION_COOKIE_SECURE",
    "REMEMBER_COOKIE_SECURE",
    "RATELIMIT_ENABLED",
    "HOUSEKEEPING_ENABLED",
    "SCHEDULER_ENABLED",
    "RUN_SCHEDULER",
    "STARTUP_CHECKS_ENABLED",
    "READY_CHECK_MIGRATIONS",
    "READY_CHECK_DB_OBJECTS",
    "POLICY_ENGINE_ENABLED",
    "CIDR_GUARD_ENABLED",
    "SECURITY_HEADERS_ENABLED",
    "CSP_MODE",
    "HSTS_ENABLED",
    "STORAGE_TYPE",
    "INVOICEAPP_INTEGRATED",
)


def _runtime_env_name() -> str:
    return (
        (os.environ.get("FLASK_ENV") or os.environ.get("ENV") or os.environ.get("APP_ENV") or "")
        .strip()
        .lower()
    )


def _is_prod_env(app: Flask) -> bool:
    env = _runtime_env_name()
    cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
    return (env in {"prod", "production"}) or (cfg_name in {"prod", "production"})


def _is_sensitive_key(key: str) -> bool:
    upper = str(key or "").upper()
    if upper in _SNAPSHOT_SENSITIVE_EXACT_KEYS:
        return True
    return any(marker in upper for marker in _SNAPSHOT_SENSITIVE_MARKERS)


def _sanitize_config_value(key: str, value):
    if _is_sensitive_key(key):
        return "***"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_config_value(key, v) for v in value]
    if isinstance(value, dict):
        return {str(k): _sanitize_config_value(str(k), v) for k, v in value.items()}
    return str(value)


def log_config_snapshot(app: Flask) -> None:
    if not app.config.get("CONFIG_SNAPSHOT_ENABLED", True):
        return
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    if _is_prod_env(app):
        snapshot = {
            "config": {
                key: _sanitize_config_value(key, app.config.get(key))
                for key in _PRODUCTION_CONFIG_SNAPSHOT_KEYS
                if key in app.config
            },
            "meta": {
                "config_name": str(app.config.get("CONFIG_NAME") or "").strip().lower(),
                "runtime_env": _runtime_env_name(),
                "total_uppercase_keys": sum(1 for key in app.config if str(key).isupper()),
            },
        }
    else:
        snapshot = {}
        for key, value in app.config.items():
            if not str(key).isupper():
                continue
            snapshot[str(key)] = _sanitize_config_value(str(key), value)
    try:
        payload = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        payload = str(snapshot)
    app.logger.info("Config snapshot: %s", payload)
