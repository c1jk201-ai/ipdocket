from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import or_

from app.extensions import db
from app.models.docket import DocketItem
from app.utils.workflow_deadline_labels import (
    workflow_deadline_kind_from_docket_id,
    workflow_deadline_title,
)


def _as_date(v: Any) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(".", "-").replace("/", "-")
    try:
        parts = s.split(" ")[0].split("-")
        if len(parts) >= 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None
    return None


def _get_first_attr(obj: Any, names: list[str]) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _set_first_attr(obj: Any, names: list[str], value: Any) -> bool:
    for n in names:
        if hasattr(obj, n):
            try:
                setattr(obj, n, value)
                return True
            except Exception:
                continue
    return False


@dataclass(frozen=True)
class DocketTemplate:
    key: str
    label: str
    draft_offset_days: int
    submit_offset_days: int


TEMPLATES: dict[str, DocketTemplate] = {
    "DEFAULT": DocketTemplate("DEFAULT", "Default(Internal D-7)", -7, -1),
    "OA": DocketTemplate("OA", "OA (Internal D-10)", -10, -2),
    "FILING": DocketTemplate("FILING", "Filing(Internal D-5)", -5, 0),
    "APPEAL": DocketTemplate("APPEAL", "/(Internal D-14)", -14, -3),
}


def pick_template_key(workflow_code: str | None) -> str:
    c = (workflow_code or "").upper()
    if "OA" in c or "OFFICE" in c:
        return "OA"
    if "FILING" in c or "FILE" in c or "Filing" in c:
        return "FILING"
    if "APPEAL" in c or "OPPOSITION" in c or "" in c or "" in c:
        return "APPEAL"
    return "DEFAULT"


def workflow_internal_due_from_template(
    base_legal_due: date | None,
    *,
    template_key: str | None = None,
    workflow_code: str | None = None,
) -> date | None:
    if not base_legal_due:
        return None
    key = (template_key or "").strip().upper() or pick_template_key(str(workflow_code or ""))
    tpl = TEMPLATES.get(key) or TEMPLATES["DEFAULT"]
    candidates = [
        base_legal_due,
        base_legal_due + timedelta(days=tpl.draft_offset_days),
        base_legal_due + timedelta(days=tpl.submit_offset_days),
    ]
    return min(candidates)


def _workflow_docket_kind_for_row(row: DocketItem) -> str | None:
    return workflow_deadline_kind_from_docket_id(getattr(row, "docket_id", None)) or (
        workflow_deadline_kind_from_docket_id(getattr(row, "raw_id", None))
    )


def _mark_workflow_docket_deleted(row: DocketItem, *, reason: str) -> None:
    if hasattr(row, "is_deleted"):
        row.is_deleted = True
    if hasattr(row, "deleted_at"):
        row.deleted_at = datetime.utcnow()
    if hasattr(row, "deleted_by"):
        row.deleted_by = None
    if hasattr(row, "delete_reason"):
        row.delete_reason = reason


def ensure_workflow_dockets(
    workflow: Any,
    *,
    template_key: str | None = None,
    base_legal_due: date | None = None,
    commit: bool = True,
) -> dict[str, str]:
    """
    Create/update the workflow-generated LEG docket under the unified
    final/internal deadline model.

    - `due_date` stores the final/legal deadline.
    - `extended_due_date` stores the distinct internal deadline only.
    - Legacy DRAFT/SUBMIT workflow-generated docket rows are soft-deleted.
    """
    wf_id = getattr(workflow, "id", None)
    if not wf_id:
        return {}

    matter_id = _get_first_attr(workflow, ["matter_id", "case_id", "matter_uuid"])
    if not matter_id:
        return {}

    code = _get_first_attr(workflow, ["code", "workflow_code", "business_code"]) or ""
    title = _get_first_attr(workflow, ["title", "name", "subject"]) or ""

    legal_due = base_legal_due
    if not legal_due:
        legal_due = _as_date(
            _get_first_attr(
                workflow,
                [
                    "legal_due_date",
                    "law_due_date",
                    "due_date",
                    "deadline",
                    "statutory_due_date",
                ],
            )
        )
    if not legal_due:
        return {}

    workflow_due = _as_date(_get_first_attr(workflow, ["due_date", "internal_due_date"]))
    internal_due = workflow_due or workflow_internal_due_from_template(
        legal_due,
        template_key=template_key,
        workflow_code=str(code),
    )
    if workflow_due is None and internal_due is not None:
        _set_first_attr(workflow, ["due_date"], internal_due)
    distinct_internal_due = internal_due if internal_due and internal_due != legal_due else None

    assignee_id = _get_first_attr(workflow, ["assignee_id", "owner_id", "user_id", "handler_id"])
    priority = _get_first_attr(workflow, ["priority", "priority_level"]) or "MEDIUM"
    assignee_user_id: int | None = None
    if assignee_id is not None:
        try:
            assignee_user_id = int(assignee_id)
        except Exception:
            assignee_user_id = None

    owner_staff_party_id = _get_first_attr(
        workflow, ["owner_staff_party_id", "staff_party_id", "staff_pid"]
    )
    owner_staff_party_id = (
        (str(owner_staff_party_id).strip() or None) if owner_staff_party_id else None
    )
    if not owner_staff_party_id and assignee_user_id is not None:
        try:
            from app.models.user import User

            user = User.query.get(assignee_user_id)
            if user and user.staff_party_id:
                owner_staff_party_id = str(user.staff_party_id).strip() or None
        except Exception:
            owner_staff_party_id = None

    try:
        from app.utils.task_classification import determine_category_by_staff_role

        resolved_category = determine_category_by_staff_role(
            str(matter_id),
            assignee_id=assignee_user_id,
            staff_party_id=owner_staff_party_id,
        )
    except Exception:
        resolved_category = "WORK"

    canonical_id = f"WF-{wf_id}-LEG"
    prefix = f"WF-{wf_id}-"
    rows = (
        DocketItem.query.filter_by(matter_id=matter_id)
        .filter(
            or_(
                DocketItem.docket_id.like(f"{prefix}%"),
                DocketItem.raw_id.like(f"{prefix}%"),
            )
        )
        .all()
    )

    leg_row = None
    for row in rows:
        if _workflow_docket_kind_for_row(row) == "LEG":
            leg_row = row
            break

    if leg_row is None:
        leg_row = DocketItem(docket_id=canonical_id, matter_id=matter_id)

    docket_name = workflow_deadline_title(
        title,
        "LEG",
        legal_due_date=legal_due,
        effective_due_date=internal_due or legal_due,
    )
    leg_row.category = resolved_category
    if hasattr(leg_row, "raw_id"):
        leg_row.raw_id = canonical_id
    if hasattr(leg_row, "is_deleted"):
        leg_row.is_deleted = False
    if hasattr(leg_row, "deleted_at"):
        leg_row.deleted_at = None
    if hasattr(leg_row, "deleted_by"):
        leg_row.deleted_by = None
    if hasattr(leg_row, "delete_reason"):
        leg_row.delete_reason = None
    leg_row.name_ref = docket_name
    leg_row.name_free = docket_name
    leg_row.due_date = legal_due.isoformat()
    leg_row.extended_due_date = distinct_internal_due.isoformat() if distinct_internal_due else None
    if owner_staff_party_id:
        _set_first_attr(leg_row, ["owner_staff_party_id"], owner_staff_party_id)
    if assignee_user_id is not None:
        _set_first_attr(leg_row, ["assignee_id", "owner_id"], assignee_user_id)
    _set_first_attr(leg_row, ["priority", "priority_level"], priority)
    _set_first_attr(leg_row, ["category"], resolved_category)

    db.session.add(leg_row)
    db.session.flush()

    for row in rows:
        if row is leg_row:
            continue
        kind = _workflow_docket_kind_for_row(row)
        if kind not in {"LEG", "DRA", "SUB"}:
            continue
        _mark_workflow_docket_deleted(
            row,
            reason="workflow_quickadd_legacy_deadline_removed",
        )
        db.session.add(row)

    did = getattr(leg_row, "docket_id", getattr(leg_row, "id", None))
    created: dict[str, str] = {}
    if did:
        created["LEG"] = str(did)

    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return created
