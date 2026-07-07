from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.extensions import db
from app.models.docket import DocketItem
from app.models.workflow import Workflow
from app.services.docket_manual_state import (
    clear_docket_manual_abandoned,
    mark_docket_manual_abandoned,
)
from app.services.workflow.status_sync import (
    linked_docket_item_for_workflow,
    sync_linked_docket_done_date_from_workflow,
)
from app.services.workflow.task_sync import ensure_worklog_for_docket


@dataclass(frozen=True)
class WorkflowStatusTransitionResult:
    old_status: str
    new_status: str
    status_changed: bool
    completed_date_changed: bool
    linked_docket_id: str | None = None
    linked_docket_done_date: str | None = None
    worklog_synced: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.status_changed or self.completed_date_changed)


def normalize_workflow_status(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return Workflow.STATUS_PENDING
    normalized = raw.lower().replace("_", " ").replace("-", " ")
    status_map = {
        "pending": Workflow.STATUS_PENDING,
        "in progress": Workflow.STATUS_IN_PROGRESS,
        "progress": Workflow.STATUS_IN_PROGRESS,
        "started": Workflow.STATUS_IN_PROGRESS,
        "completed": Workflow.STATUS_COMPLETED,
        "complete": Workflow.STATUS_COMPLETED,
        "done": Workflow.STATUS_COMPLETED,
        "abandoned": Workflow.STATUS_ABANDONED,
        "cancelled": Workflow.STATUS_ABANDONED,
        "canceled": Workflow.STATUS_ABANDONED,
        "excluded": Workflow.STATUS_ABANDONED,
        "Task ": Workflow.STATUS_ABANDONED,
        "Task": Workflow.STATUS_ABANDONED,
        "Task Abandoned": Workflow.STATUS_ABANDONED,
        "TaskAbandoned": Workflow.STATUS_ABANDONED,
        "Abandoned": Workflow.STATUS_ABANDONED,
    }
    return status_map.get(normalized, raw)


def _coerce_date(value: object | None) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    raw = str(value or "").strip()
    if not raw:
        return None
    if "T" in raw:
        raw = raw.split("T", 1)[0].strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def apply_workflow_status_transition(
    workflow: Workflow,
    new_status: object,
    *,
    actor_id: int | None = None,
    note: str | None = None,
    completed_on: date | str | None = None,
    linked_docket_item: DocketItem | None = None,
    sync_worklog: bool = True,
    strict: bool = True,
) -> WorkflowStatusTransitionResult:
    """Apply workflow status side effects without committing the current session."""

    if workflow is None:
        raise ValueError("workflow is required")

    old_status = str(getattr(workflow, "status", None) or Workflow.STATUS_PENDING).strip()
    old_completed_date = getattr(workflow, "completed_date", None)
    status = normalize_workflow_status(new_status)
    if strict and status not in Workflow.STATUSES:
        raise ValueError(f"unsupported workflow status: {new_status}")

    workflow.status = status
    terminal = status in Workflow.TERMINAL_STATUSES

    if terminal:
        resolved_completed_on = _coerce_date(completed_on)
        if resolved_completed_on is None:
            resolved_completed_on = old_completed_date if old_status == status else None
        workflow.completed_date = resolved_completed_on or date.today()
        if actor_id:
            workflow.completed_by_id = actor_id
    else:
        workflow.completed_date = None
        workflow.completed_by_id = None

    linked = linked_docket_item
    if linked is None:
        linked = linked_docket_item_for_workflow(workflow)

    worklog_synced = False
    if linked is not None:
        if status == Workflow.STATUS_ABANDONED:
            mark_docket_manual_abandoned(
                linked,
                reason=note,
                when=getattr(workflow, "completed_date", None),
            )
        else:
            clear_docket_manual_abandoned(linked)
        sync_linked_docket_done_date_from_workflow(workflow)
        if sync_worklog:
            wl = ensure_worklog_for_docket(docket_item=linked, actor_id=actor_id)
            worklog_synced = wl is not None

    db.session.add(workflow)
    if linked is not None:
        db.session.add(linked)

    return WorkflowStatusTransitionResult(
        old_status=old_status,
        new_status=status,
        status_changed=old_status != status,
        completed_date_changed=old_completed_date != getattr(workflow, "completed_date", None),
        linked_docket_id=getattr(linked, "docket_id", None) if linked is not None else None,
        linked_docket_done_date=getattr(linked, "done_date", None) if linked is not None else None,
        worklog_synced=worklog_synced,
    )
