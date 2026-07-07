import importlib
import logging
import os
import pkgutil

from flask import Flask

from app.core.setup.logging_setup import _log_swallowed
from app.core.setup.security_guards import _is_prod_env
from app.extensions import db
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)


def check_migrations_status(app: Flask) -> dict:
    enabled = bool(app.config.get("READY_CHECK_MIGRATIONS", False))
    if not enabled:
        return {"enabled": False}
    return {"enabled": False, "reason": "schema_migrations_removed"}


def _normalize_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        item = str(raw or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def check_required_db_objects_status(app: Flask) -> dict:
    enabled = bool(app.config.get("READY_CHECK_DB_OBJECTS", True))
    if not enabled:
        return {"enabled": False}

    required_views = _normalize_str_list(
        app.config.get("READY_REQUIRED_DB_VIEWS", ["v_matter_overview"])
    )
    if not required_views:
        return {"enabled": True, "ok": True, "required_views": []}

    required_columns_map: dict[str, set[str]] = {
        "v_matter_overview": {"matter_id", "our_ref", "created_at"},
    }

    missing_views: list[str] = []
    missing_columns: dict[str, list[str]] = {}

    def _collect() -> dict:
        for view_name in required_views:
            exists = (
                db.session.execute(
                    text(
                        """
                        SELECT 1
                        FROM information_schema.views
                        WHERE table_schema = 'public' AND table_name = :view_name
                        LIMIT 1
                        """
                    ),
                    {"view_name": view_name},
                ).first()
                is not None
            )
            if not exists:
                missing_views.append(view_name)
                continue

            required_cols = required_columns_map.get(view_name, set())
            if not required_cols:
                continue

            db_cols = {
                str(c or "").strip().lower()
                for c in db.session.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = :view_name
                        """
                    ),
                    {"view_name": view_name},
                )
                .scalars()
                .all()
            }
            missing = sorted(col for col in required_cols if col.lower() not in db_cols)
            if missing:
                missing_columns[view_name] = missing
        ok = not missing_views and not missing_columns
        return {
            "enabled": True,
            "ok": bool(ok),
            "required_views": required_views,
            "missing_views": sorted(missing_views),
            "missing_columns": missing_columns,
        }

    try:
        from flask import has_app_context

        if not has_app_context():
            with app.app_context():
                return _collect()
        return _collect()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            _log_swallowed("db_objects.rollback", rollback_exc)
        return {"enabled": True, "ok": False, "error": f"db_error:{type(exc).__name__}"}


def _enforce_required_db_objects_on_boot(app: Flask) -> None:
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    raw = (os.environ.get("STARTUP_CHECK_DB_OBJECTS_ON_BOOT") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return
    if not bool(app.config.get("STARTUP_CHECK_DB_OBJECTS_ON_BOOT", True)):
        return

    status = check_required_db_objects_status(app)
    if status.get("enabled") and (status.get("ok") is False):
        details: list[str] = []
        missing_views = status.get("missing_views") or []
        missing_columns = status.get("missing_columns") or {}
        if missing_views:
            details.append(f"missing_views={missing_views}")
        if missing_columns:
            details.append(f"missing_columns={missing_columns}")
        if status.get("error"):
            details.append(f"error={status.get('error')}")
        suffix = "; ".join(details) if details else "unknown_reason"
        msg = (
            "Required DB objects are not ready "
            f"({suffix}). Initialize the database schema before starting the app."
        )
        if bool(app.config.get("STARTUP_CHECKS_ENFORCE")) or _is_prod_env(app):
            raise RuntimeError(msg)
        try:
            app.logger.warning(msg)
        except Exception:
            logger.warning(msg)


def _enforce_model_mappers_on_boot(app: Flask) -> None:
    if app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    raw = (os.environ.get("STARTUP_CHECK_MODEL_MAPPERS_ON_BOOT") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return
    if not bool(app.config.get("STARTUP_CHECK_MODEL_MAPPERS_ON_BOOT", True)):
        return

    try:
        from sqlalchemy.orm import configure_mappers

        import app.models as models_pkg

        for mod_info in pkgutil.iter_modules(models_pkg.__path__):
            if mod_info.ispkg or mod_info.name.startswith("_"):
                continue
            importlib.import_module(f"{models_pkg.__name__}.{mod_info.name}")

        configure_mappers()
    except Exception as exc:
        msg = f"SQLAlchemy mapper configuration failed on boot: {type(exc).__name__}: {exc}"
        if bool(app.config.get("STARTUP_CHECKS_ENFORCE")) or _is_prod_env(app):
            raise RuntimeError(msg) from exc
        try:
            app.logger.warning(msg)
        except Exception:
            logger.warning(msg)


def _enforce_migrations_on_boot(app: Flask) -> None:
    return


def _validate_db_config(app: Flask) -> None:
    db_uri = (app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip()
    testing = bool(app.config.get("TESTING")) or (os.environ.get("TESTING") == "1")
    if testing:
        test_db_uri = (os.environ.get("TEST_DATABASE_URI") or "").strip()
        if test_db_uri:
            app.config["SQLALCHEMY_DATABASE_URI"] = test_db_uri
            app.config["DB_SCHEMA_AUTO_CREATE"] = False
            return
        if not db_uri:
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
            app.config["DB_SCHEMA_AUTO_CREATE"] = False
            return

    if db_uri:
        return

    raise ValueError(
        "DATABASE_URL environment variable is required. "
        "Example: postgresql://user:password@localhost:5432/ipm_db"
    )
