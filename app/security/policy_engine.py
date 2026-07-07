from __future__ import annotations

import logging
import threading
from typing import Iterable

from flask import current_app, g, has_request_context, session
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.extensions import db
from app.utils.permissions import can_manage_case_globally, policy_accessible_matter_ids_select
from app.utils.policy_sql import looks_like_restricted_raw_sql as _looks_like_restricted_raw_sql

logger = logging.getLogger(__name__)
_POLICY_METRICS_LOCK = threading.Lock()
_POLICY_GUARD_METRICS = {"policy_bypass_count": 0}


def _increment_policy_bypass_count() -> None:
    try:
        with _POLICY_METRICS_LOCK:
            _POLICY_GUARD_METRICS["policy_bypass_count"] = (
                int(_POLICY_GUARD_METRICS.get("policy_bypass_count") or 0) + 1
            )
    except Exception:
        return


def get_policy_guard_metrics() -> dict[str, int]:
    with _POLICY_METRICS_LOCK:
        return dict(_POLICY_GUARD_METRICS)


def install_raw_sql_guard(app, engine):
    """
    Guard raw SQL against restricted tables:
      - POLICY_RAW_SQL_GUARD_MODE = "report" | "enforce" | "off"
      - production requires "enforce" at startup
    Bypass explicitly:
      - use app.utils.policy_sql.policy_bypass_text(sql=..., reason=..., scope=...)
      - legacy helper app.utils.policy_sql.policy_text(...) still auto-applies a tagged bypass
    """
    mode = (app.config.get("POLICY_RAW_SQL_GUARD_MODE") or "report").strip().lower()
    if mode not in {"report", "enforce", "off"}:
        raise RuntimeError("POLICY_RAW_SQL_GUARD_MODE must be one of: report, enforce, off.")
    if mode == "off":
        return

    @event.listens_for(engine, "before_execute", retval=False)
    def _before_execute(conn, clauseelement, multiparams, params, execution_options):
        try:
            stmt_options = getattr(clauseelement, "_execution_options", None) or {}
            if execution_options.get("policy_bypass") or stmt_options.get("policy_bypass"):
                _increment_policy_bypass_count()
                reason = execution_options.get("policy_bypass_reason") or stmt_options.get(
                    "policy_bypass_reason"
                )
                scope = execution_options.get("policy_bypass_scope") or stmt_options.get(
                    "policy_bypass_scope"
                )
                if app.config.get("POLICY_RAW_SQL_BYPASS_REASON_REQUIRED") and (
                    not reason or not scope
                ):
                    raise RuntimeError("SECURITY: Raw SQL policy bypass requires reason and scope.")
                return
            if _looks_like_restricted_raw_sql(clauseelement):
                msg = "SECURITY: Raw SQL touching restricted tables detected. Use ORM or execution_options(policy_bypass=True) if intended."
                sql_preview = str(getattr(clauseelement, "text", clauseelement))[:500]
                if mode == "enforce":
                    logger.error("%s sql=%r", msg, sql_preview)
                    raise RuntimeError(msg)
                logger.warning("%s sql=%r", msg, sql_preview)
        except Exception:
            raise


_POLICY_ENGINE_REGISTERED = False
_MATTER_SCOPED_MODELS: list[type] | None = None


def _iter_matter_scoped_models() -> Iterable[type]:
    registry = getattr(db.Model, "registry", None)
    class_registry = getattr(registry, "_class_registry", None) if registry is not None else None
    if not class_registry:
        # Fallback for older SQLAlchemy/Flask-SQLAlchemy layouts
        class_registry = getattr(db.Model, "_decl_class_registry", {})  # type: ignore[attr-defined]

    for obj in class_registry.values():
        if not isinstance(obj, type):
            continue
        if not issubclass(obj, db.Model):
            continue
        if not hasattr(obj, "__table__"):
            continue
        if hasattr(obj, "matter_id"):
            yield obj


def _get_matter_scoped_models() -> list[type]:
    global _MATTER_SCOPED_MODELS
    if _MATTER_SCOPED_MODELS is None:
        _MATTER_SCOPED_MODELS = sorted(
            set(_iter_matter_scoped_models()),
            key=lambda cls: (getattr(cls, "__module__", ""), getattr(cls, "__name__", "")),
        )
    return _MATTER_SCOPED_MODELS


def init_policy_engine() -> None:
    """
    Enforce "Role + Responsible + "  Search Filter ORM from .

    - request context + authenticated userfrom Apply
    - admin/global role bypass
    -  matter_id   Auto Apply (include_aliases=True)
    """
    global _POLICY_ENGINE_REGISTERED
    if _POLICY_ENGINE_REGISTERED:
        return
    _POLICY_ENGINE_REGISTERED = True

    @event.listens_for(Session, "do_orm_execute")
    def _apply_policy(execute_state):  # noqa: ANN001
        if not execute_state.is_select:
            return
        if not getattr(execute_state, "is_orm_statement", False):
            return
        if not has_request_context():
            return
        if not current_app.config.get("POLICY_ENGINE_ENABLED", True):
            return
        # Recursion guard: this hook may indirectly trigger ORM SELECTs (e.g. lazy-loading the
        # login user attributes, or building the allowed-matter subquery). Those nested SELECTs
        # must not re-enter the policy hook, or we can end up in infinite recursion.
        if getattr(g, "_policy_engine_applying", False):
            return
        g._policy_engine_applying = True
        try:
            # Avoid touching `flask_login.current_user` here: resolving it may run ORM queries,
            # which would recursively trigger this hook. Use the session `_user_id` and load a
            # fresh user instance under the recursion guard.
            try:
                user_id = session.get("_user_id")
            except Exception:
                user_id = None
            if user_id is None:
                return
            try:
                user_id_int = int(user_id)
            except Exception:
                return

            from app.models.user import User

            user = db.session.get(User, user_id_int)
            if not user:
                return
            if not getattr(user, "is_active", True):
                return
            if can_manage_case_globally(user):
                return

            allowed = getattr(g, "_policy_allowed_matter_ids_select", None)
            if allowed is None:
                allowed = policy_accessible_matter_ids_select(user)
                g._policy_allowed_matter_ids_select = allowed

            opts = getattr(g, "_policy_loader_criteria_opts", None)
            if opts is None:
                crit = lambda cls: cls.matter_id.in_(allowed)  # noqa: E731
                opts = [
                    with_loader_criteria(model, crit, include_aliases=True)
                    for model in _get_matter_scoped_models()
                ]
                g._policy_loader_criteria_opts = opts

            execute_state.statement = execute_state.statement.options(*opts)
        finally:
            g._policy_engine_applying = False
