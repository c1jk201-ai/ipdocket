from __future__ import annotations

from datetime import date, datetime

from flask import current_app
from flask_login import current_user

from app.extensions import db
from app.models.workflow import Workflow
from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


def _date_only_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        head = s[:10]
        if head[0:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit():
            return head
    return s


def _compact_event_match_text(value: object) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _is_deadline_event_key(value: object) -> bool:
    compact = _compact_event_match_text(value)
    if not compact:
        return False
    return any(
        token in compact
        for token in (
            "deadline",
            "duedate",
            "due",
            "period",
        )
    )


def apply_auto_status_from_db(*, matter, dom_patent: dict | None = None, **kwargs: dict) -> None:
    if not matter or not getattr(matter, "matter_id", None):
        return
    memo_txt = (matter.memo or "").strip()
    case_data = dom_patent
    if not case_data:
        for value in kwargs.values():
            if isinstance(value, dict):
                case_data = value
                break
    if case_data:
        memo_txt = (case_data.get("memo2") or memo_txt).strip()
    apply_auto_status_cache_to_matter(matter=matter, memo=memo_txt)
    try:
        _auto_complete_workflows_from_events(matter_id=str(matter.matter_id))
    except Exception:
        current_app.logger.error("Failed to auto-complete workflows for %s", matter.matter_id)


def _auto_complete_workflows_from_events(*, matter_id: str) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return
    rules = [
        ("Filing date", ["FilingDeadline", "Filing Deadline", "Filing deadline", " Confirm", "Confirm"]),
        ("ForeignFiling date", ["ForeignFilingDeadline", "Foreign Filing Deadline", "ForeignFilingDeadline", " Confirm", "Confirm"]),
        ("Examination request date", ["Examination requestDeadline", "Examination request Deadline"]),
        ("Registration date", ["RegistrationDeadline", "Registration Deadline", "RegistrationDeadline"]),
        ("Abandoned/Withdrawn", ["Abandoned", "Withdrawn"]),
        ("Done/Closed", ["", "Matter closed", "Done"]),
    ]
    target_keys = {key for key, _ in rules}
    rows = db.session.execute(
        text(
            """
            SELECT me.event_key, me.event_at
            FROM matter_event me
            WHERE me.matter_id = :mid
              AND me.event_at IS NOT NULL
              AND TRIM(me.event_at) <> ''
            """
        ).execution_options(policy_bypass=True),
        {"mid": mid},
    ).all()

    event_dates: dict[str, str] = {}
    all_event_dates: list[tuple[str, str]] = []
    for key, event_at in rows:
        key = (key or "").strip()
        event_at = _date_only_str(event_at)
        if event_at:
            all_event_dates.append((key, event_at))
            if key in target_keys:
                event_dates[key] = event_at
    if not all_event_dates:
        return

    workflows = Workflow.query.filter_by(case_id=mid).all()
    if not workflows:
        return

    from app.services.workflow.deferred_task_sync import (
        enqueue_workflow_sync,
        enqueue_workflow_task_sync,
    )
    from app.services.workflow.status_sync import sync_linked_docket_done_date_from_workflow

    try:
        actor_id = current_user.id if current_user.is_authenticated else None
    except Exception:
        actor_id = None

    def _is_overseas_filing_name(value: str) -> bool:
        return "ForeignFiling" in value or "Foreign Filing" in value

    def _allows_generic_event_match(workflow: Workflow) -> bool:
        business_code = str(getattr(workflow, "business_code", "") or "").strip().casefold()
        return business_code.startswith("docket:") or business_code.startswith("intake:")

    def _generic_event_match_date(workflow: Workflow, workflow_name: str) -> str:
        if not _allows_generic_event_match(workflow):
            return ""
        name_key = _compact_event_match_text(workflow_name)
        if len(name_key) < 4:
            return ""
        for event_key, event_date in all_event_dates:
            if _is_deadline_event_key(event_key):
                continue
            event_key_key = _compact_event_match_text(event_key)
            if len(event_key_key) < 4:
                continue
            if event_key_key in name_key or name_key in event_key_key:
                return event_date
        return ""

    for workflow in workflows:
        if (workflow.status or "") in ("Completed", "Abandoned"):
            continue
        name = (workflow.name or "").strip()
        if not name:
            continue
        matched_date = ""
        for event_key, keywords in rules:
            if not event_dates.get(event_key):
                continue
            if event_key == "Filing date" and _is_overseas_filing_name(name):
                continue
            if any(keyword in name for keyword in keywords):
                matched_date = event_dates[event_key]
                break
        if not matched_date:
            matched_date = _generic_event_match_date(workflow, name)
            if not matched_date:
                continue

        workflow.status = "Completed"
        try:
            workflow.completed_date = date.fromisoformat(matched_date)
        except Exception:
            workflow.completed_date = date.today()
        db.session.add(workflow)
        sync_linked_docket_done_date_from_workflow(workflow, completed_on=workflow.completed_date)
        try:
            enqueue_workflow_task_sync(workflow_id=int(workflow.id), actor_id=actor_id)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter.auto_status_apply.enqueue_workflow_task_sync",
                log_key="matter.auto_status_apply.enqueue_workflow_task_sync",
                log_window_seconds=300,
            )
        try:
            enqueue_workflow_sync(workflow_id=int(workflow.id))
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter.auto_status_apply.enqueue_workflow_sync",
                log_key="matter.auto_status_apply.enqueue_workflow_sync",
                log_window_seconds=300,
            )
