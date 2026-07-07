from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterStatusHistory
from app.models.workflow import Workflow
from app.services.case.post_registration_followups import (
    docket_id_from_workflow_business_code,
    is_post_registration_mgmt_docket,
)
from app.services.case.terminal_status import is_future_term_expiry_status, is_terminal_case_status
from app.services.workflow.task_sync import ensure_worklog_for_docket
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaseStatusCleanupResult:
    applied: bool = False
    docket_closed: int = 0
    workflow_closed: int = 0
    worklog_closed: int = 0


@dataclass(frozen=True)
class CaseStatusCleanupBatchResult:
    processed_cases: int = 0
    closed_cases: int = 0
    docket_closed: int = 0
    workflow_closed: int = 0
    worklog_closed: int = 0


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _coerce_status_date(value: object) -> date:
    if isinstance(value, date):
        return value

    raw = _normalize_text(value)
    if raw.startswith(("AUTO_CANCELLED:", "AUTO_EXPIRED:")):
        raw = raw.split(":", 1)[1].strip()
    if "T" in raw:
        raw = raw.split("T", 1)[0].strip()
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError as exc:
            report_swallowed_exception(
                exc,
                context="status_task_cleanup.coerce_status_date",
                log_key="status_task_cleanup.coerce_status_date",
                log_window_seconds=300,
            )
    return date.today()


def _workflow_closure_note(*, status_text: str, note: str | None = None) -> str:
    base = f"[Matter Status Change:{status_text}] Task "
    extra = _normalize_text(note)
    if not extra:
        return base
    return f"{base} - {extra}"


def _append_unique_line(existing: str | None, line: str) -> str:
    current = _normalize_text(existing)
    target = _normalize_text(line)
    if not target:
        return current
    if not current:
        return target
    existing_lines = {item.strip() for item in current.splitlines() if item.strip()}
    if target in existing_lines:
        return current
    return f"{current}\n{target}".strip()


def terminal_case_status_value(
    matter: Matter | None,
    *,
    inhouse_status: str | None = None,
    status_blue: str | None = None,
    status_red: str | None = None,
    status_red_related_date: str | None = None,
) -> str | None:
    candidates = (
        _normalize_text(
            inhouse_status
            if inhouse_status is not None
            else getattr(matter, "inhouse_status", None)
        ),
        _normalize_text(
            status_blue if status_blue is not None else getattr(matter, "status_blue", None)
        ),
    )
    for value in candidates:
        if value and is_terminal_case_status(value):
            return value

    red_value = _normalize_text(
        status_red if status_red is not None else getattr(matter, "status_red", None)
    )
    red_related_date = (
        status_red_related_date
        if status_red_related_date is not None
        else (getattr(matter, "status_red_related_date", None) if matter is not None else None)
    )
    if is_future_term_expiry_status(red_value, red_related_date):
        return None
    if red_value and is_terminal_case_status(red_value):
        return red_value
    return None


def _latest_terminal_status_date_for_matter(matter_id: str) -> date:
    rows = (
        MatterStatusHistory.query.filter(MatterStatusHistory.matter_id == matter_id)
        .order_by(MatterStatusHistory.created_at.desc(), MatterStatusHistory.id.desc())
        .all()
    )
    for row in rows:
        if is_terminal_case_status(getattr(row, "status", None)):
            return _coerce_status_date(getattr(row, "status_date", None))
    return date.today()


def cleanup_case_related_tasks_if_terminal(
    *,
    matter_id: str | None,
    old_status: str | None,
    new_status: str | None,
    status_date: object = None,
    note: str | None = None,
    actor_id: int | None = None,
    force: bool = False,
    commit: bool = True,
) -> CaseStatusCleanupResult:
    matter_id_text = _normalize_text(matter_id)
    new_status_text = _normalize_text(new_status)
    old_status_text = _normalize_text(old_status)

    if not matter_id_text or not new_status_text:
        return CaseStatusCleanupResult()
    if not force and old_status_text == new_status_text:
        return CaseStatusCleanupResult()
    if is_future_term_expiry_status(new_status_text, status_date):
        return CaseStatusCleanupResult()
    if not is_terminal_case_status(new_status_text):
        return CaseStatusCleanupResult()

    cleanup_date = _coerce_status_date(status_date)
    done_value = f"AUTO_CANCELLED:{cleanup_date.isoformat()}"
    closure_note = _workflow_closure_note(status_text=new_status_text, note=note)

    docket_closed = 0
    workflow_closed = 0
    worklog_closed = 0

    docket_rows = (
        DocketItem.query.filter(DocketItem.matter_id == matter_id_text)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .order_by(DocketItem.docket_id.asc())
        .all()
    )
    for docket_item in docket_rows:
        if is_post_registration_mgmt_docket(docket_item):
            continue

        docket_item.done_date = done_value
        db.session.add(docket_item)
        docket_closed += 1

        wl = ensure_worklog_for_docket(docket_item=docket_item, actor_id=actor_id)
        if wl is None:
            continue
        wl.action_type = "abandoned"
        wl.status = "abandoned"
        wl.description = closure_note
        wl.completed_at = datetime.utcnow()
        if actor_id:
            wl.completed_by_id = actor_id
        db.session.add(wl)
        worklog_closed += 1

    workflow_rows = (
        Workflow.query.filter(Workflow.case_id == matter_id_text)
        .filter(or_(Workflow.status.is_(None), Workflow.status.notin_(("Completed", "Abandoned"))))
        .order_by(Workflow.id.asc())
        .all()
    )
    preserved_docket_ids = {
        _normalize_text(getattr(row, "docket_id", None))
        for row in docket_rows
        if is_post_registration_mgmt_docket(row)
    }
    for workflow in workflow_rows:
        if (
            docket_id_from_workflow_business_code(getattr(workflow, "business_code", None))
            in preserved_docket_ids
        ):
            continue

        workflow.status = "Abandoned"
        workflow.completed_date = cleanup_date
        if actor_id:
            workflow.completed_by_id = actor_id
        workflow.note = _append_unique_line(getattr(workflow, "note", None), closure_note) or None
        db.session.add(workflow)
        workflow_closed += 1

    if commit:
        db.session.commit()

    return CaseStatusCleanupResult(
        applied=True,
        docket_closed=docket_closed,
        workflow_closed=workflow_closed,
        worklog_closed=worklog_closed,
    )


def apply_case_status_side_effects(
    *,
    matter_id: str | None,
    old_status: str | None,
    new_status: str | None,
    status_date: object = None,
    note: str | None = None,
    actor_id: int | None = None,
    force: bool = False,
    logger_override=None,
) -> CaseStatusCleanupResult:
    active_logger = logger_override or logger
    try:
        task_result = cleanup_case_related_tasks_if_terminal(
            matter_id=matter_id,
            old_status=old_status,
            new_status=new_status,
            status_date=status_date,
            note=note,
            actor_id=actor_id,
            force=force,
            commit=True,
        )
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            active_logger.debug("Task cleanup rollback failed", exc_info=True)
        active_logger.warning(
            "Task cleanup failed after case status change (matter_id=%s, old=%s, new=%s): %s",
            matter_id,
            old_status,
            new_status,
            exc,
        )
        task_result = CaseStatusCleanupResult()

    return CaseStatusCleanupResult(
        applied=bool(task_result.applied),
        docket_closed=task_result.docket_closed,
        workflow_closed=task_result.workflow_closed,
        worklog_closed=task_result.worklog_closed,
    )


def _candidate_terminal_case_ids(limit: int) -> list[str]:
    safe_limit = max(1, min(int(limit or 0), 5000))
    seen: set[str] = set()
    out: list[str] = []

    docket_rows = (
        db.session.query(DocketItem.matter_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .order_by(DocketItem.matter_id.asc())
        .limit(safe_limit)
        .all()
    )
    workflow_rows = (
        db.session.query(Workflow.case_id)
        .filter(or_(Workflow.status.is_(None), Workflow.status.notin_(("Completed", "Abandoned"))))
        .order_by(Workflow.case_id.asc())
        .limit(safe_limit)
        .all()
    )

    for (raw_id,) in list(docket_rows) + list(workflow_rows):
        matter_id = _normalize_text(raw_id)
        if not matter_id or matter_id in seen:
            continue
        seen.add(matter_id)
        out.append(matter_id)
        if len(out) >= safe_limit:
            break
    return out


def reconcile_terminal_case_open_items(
    *,
    limit: int = 200,
    commit: bool = True,
    logger_override=None,
) -> CaseStatusCleanupBatchResult:
    active_logger = logger_override or logger
    processed_cases = 0
    closed_cases = 0
    docket_closed = 0
    workflow_closed = 0
    worklog_closed = 0

    for matter_id in _candidate_terminal_case_ids(limit):
        processed_cases += 1
        savepoint = None
        try:
            savepoint = db.session.begin_nested()
            matter = db.session.get(Matter, matter_id)
            status_text = terminal_case_status_value(matter)
            if not status_text:
                savepoint.commit()
                continue
            result = cleanup_case_related_tasks_if_terminal(
                matter_id=matter_id,
                old_status=None,
                new_status=status_text,
                status_date=_latest_terminal_status_date_for_matter(matter_id),
                actor_id=None,
                force=True,
                commit=False,
            )
            if result.docket_closed or result.workflow_closed or result.worklog_closed:
                closed_cases += 1
                docket_closed += result.docket_closed
                workflow_closed += result.workflow_closed
                worklog_closed += result.worklog_closed
            savepoint.commit()
        except Exception as exc:
            if savepoint is not None:
                try:
                    savepoint.rollback()
                except Exception:
                    active_logger.debug(
                        "Failed to rollback terminal case cleanup savepoint",
                        exc_info=True,
                    )
            active_logger.warning(
                "Terminal case cleanup skipped after error (matter_id=%s): %s",
                matter_id,
                exc,
            )

    if commit:
        db.session.commit()

    return CaseStatusCleanupBatchResult(
        processed_cases=processed_cases,
        closed_cases=closed_cases,
        docket_closed=docket_closed,
        workflow_closed=workflow_closed,
        worklog_closed=worklog_closed,
    )
