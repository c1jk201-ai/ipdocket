from __future__ import annotations

import re
from collections import deque

from sqlalchemy import or_

from app.extensions import db
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.utils.search import matches_search_expression, parse_search_expression, text_matches_query

_WORKLOG_QUERY_FIELD_ALIASES = {
    "ref": "our_ref",
    "our_ref": "our_ref",
    "Matter reference": "our_ref",
    "task": "task_name",
    "name": "task_name",
    "title": "task_name",
    "Task": "task_name",
    "Task": "task_name",
    "applicant": "applicant_name",
    "Applicant": "applicant_name",
    "owner": "owner_name",
    "assignee": "owner_name",
    "TaskResponsible": "owner_name",
    "TaskContact": "owner_name",
    "attorney": "attorney_names",
    "": "attorney_names",
    "Responsible attorney": "attorney_names",
    "handler": "handler_names",
    "ProcessResponsible": "handler_names",
    "Handler": "handler_names",
    "manager": "manager_names",
    "Responsible": "manager_names",
    "Manager": "manager_names",
    "staff": "staff",
    "Responsible": "staff",
    "Contact": "staff",
    "memo": "note",
    "note": "note",
    "Notes": "note",
    "description": "note",
    "Description": "note",
    "status": "status",
    "Status": "status",
    "category": "category",
    "Type": "category",
}


def _append_search_terms(target: list[str], *values: object) -> None:
    seen = {str(item or "").strip().lower() for item in target if str(item or "").strip()}
    stack = deque(values)
    while stack:
        raw = stack.popleft()
        if raw is None:
            continue
        if isinstance(raw, (list, tuple, set)):
            for item in reversed(list(raw)):
                stack.appendleft(item)
            continue
        for token in re.split(r"[;,]", str(raw or "")):
            item = token.strip()
            if not item:
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            target.append(item)


def _search_text_matches_query(search_text: str, search_query: str) -> bool:
    return text_matches_query(search_text, search_query)


def _parse_worklog_search_expression(search_query: str):
    return parse_search_expression(search_query, field_aliases=_WORKLOG_QUERY_FIELD_ALIASES)


def _append_field_search_values(
    field_values: dict[str, list[str]],
    field_name: str,
    *values,
) -> None:
    if not field_name:
        return
    target = field_values.setdefault(field_name, [])
    _append_search_terms(target, *values)


def _flatten_field_search_values(field_values: dict[str, object]) -> str:
    terms: list[str] = []
    for value in (field_values or {}).values():
        _append_search_terms(terms, value)
    return " ".join(terms)


def _task_search_field_values(task: dict) -> dict[str, list[str]]:
    field_values: dict[str, list[str]] = {}
    _append_field_search_values(field_values, "our_ref", task.get("our_ref"))
    _append_field_search_values(field_values, "task_name", task.get("task_name"))
    _append_field_search_values(field_values, "applicant_name", task.get("applicant_name"))
    _append_field_search_values(
        field_values, "owner_name", task.get("owner_name"), task.get("owner_id")
    )
    _append_field_search_values(field_values, "attorney_names", task.get("attorney_names"))
    _append_field_search_values(field_values, "handler_names", task.get("handler_names"))
    _append_field_search_values(field_values, "manager_names", task.get("manager_names"))
    _append_field_search_values(
        field_values,
        "note",
        task.get("memo"),
        task.get("worklog_description"),
    )
    _append_field_search_values(
        field_values,
        "category",
        task.get("category"),
        task.get("category_type"),
        task.get("category_display"),
    )
    _append_field_search_values(field_values, "status", task.get("status"))
    _append_field_search_values(
        field_values,
        "staff",
        task.get("owner_name"),
        task.get("attorney_names"),
        task.get("handler_names"),
        task.get("manager_names"),
    )

    row_field_map = {
        "owners": "owner_name",
        "attorneys": "attorney_names",
        "handlers": "handler_names",
        "managers": "manager_names",
    }
    for rows_key, field_name in row_field_map.items():
        for row in task.get(rows_key) or []:
            if not isinstance(row, dict):
                continue
            _append_field_search_values(field_values, field_name, row.get("name"), row.get("id"))
            _append_field_search_values(field_values, "staff", row.get("name"), row.get("id"))

    return {key: values for key, values in field_values.items() if values}


def _matches_worklog_search_query(
    *,
    search_text: str,
    search_query: str,
    field_values: dict[str, object] | None = None,
    search_expression=None,
) -> bool:
    expression = (
        search_expression
        if search_expression is not None
        else _parse_worklog_search_expression(search_query)
    )
    return matches_search_expression(search_text, expression, field_values=field_values)


def _task_search_text(task: dict) -> str:
    terms: list[str] = []
    _append_search_terms(
        terms,
        task.get("our_ref"),
        task.get("task_name"),
        task.get("applicant_name"),
        task.get("owner_name"),
        task.get("attorney_names"),
        task.get("handler_names"),
        task.get("manager_names"),
        task.get("memo"),
        task.get("worklog_description"),
    )
    for rows_key in ("owners", "attorneys", "handlers", "managers"):
        for row in task.get(rows_key) or []:
            if not isinstance(row, dict):
                continue
            _append_search_terms(terms, row.get("name"), row.get("id"))
    return " ".join(terms)


def _task_matches_search_query(task: dict, search_query: str) -> bool:
    return _matches_worklog_search_query(
        search_text=_task_search_text(task),
        search_query=search_query,
        field_values=_task_search_field_values(task),
    )


def _load_intake_case_access_recommendation_by_task_id(tasks: list[dict]) -> dict[str, dict]:
    intake_tasks: list[dict] = []
    owner_identifiers: set[str] = set()
    matter_ids: set[str] = set()

    for task in tasks or []:
        if not bool(task.get("_intake_confirmation_task")):
            continue
        task_id = str(task.get("id") or "").strip()
        matter_id = str(task.get("matter_id") or "").strip()
        if not task_id or not matter_id:
            continue

        owner_ids = {
            str(row.get("id") or "").strip()
            for row in (task.get("owners") or [])
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }
        fallback_owner_id = str(task.get("owner_id") or "").strip()
        if fallback_owner_id:
            owner_ids.add(fallback_owner_id)
        if not owner_ids:
            continue

        intake_tasks.append(
            {
                "id": task_id,
                "matter_id": matter_id,
                "owner_ids": owner_ids,
                "owner_rows": [row for row in (task.get("owners") or []) if isinstance(row, dict)],
            }
        )
        matter_ids.add(matter_id)
        owner_identifiers.update(owner_ids)

    if not intake_tasks or not matter_ids or not owner_identifiers:
        return {}

    numeric_owner_ids = {int(raw) for raw in owner_identifiers if str(raw).isdigit()}
    owner_filters = []
    if numeric_owner_ids:
        owner_filters.append(User.id.in_(sorted(numeric_owner_ids)))
    owner_filters.append(User.staff_party_id.in_(sorted(owner_identifiers)))

    owner_identifier_to_user_ids: dict[str, set[int]] = {}
    candidate_user_ids: set[int] = set()
    for user in User.query.filter(or_(*owner_filters)).all():
        try:
            user_id = int(getattr(user, "id", 0) or 0)
        except Exception:
            user_id = 0
        if user_id <= 0:
            continue
        candidate_user_ids.add(user_id)
        owner_identifier_to_user_ids.setdefault(str(user_id), set()).add(user_id)
        staff_party_id = str(getattr(user, "staff_party_id", None) or "").strip()
        if staff_party_id:
            owner_identifier_to_user_ids.setdefault(staff_party_id, set()).add(user_id)

    if not candidate_user_ids:
        return {}

    case_paths = {f"/case/{matter_id}": matter_id for matter_id in matter_ids}
    path_conditions = []
    for base_path in sorted(case_paths.keys()):
        path_conditions.extend(
            [
                UserAccessLog.path == base_path,
                UserAccessLog.path.like(f"{base_path}/%"),
                UserAccessLog.path.like(f"{base_path}?%"),
            ]
        )
    access_q = (
        db.session.query(UserAccessLog.user_id, UserAccessLog.path)
        .filter(UserAccessLog.user_id.in_(sorted(candidate_user_ids)))
        .filter(UserAccessLog.method.in_(("GET", "HEAD")))
        .filter(or_(*path_conditions))
    )
    if hasattr(UserAccessLog, "status_code"):
        access_q = access_q.filter(
            or_(UserAccessLog.status_code.is_(None), UserAccessLog.status_code < 400)
        )

    visited_user_ids_by_matter_id: dict[str, set[int]] = {}
    for user_id, path in access_q.distinct().all():
        raw_path = str(path or "").strip()
        matter_id = case_paths.get(raw_path)
        if not matter_id:
            for base_path, candidate_matter_id in case_paths.items():
                if raw_path.startswith(f"{base_path}/") or raw_path.startswith(f"{base_path}New"):
                    matter_id = candidate_matter_id
                    break
        if not matter_id:
            continue
        try:
            normalized_user_id = int(user_id)
        except Exception:
            continue
        visited_user_ids_by_matter_id.setdefault(matter_id, set()).add(normalized_user_id)

    if not visited_user_ids_by_matter_id:
        return {}

    recommendations_by_task_id: dict[str, dict] = {}
    for task in intake_tasks:
        matter_id = task["matter_id"]
        visited_user_ids = visited_user_ids_by_matter_id.get(matter_id) or set()
        if not visited_user_ids:
            continue

        visited_owner_names: list[str] = []
        for row in task["owner_rows"]:
            owner_id = str(row.get("id") or "").strip()
            if not owner_id:
                continue
            resolved_user_ids = owner_identifier_to_user_ids.get(owner_id) or set()
            if not resolved_user_ids.intersection(visited_user_ids):
                continue
            owner_name = str(row.get("name") or "").strip()
            if owner_name and owner_name not in visited_owner_names:
                visited_owner_names.append(owner_name)

        if not any(
            (owner_identifier_to_user_ids.get(owner_id) or set()).intersection(visited_user_ids)
            for owner_id in task["owner_ids"]
        ):
            continue

        recommendation_text = "Task Contact Matter page Confirm."
        if visited_owner_names:
            recommendation_text = (
                f"Task Contact Matter page  History: {', '.join(visited_owner_names)}"
            )

        recommendations_by_task_id[task["id"]] = {
            "recommended": True,
            "text": recommendation_text,
        }

    return recommendations_by_task_id
