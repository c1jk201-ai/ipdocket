from __future__ import annotations

from typing import Any

from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import PERM_CASE_EDIT_ALL, PERM_CASE_VIEW_ALL, resolve_role_scope


def has_global_case_worklog_view(user: Any) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False

    checker = getattr(user, "has_permission", None)
    if not callable(checker):
        return False

    try:
        return bool(checker(PERM_CASE_VIEW_ALL) or checker(PERM_CASE_EDIT_ALL))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="worklog.visibility.has_global_case_worklog_view",
            log_key="worklog.visibility.has_global_case_worklog_view",
            log_window_seconds=300,
        )
        return False


def worklog_role_scope_flags(user: Any) -> dict[str, bool]:
    role = getattr(user, "role", None) if user else None
    flags = dict(resolve_role_scope(role))
    if has_global_case_worklog_view(user):
        flags.update(
            {
                "show_all_mgmt": True,
                "show_all_work": True,
                "show_own_mgmt": False,
                "show_own_work": False,
            }
        )
    return flags
