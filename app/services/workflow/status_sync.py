from __future__ import annotations

import re
from datetime import date

from app.extensions import db
from app.models.docket import DocketItem
from app.models.workflow import Workflow
from app.utils.docket_dates import (
    adjusted_legal_due_for_docket,
    effective_due_for_work,
    internal_due_for_docket,
)
from app.utils.workflow_deadline_labels import strip_workflow_deadline_title_suffix

_DOCKET_BC_RE = re.compile(r"^DOCKET:([^:]+)", re.IGNORECASE)


def _date_to_ymd(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raw = (str(value or "").strip() if value is not None else "").strip()
    if not raw:
        return ""
    return raw[:10]


def _coerce_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    raw = _date_to_ymd(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def _distinct_internal_due_date(
    *,
    legal_due_date: date | None,
    internal_due_date: date | None,
) -> date | None:
    if not internal_due_date:
        return None
    if legal_due_date and internal_due_date == legal_due_date:
        return None
    return internal_due_date


def _is_valid_linked_docket_item(
    workflow: Workflow | None,
    docket_item: DocketItem | None,
) -> bool:
    if docket_item is None:
        return False

    docket_id = str(getattr(docket_item, "docket_id", "") or "").strip()
    if not docket_id:
        return False
    if hasattr(docket_item, "is_deleted") and bool(getattr(docket_item, "is_deleted", False)):
        return False
    if workflow is not None and str(getattr(docket_item, "matter_id", "")) != str(
        getattr(workflow, "case_id", "")
    ):
        return False
    return True


def linked_docket_item_for_workflow(workflow: Workflow | None) -> DocketItem | None:
    """If this workflow is docket-backed (business_code=DOCKET:...), return linked DocketItem."""
    if not workflow:
        return None

    business_code = (getattr(workflow, "business_code", None) or "").strip()
    m = _DOCKET_BC_RE.match(business_code)
    if not m:
        return None

    docket_id = (m.group(1) or "").strip()
    if not docket_id:
        return None

    di = DocketItem.query.filter_by(docket_id=docket_id).first()
    if not _is_valid_linked_docket_item(workflow, di):
        return None
    return di


def workflow_display_values(
    workflow: Workflow | None,
    *,
    linked_docket_item: DocketItem | None = None,
) -> dict[str, object]:
    di = (
        linked_docket_item
        if _is_valid_linked_docket_item(workflow, linked_docket_item)
        else linked_docket_item_for_workflow(workflow)
    )

    display_name = getattr(workflow, "name", None) if workflow is not None else None
    legal_due = _coerce_date(
        getattr(workflow, "legal_due_date", None) if workflow is not None else None
    ) or _coerce_date(getattr(workflow, "due_date", None) if workflow is not None else None)
    raw_internal_due = _coerce_date(
        getattr(workflow, "due_date", None) if workflow is not None else None
    )
    internal_due = _distinct_internal_due_date(
        legal_due_date=legal_due,
        internal_due_date=raw_internal_due,
    )
    due_date = raw_internal_due or legal_due

    if display_name is None and di is not None:
        display_name = getattr(di, "name_free", None) or getattr(di, "name_ref", None)

    # Linked dockets seed workflow due dates, but Task /Notice workflow  value .
    # New rows where workflow due dates were never initialized still fall back to the docket.
    if legal_due is None and due_date is None and di is not None:
        legal_due = adjusted_legal_due_for_docket(
            getattr(di, "due_date", None),
            getattr(di, "extended_due_date", None),
        )
        internal_due = internal_due_for_docket(
            getattr(di, "due_date", None),
            getattr(di, "extended_due_date", None),
        )
        due_date = internal_due or legal_due

    display_name = strip_workflow_deadline_title_suffix(display_name)

    return {
        "linked_docket_id": getattr(di, "docket_id", None) if di is not None else None,
        "name": str(display_name or "").strip() or None,
        "legal_due_date": legal_due,
        "internal_due_date": internal_due,
        "due_date": due_date,
    }


def docket_due_values_for_workflow_sync(
    docket_item: DocketItem | None,
) -> tuple[date | None, date | None]:
    """Return (workflow_due_date, workflow_legal_due_date) derived from a docket."""
    if docket_item is None:
        return None, None
    due_date = effective_due_for_work(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    legal_due_date = adjusted_legal_due_for_docket(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    return due_date, legal_due_date


def sync_workflow_due_dates_from_docket_source(
    workflow: Workflow | None,
    *,
    due_date: date | None,
    legal_due_date: date | None,
) -> bool:
    """Apply docket-derived due dates to a Workflow when the source docket dates changed.

    Workflow due fields remain independently editable after creation. The source snapshot
    lets us tell a workflow-side edit apart from a later docket-side deadline change.
    """
    if not workflow:
        return False

    current_due_date = _coerce_date(getattr(workflow, "due_date", None))
    current_legal_due_date = _coerce_date(getattr(workflow, "legal_due_date", None))
    source_due_date = _coerce_date(getattr(workflow, "source_docket_due_date", None))
    source_legal_due_date = _coerce_date(getattr(workflow, "source_docket_legal_due_date", None))
    has_source_snapshot = source_due_date is not None or source_legal_due_date is not None

    should_apply_due_dates = False
    if getattr(workflow, "id", None) is None and not has_source_snapshot:
        should_apply_due_dates = True
    elif has_source_snapshot:
        should_apply_due_dates = (
            source_due_date != due_date or source_legal_due_date != legal_due_date
        )
    elif due_date is None and legal_due_date is None:
        should_apply_due_dates = True
    elif current_due_date is None and current_legal_due_date is None:
        should_apply_due_dates = True

    changed = False
    if should_apply_due_dates:
        if current_due_date != due_date:
            workflow.due_date = due_date
            changed = True
        if current_legal_due_date != legal_due_date:
            workflow.legal_due_date = legal_due_date
            changed = True

    if source_due_date != due_date:
        workflow.source_docket_due_date = due_date
        changed = True
    if source_legal_due_date != legal_due_date:
        workflow.source_docket_legal_due_date = legal_due_date
        changed = True

    if changed:
        db.session.add(workflow)
    return changed


def reconcile_linked_docket_workflow_fields(
    workflow: Workflow | None,
    *,
    linked_docket_item: DocketItem | None = None,
) -> bool:
    if not workflow:
        return False

    di = (
        linked_docket_item
        if _is_valid_linked_docket_item(workflow, linked_docket_item)
        else linked_docket_item_for_workflow(workflow)
    )
    if di is None:
        return False

    due_date, legal_due_date = docket_due_values_for_workflow_sync(di)
    return sync_workflow_due_dates_from_docket_source(
        workflow,
        due_date=due_date,
        legal_due_date=legal_due_date,
    )


def compute_synced_done_date(
    *,
    status: str | None,
    current_done_date: str | None,
    completed_on: date | str | None = None,
) -> str | None:
    """
    Compute expected DocketItem.done_date from workflow status.

    Returns:
    - YYYY-MM-DD for Completed (when current is empty/auto marker),
    - AUTO_CANCELLED:YYYY-MM-DD for Abandoned,
    - None for all other statuses.
    """
    status_now = (status or "").strip()
    current_done = (current_done_date or "").strip()
    completed_ymd = _date_to_ymd(completed_on) or date.today().isoformat()

    if status_now == "Completed":
        # Preserve explicit/manual done dates.
        if current_done and not current_done.upper().startswith("AUTO_"):
            return current_done
        return completed_ymd

    if status_now == "Abandoned":
        if current_done.startswith("AUTO_CANCELLED:"):
            return current_done
        return f"AUTO_CANCELLED:{completed_ymd}"

    return None


def sync_linked_docket_done_date_from_workflow(
    workflow: Workflow | None,
    *,
    completed_on: date | str | None = None,
) -> DocketItem | None:
    """
    Keep linked DocketItem.done_date consistent for DOCKET-backed workflows.

    Rules:
    - Completed: set YYYY-MM-DD when open/auto-marked.
    - Abandoned: set AUTO_CANCELLED:YYYY-MM-DD.
    - Other statuses: clear done_date.
    """
    di = linked_docket_item_for_workflow(workflow)
    if not di or not workflow:
        return di

    expected_done = compute_synced_done_date(
        status=getattr(workflow, "status", None),
        current_done_date=getattr(di, "done_date", None),
        completed_on=completed_on or getattr(workflow, "completed_date", None),
    )
    di.done_date = expected_done

    db.session.add(di)
    return di
