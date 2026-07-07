import logging
import os
import warnings
from urllib.parse import urlparse

from flask import Flask

logger = logging.getLogger(__name__)

_PRODUCTION_REQUIRED_ENV_KEYS = (
    "SECRET_KEY",
    "RATELIMIT_STORAGE_URI",
    "BASE_URL",
    "SCHEDULER_ENABLED",
)
_WEAK_SECRET_VALUES = {
    "dev-secret-key",
    "your-super-secret-key-change-this-in-production",
    "change_me_to_a_long_random_string",
}

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


def _apply_production_safety_defaults(app: Flask) -> None:
    if not _is_prod_env(app):
        return
    if "STARTUP_CHECKS_ENFORCE" not in os.environ and not bool(
        app.config.get("STARTUP_CHECKS_ENFORCE")
    ):
        app.config["STARTUP_CHECKS_ENFORCE"] = True
        try:
            app.logger.info(
                "STARTUP_CHECKS_ENFORCE defaulted to 1 in production (override with env if needed)."
            )
        except Exception:
            logger.info(
                "STARTUP_CHECKS_ENFORCE defaulted to 1 in production (override with env if needed)."
            )
    if "RATELIMIT_REQUIRE_SHARED_STORAGE" not in os.environ:
        app.config["RATELIMIT_REQUIRE_SHARED_STORAGE"] = True
        try:
            app.logger.info("RATELIMIT_REQUIRE_SHARED_STORAGE defaulted to 1 in production.")
        except Exception:
            logger.info("RATELIMIT_REQUIRE_SHARED_STORAGE defaulted to 1 in production.")
    if "POLICY_RAW_SQL_GUARD_MODE" not in os.environ:
        app.config["POLICY_RAW_SQL_GUARD_MODE"] = "enforce"
        try:
            app.logger.info("POLICY_RAW_SQL_GUARD_MODE defaulted to enforce in production.")
        except Exception:
            logger.info("POLICY_RAW_SQL_GUARD_MODE defaulted to enforce in production.")


def _configure_image_safety(app: Flask) -> None:
    try:
        from PIL import Image
    except Exception:
        return

    try:
        max_pixels = int(app.config.get("IMAGE_MAX_PIXELS") or 0)
    except Exception:
        max_pixels = 0
    if max_pixels > 0:
        try:
            Image.MAX_IMAGE_PIXELS = max_pixels
        except Exception:
            app.logger.debug(
                "Pillow MAX_IMAGE_PIXELS override failed (IMAGE_MAX_PIXELS=%s)",
                max_pixels,
                exc_info=True,
            )

    if bool(app.config.get("IMAGE_STRICT_DECOMPRESSION_BOMB", True)):
        try:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
        except Exception:
            app.logger.debug(
                "Failed to set Pillow decompression-bomb warning filter", exc_info=True
            )


def _env_present(key: str) -> bool:
    return key in os.environ and str(os.environ.get(key) or "").strip() != ""


def _raise_if_placeholder_secret(key: str, value: str, placeholders: set[str]) -> None:
    normalized = (value or "").strip().lower()
    if normalized in placeholders:
        raise RuntimeError(f"SECURITY: {key} must not use the development/example value.")


def _guard_production_required_security_config(app: Flask) -> None:
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if not _is_prod_env(app):
        return

    missing = [key for key in _PRODUCTION_REQUIRED_ENV_KEYS if not _env_present(key)]
    if missing:
        raise RuntimeError(
            "SECURITY: Missing required production environment variables: "
            + ", ".join(sorted(missing))
        )

    secret_key = str(app.config.get("SECRET_KEY") or "").strip()
    if not secret_key:
        raise RuntimeError("SECURITY: SECRET_KEY must be set in production.")
    _raise_if_placeholder_secret("SECRET_KEY", secret_key, _WEAK_SECRET_VALUES)

    base_url = str(app.config.get("BASE_URL") or "").strip()
    if not base_url:
        raise RuntimeError("SECURITY: BASE_URL must be set in production.")
    parsed_base = urlparse(base_url)
    host = (parsed_base.hostname or "").strip().lower()
    if parsed_base.scheme not in {"http", "https"} or not host:
        raise RuntimeError("SECURITY: BASE_URL must be an absolute http(s) URL in production.")
    if host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise RuntimeError("SECURITY: BASE_URL must not use a localhost default in production.")

    ratelimit_uri = str(app.config.get("RATELIMIT_STORAGE_URI") or "").strip()
    if not ratelimit_uri:
        raise RuntimeError("SECURITY: RATELIMIT_STORAGE_URI must be set in production.")
    ratelimit_scheme = (urlparse(ratelimit_uri).scheme or "").strip().lower()
    if not ratelimit_scheme or ratelimit_scheme == "memory":
        raise RuntimeError("SECURITY: RATELIMIT_STORAGE_URI must use shared storage in production.")
    if not bool(app.config.get("RATELIMIT_REQUIRE_SHARED_STORAGE")):
        raise RuntimeError(
            "SECURITY: RATELIMIT_REQUIRE_SHARED_STORAGE=1 is required in production."
        )

    raw_sql_guard_mode = str(app.config.get("POLICY_RAW_SQL_GUARD_MODE") or "").strip().lower()
    if raw_sql_guard_mode != "enforce":
        raise RuntimeError("SECURITY: POLICY_RAW_SQL_GUARD_MODE=enforce is required in production.")


def configure_security(app: Flask) -> None:
    from app.core.setup.logging_setup import _log_swallowed

    _guard_production_required_security_config(app)
    _configure_image_safety(app)

    env = _runtime_env_name()
    cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
    is_prod = (env in {"prod", "production"}) or (cfg_name in {"prod", "production"})

    if not app.debug and not (app.config.get("SECRET_KEY") or "").strip():
        msg = "SECURITY: SECRET_KEY must be set in non-debug environments."
        if is_prod:
            raise RuntimeError(msg)
        try:
            app.logger.warning(msg)
        except Exception as exc:
            _log_swallowed("configure_security.secret_key_missing_warning", exc)

    if not app.debug and app.config.get("SECRET_KEY") == "dev-secret-key":
        msg = (
            "SECURITY: Refusing to run with default SECRET_KEY 'dev-secret-key'. "
            "Set the SECRET_KEY environment variable."
        )
        if is_prod:
            raise RuntimeError(msg)
        try:
            app.logger.warning(msg)
        except Exception as exc:
            _log_swallowed("configure_security.secret_key_warning", exc)
