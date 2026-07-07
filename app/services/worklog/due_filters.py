from __future__ import annotations

from datetime import date

from flask import current_app
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Load

from app.models.ip_records import DocketItem, Matter
from app.models.workflow import Workflow
from app.utils.timezone import today_local

VALID_WORKLOG_DUE_AXES = frozenset({"all", "final", "internal"})


def today_in_app_timezone() -> date:
    return today_local(current_app.config.get("TIMEZONE"))


def effective_docket_due_expr():
    # DocketItem stores due dates as ISO strings (YYYY-MM-DD). Keep comparisons lexicographic.
    return func.coalesce(
        func.nullif(DocketItem.extended_due_date, ""),
        func.nullif(DocketItem.due_date, ""),
    )


def normalize_worklog_due_axis(value: object, *, default: str = "all") -> str:
    token = str(value or "").strip().lower()
    if token in VALID_WORKLOG_DUE_AXES:
        return token
    return default if default in VALID_WORKLOG_DUE_AXES else "all"


def worklog_final_due_expr():
    return func.coalesce(Workflow.legal_due_date, Workflow.due_date)


def worklog_internal_due_expr():
    # H-4 fix: legal_due_date NULL  due_date internal 
    # worklog_final_due_expr()from   final  In Progress Display.
    # internal due  legal_due_date  due_date   .
    return case(
        (
            and_(
                Workflow.due_date.isnot(None),
                Workflow.legal_due_date.isnot(None),
                Workflow.due_date != Workflow.legal_due_date,
            ),
            Workflow.due_date,
        ),
        else_=None,
    )


def worklog_due_expr_for_axis(due_axis: str):
    normalized = normalize_worklog_due_axis(due_axis, default="all")
    if normalized == "final":
        return worklog_final_due_expr()
    if normalized == "internal":
        return worklog_internal_due_expr()
    return func.coalesce(Workflow.due_date, Workflow.legal_due_date)


def worklog_calendar_due_range_condition(
    *,
    start_date: date,
    end_date: date,
    due_axis: str,
):
    normalized_axis = normalize_worklog_due_axis(due_axis, default="all")
    final_due_expr = worklog_final_due_expr()
    internal_due_expr = worklog_internal_due_expr()

    def _in_range(expr):
        return and_(expr.isnot(None), expr >= start_date, expr <= end_date)

    if normalized_axis == "final":
        due_in_range = _in_range(final_due_expr)
    elif normalized_axis == "internal":
        due_in_range = _in_range(internal_due_expr)
    else:
        due_in_range = or_(_in_range(final_due_expr), _in_range(internal_due_expr))

    # Keep legacy docket-backed workflows whose due dates have not been materialized
    # on the workflow row yet. Those rows still need linked-docket fallback.
    # M-5 fix: Done/Cancel workflow legacy fallbackfrom  
    # Closed tasks still need a due-axis value for filtering.
    legacy_due_fallback = and_(
        Workflow.business_code.like("DOCKET:%"),
        Workflow.legal_due_date.is_(None),
        Workflow.due_date.is_(None),
        Workflow.status.notin_(["Completed", "Abandoned"]),
    )
    return or_(due_in_range, legacy_due_fallback)


def worklog_calendar_query_options():
    return (
        Load(Workflow).load_only(
            Workflow.id,
            Workflow.case_id,
            Workflow.name,
            Workflow.status,
            Workflow.category,
            Workflow.business_code,
            Workflow.note,
            Workflow.due_date,
            Workflow.legal_due_date,
            Workflow.completed_date,
            Workflow.assignee_id,
            Workflow.attorney_assignee_id,
            Workflow.inspector_id,
        ),
        Load(Matter).load_only(
            Matter.matter_id,
            Matter.our_ref,
        ),
    )
