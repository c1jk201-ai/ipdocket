from __future__ import annotations

import math
from typing import Any

from sqlalchemy import and_, func, or_

from app.models.workflow import Workflow
from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES

TC_SCOPE_CANDIDATE = "candidate"
TC_SCOPE_WORK = "work"
TC_SCOPE_MGMT = "mgmt"
TC_SCOPE_ALL = "all"

_WORK_CATEGORIES_UPPER = tuple(sorted({str(c).upper() for c in WORK_CATEGORIES if c}))
_MGMT_CATEGORIES_UPPER = tuple(sorted({str(c).upper() for c in MGMT_CATEGORIES if c}))


def normalize_tc_scope(raw: Any, *, default: str = TC_SCOPE_CANDIDATE) -> str:
    fallback = str(default or TC_SCOPE_CANDIDATE).strip().lower() or TC_SCOPE_CANDIDATE
    scope = str(raw or fallback).strip().lower()
    if scope in {"all", "all_tasks"}:
        return TC_SCOPE_ALL
    if scope in {"work", "strict", "strict_work"}:
        return TC_SCOPE_WORK
    if scope in {"mgmt", "management"}:
        return TC_SCOPE_MGMT
    return TC_SCOPE_CANDIDATE


def apply_tc_scope_filter(query, scope: str):
    """Apply the shared TC inclusion rules to a Workflow query."""
    query = query.filter(
        or_(Workflow.business_code.is_(None), Workflow.business_code.notlike("ANNUITY:%"))
    )

    normalized = normalize_tc_scope(scope)
    if normalized == TC_SCOPE_ALL:
        return query

    category_upper = func.upper(func.coalesce(Workflow.category, ""))
    business_code_upper = func.upper(func.coalesce(Workflow.business_code, ""))

    if normalized == TC_SCOPE_WORK:
        return query.filter(category_upper.in_(_WORK_CATEGORIES_UPPER))

    if normalized == TC_SCOPE_MGMT:
        return query.filter(
            or_(
                category_upper.in_(_MGMT_CATEGORIES_UPPER),
                business_code_upper.like("MGMT:%"),
            )
        )

    return query.filter(
        or_(
            category_upper.in_(_WORK_CATEGORIES_UPPER),
            and_(
                or_(
                    Workflow.category.is_(None),
                    func.trim(func.coalesce(Workflow.category, "")) == "",
                ),
                ~business_code_upper.like("MGMT:%"),
            ),
        )
    )


def parse_finite_float(value: Any) -> float | None:
    """Parse a float and reject non-finite values such as NaN or infinity."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_tc_hours(value: Any) -> float | None:
    return parse_finite_float(value)
