from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _report_semantics_error(exc: Exception, *, context: str) -> None:
    try:
        from app.utils.error_logging import report_swallowed_exception

        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=300,
        )
    except Exception as report_exc:
        logger.debug("Failed to report workflow semantics error: %s", report_exc)


def normalize_workflow_category(category: str | None) -> str | None:
    normalized = (category or "").strip().upper()
    if normalized == "WORK":
        return "WORK"
    if normalized in {"MGMT", "MANAGEMENT"}:
        return "MGMT"
    if normalized in {"HYBRID", "MIXED", "MGMT_WORK", "WORK_MGMT"}:
        return "MGMT_WORK"
    return None


def _coerce_positive_user_id(raw_value) -> int | None:
    try:
        value = int(raw_value) if raw_value is not None else None
    except Exception:
        value = None
    if value and value > 0:
        return value
    return None


def infer_workflow_category_from_assignments(
    *,
    handler_id: int | None,
    attorney_id: int | None,
    manager_id: int | None,
) -> str:
    has_mgmt = _coerce_positive_user_id(manager_id) is not None
    has_work = any(
        _coerce_positive_user_id(raw_value) is not None for raw_value in (handler_id, attorney_id)
    )
    if has_mgmt and has_work:
        return "MGMT_WORK"
    if has_mgmt:
        return "MGMT"
    return "WORK"


def derive_workflow_category(
    *,
    case_id: str | None,
    handler_id: int | None,
    attorney_id: int | None,
    manager_id: int | None,
    manual_category: str | None = None,
    hint_category: str | None = None,
    hint_name_ref: str | None = None,
    hint_name_free: str | None = None,
    source: str | None = None,
) -> str:
    normalized_manual = normalize_workflow_category(manual_category)
    if normalized_manual:
        return normalized_manual

    resolved_from_assignments = infer_workflow_category_from_assignments(
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
    )
    has_mgmt = resolved_from_assignments in {"MGMT", "MGMT_WORK"}

    if has_mgmt:
        try:
            from app.utils.task_assignment_rules import is_manager_only_notice

            if is_manager_only_notice(
                name_ref=hint_name_ref,
                name_free=hint_name_free,
                category=hint_category,
                source=source,
            ):
                return "MGMT"
        except Exception as exc:
            _report_semantics_error(
                exc,
                context="workflow_semantics.derive_workflow_category.is_manager_only_notice",
            )

    if resolved_from_assignments == "MGMT_WORK":
        return "MGMT_WORK"

    target_user_id = (
        _coerce_positive_user_id(manager_id)
        if has_mgmt
        else (_coerce_positive_user_id(handler_id) or _coerce_positive_user_id(attorney_id))
    )

    try:
        from app.utils.task_classification import determine_category_by_staff_role

        return determine_category_by_staff_role(
            matter_id=str(case_id) if case_id else None,
            assignee_id=target_user_id,
            category=hint_category,
            name_ref=hint_name_ref,
            name_free=hint_name_free,
        )
    except Exception as exc:
        _report_semantics_error(
            exc,
            context="workflow_semantics.derive_workflow_category.determine_category_by_staff_role",
        )

    if has_mgmt:
        return "MGMT"
    return "WORK"


def workflow_owner_role_codes(
    *,
    category: str | None,
    handler_id: int | None,
    attorney_id: int | None,
    manager_id: int | None,
) -> tuple[str, ...]:
    normalized = normalize_workflow_category(category) or infer_workflow_category_from_assignments(
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
    )
    if normalized == "MGMT":
        candidates = (("manager", manager_id),)
    elif normalized == "MGMT_WORK":
        candidates = (
            ("attorney", attorney_id),
            ("handler", handler_id),
            ("manager", manager_id),
        )
    else:
        candidates = (
            ("attorney", attorney_id),
            ("handler", handler_id),
        )
    return tuple(
        role_code
        for role_code, raw_user_id in candidates
        if _coerce_positive_user_id(raw_user_id) is not None
    )


def workflow_primary_owner_user_id(
    *,
    category: str | None,
    handler_id: int | None,
    attorney_id: int | None,
    manager_id: int | None,
) -> int | None:
    normalized = normalize_workflow_category(category) or infer_workflow_category_from_assignments(
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
    )
    if normalized == "MGMT":
        candidates = (manager_id,)
    elif normalized == "MGMT_WORK":
        candidates = (handler_id, attorney_id, manager_id)
    else:
        candidates = (handler_id, attorney_id)

    for raw in candidates:
        value = _coerce_positive_user_id(raw)
        if value is not None:
            return value
    for raw in (handler_id, attorney_id, manager_id):
        value = _coerce_positive_user_id(raw)
        if value is not None:
            return value
    return None


def workflow_sync_category_types(
    category: str | None,
    *,
    has_hybrid_assignments: bool = False,
) -> tuple[str, ...] | None:
    normalized = normalize_workflow_category(category)
    if normalized == "MGMT_WORK":
        return ("WORK", "MGMT")
    if normalized == "MGMT":
        return ("MGMT",)
    if normalized == "WORK":
        return ("WORK",)
    if has_hybrid_assignments:
        return ("WORK", "MGMT")
    return None


def workflow_badge_values(raw_category: str | None) -> tuple[str, str]:
    normalized = normalize_workflow_category(raw_category)
    if normalized == "MGMT_WORK":
        return ("hybrid", "HYBRID")
    if normalized == "MGMT":
        return ("mgmt", "")
    return ("work", "")
