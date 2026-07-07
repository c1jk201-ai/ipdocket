from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import g

from app.extensions import db
from app.models.case_audit_log import CaseAuditLog
from app.utils.error_logging import report_swallowed_exception


def _get_request_id() -> str | None:
    try:
        return getattr(g, "request_id", None)
    except Exception:
        return None


def _truncate(value: Any, limit: int = 500) -> Any:
    """
    JSON/ old/new   value  DB/   .
    - str: limit   Save
    - dict/list:  ,   to-string truncation ( )
    """
    try:
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            if len(v) > limit:
                return v[:limit] + f"...(+{len(v)-limit})"
            return v
        # dict/list Defaultto  Save( JSON )
        return value
    except Exception:
        return None


def record_case_audit(
    *,
    case_id: str,
    field_name: str,
    action: str = "USER",
    actor_user_id: int | None = None,
    old_value: Any = None,
    new_value: Any = None,
    request_id: str | None = None,
) -> CaseAuditLog | None:
    """
    CaseAuditLog  Change history Log.
    - commit  row(   )
    """
    try:
        row = CaseAuditLog(
            case_id=str(case_id),
            actor_user_id=actor_user_id,
            action=(action or "USER").upper(),
            field_name=(field_name or "event").strip()[:200],
            old_value=_truncate(old_value),
            new_value=_truncate(new_value),
            request_id=request_id or _get_request_id(),
            created_at=datetime.utcnow(),
        )
        db.session.add(row)
        return row
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case_audit_service.record_case_audit",
            log_key="case_audit_service.record_case_audit",
            log_window_seconds=300,
        )
        return None
