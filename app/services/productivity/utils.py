from __future__ import annotations

from datetime import date
from typing import Any, Optional

from flask_login import current_user

from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter
from app.utils.timezone import today_local


def get_today() -> date:
    return today_local()


def get_user_id() -> str:
    """
     id      stringto .
    """
    try:
        if current_user is not None and getattr(current_user, "is_authenticated", False):
            return str(getattr(current_user, "id"))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="productivity_utils.get_user_id",
            log_key="productivity_utils.get_user_id",
            log_window_seconds=300,
        )
    return "anonymous"


def check_can_access_matter_id(matter_id: str, action: str = "view") -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        return False
    return can_access_matter(current_user, mid, action=action)


def has_attr_safe(model, name: str) -> bool:
    try:
        return hasattr(model, name)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="productivity_utils.has_attr_safe",
            log_key=f"productivity_utils.has_attr_safe.{name}",
            log_window_seconds=300,
        )
        return False


def set_if_attr(obj, name: str, value: Any) -> None:
    try:
        if hasattr(obj, name):
            setattr(obj, name, value)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="productivity_utils.set_if_attr",
            log_key=f"productivity_utils.set_if_attr.{name}",
            log_window_seconds=300,
        )


def get_docket_pk(d: Any) -> str:
    return str(getattr(d, "docket_id", None) or getattr(d, "id", None) or "").strip()


def get_docket_title(d: Any) -> str:
    return (
        (getattr(d, "name_free", None) or "").strip()
        or (getattr(d, "name_ref", None) or "").strip()
        or (getattr(d, "name", None) or "").strip()
        or (getattr(d, "title", None) or "").strip()
        or "Deadline"
    )
