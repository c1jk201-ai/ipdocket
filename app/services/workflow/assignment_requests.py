from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.extensions import db
from app.models.user import User
from app.models.workflow import Workflow
from app.models.workflow_assignment_request import WorkflowAssignmentRequest
from app.services.workflow.status_sync import linked_docket_item_for_workflow
from app.services.workflow.task_sync import persist_manual_workflow_assignment_override
from app.utils.workflow_semantics import derive_workflow_category

ROLE_FIELDS: dict[str, str] = {
    WorkflowAssignmentRequest.ROLE_HANDLER: "assignee_id",
    WorkflowAssignmentRequest.ROLE_ATTORNEY: "attorney_assignee_id",
    WorkflowAssignmentRequest.ROLE_MANAGER: "inspector_id",
}
ROLE_LABELS: dict[str, str] = {
    WorkflowAssignmentRequest.ROLE_HANDLER: "Handler",
    WorkflowAssignmentRequest.ROLE_ATTORNEY: "Responsible attorney",
    WorkflowAssignmentRequest.ROLE_MANAGER: "Manager",
}
_MISSING = object()


class AssignmentRequestError(Exception):
    pass


class AssignmentRequestForbidden(AssignmentRequestError):
    pass


class AssignmentRequestInvalidAction(AssignmentRequestError):
    pass


@dataclass(frozen=True)
class AssignmentResponseResult:
    request: WorkflowAssignmentRequest
    workflow_changed: bool = False
    already_final: bool = False


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def role_label(role_code: str | None) -> str:
    return ROLE_LABELS.get(str(role_code or "").strip(), str(role_code or "").strip() or "-")


def assignment_field_for_role(role_code: str) -> str:
    role = str(role_code or "").strip()
    field = ROLE_FIELDS.get(role)
    if not field:
        raise ValueError(f"unsupported workflow assignment role: {role_code!r}")
    return field


def workflow_assignment_state(workflow: Workflow | None) -> dict[str, int | None]:
    return {
        role_code: _coerce_int(getattr(workflow, field, None)) if workflow is not None else None
        for role_code, field in ROLE_FIELDS.items()
    }


def _normalize_before_state(before: object) -> dict[str, int | None]:
    if isinstance(before, dict):
        return {
            WorkflowAssignmentRequest.ROLE_HANDLER: _coerce_int(
                before.get(WorkflowAssignmentRequest.ROLE_HANDLER, before.get("assignee_id"))
            ),
            WorkflowAssignmentRequest.ROLE_ATTORNEY: _coerce_int(
                before.get(
                    WorkflowAssignmentRequest.ROLE_ATTORNEY,
                    before.get("attorney_assignee_id"),
                )
            ),
            WorkflowAssignmentRequest.ROLE_MANAGER: _coerce_int(
                before.get(WorkflowAssignmentRequest.ROLE_MANAGER, before.get("inspector_id"))
            ),
        }
    if isinstance(before, (tuple, list)):
        values = list(before)
        return {
            WorkflowAssignmentRequest.ROLE_HANDLER: _coerce_int(
                values[0] if len(values) > 0 else None
            ),
            WorkflowAssignmentRequest.ROLE_ATTORNEY: _coerce_int(
                values[1] if len(values) > 1 else None
            ),
            WorkflowAssignmentRequest.ROLE_MANAGER: _coerce_int(
                values[2] if len(values) > 2 else None
            ),
        }
    return {
        WorkflowAssignmentRequest.ROLE_HANDLER: None,
        WorkflowAssignmentRequest.ROLE_ATTORNEY: None,
        WorkflowAssignmentRequest.ROLE_MANAGER: None,
    }


def _pending_for_role(workflow_id: int, role_code: str) -> list[WorkflowAssignmentRequest]:
    return (
        WorkflowAssignmentRequest.query.filter_by(
            workflow_id=int(workflow_id),
            role_code=role_code,
            status=WorkflowAssignmentRequest.STATUS_PENDING,
        )
        .order_by(WorkflowAssignmentRequest.requested_at.asc(), WorkflowAssignmentRequest.id.asc())
        .all()
    )


def cancel_pending_assignment_requests(
    *,
    workflow_id: int,
    role_code: str,
    reason: str = "cancelled",
) -> list[WorkflowAssignmentRequest]:
    now = _now()
    cancelled: list[WorkflowAssignmentRequest] = []
    for row in _pending_for_role(int(workflow_id), role_code):
        row.status = WorkflowAssignmentRequest.STATUS_CANCELLED
        row.responded_at = now
        row.response_note = reason
        db.session.add(row)
        cancelled.append(row)
    if cancelled:
        db.session.flush()
    return cancelled


def request_assignment_confirmation(
    workflow: Workflow,
    role_code: str,
    target_user_id: int | None,
    requested_by_id: int | None,
    source: str | None,
    *,
    previous_user_id: int | None | object = _MISSING,
) -> WorkflowAssignmentRequest | None:
    role = str(role_code or "").strip()
    field = assignment_field_for_role(role)
    workflow_id = _coerce_int(getattr(workflow, "id", None))
    if workflow_id is None:
        raise ValueError("workflow must be flushed before assignment confirmation is requested")

    target_id = _coerce_int(target_user_id)
    requester_id = _coerce_int(requested_by_id)
    previous_id = _coerce_int(previous_user_id)
    if previous_user_id is _MISSING:
        previous_id = _coerce_int(getattr(workflow, field, None))

    cancel_pending_assignment_requests(
        workflow_id=workflow_id,
        role_code=role,
        reason="reassigned",
    )

    if target_id is None:
        return None

    now = _now()
    is_self_assignment = requester_id is not None and target_id == requester_id
    request_status = WorkflowAssignmentRequest.STATUS_PENDING
    responded_at = None
    response_note = None
    workflow_status = str(getattr(workflow, "status", "") or "").strip()
    if workflow_status == Workflow.STATUS_COMPLETED:
        request_status = WorkflowAssignmentRequest.STATUS_ACCEPTED
        responded_at = now
        response_note = "workflow-completed"
    elif workflow_status == Workflow.STATUS_ABANDONED:
        request_status = WorkflowAssignmentRequest.STATUS_CANCELLED
        responded_at = now
        response_note = "workflow-abandoned"
    elif is_self_assignment:
        request_status = WorkflowAssignmentRequest.STATUS_ACCEPTED
        responded_at = now
        response_note = "self-assignment"

    req = WorkflowAssignmentRequest(
        workflow_id=workflow_id,
        role_code=role,
        previous_user_id=previous_id,
        target_user_id=target_id,
        requested_by_id=requester_id,
        status=request_status,
        source=(str(source or "").strip() or None),
        requested_at=now,
        responded_at=responded_at,
        response_note=response_note,
    )
    db.session.add(req)
    db.session.flush()
    return req


def sync_assignment_requests_for_changed_roles(
    workflow: Workflow,
    before: object,
    requested_by_id: int | None,
    source: str | None,
) -> list[WorkflowAssignmentRequest]:
    before_state = _normalize_before_state(before)
    after_state = workflow_assignment_state(workflow)
    requests: list[WorkflowAssignmentRequest] = []
    for role_code in ROLE_FIELDS:
        old_user_id = before_state.get(role_code)
        new_user_id = after_state.get(role_code)
        if old_user_id == new_user_id:
            continue
        req = request_assignment_confirmation(
            workflow,
            role_code,
            new_user_id,
            requested_by_id,
            source,
            previous_user_id=old_user_id,
        )
        if req is not None:
            requests.append(req)
    return requests


def _set_workflow_category_after_assignment_change(workflow: Workflow) -> None:
    workflow.category = derive_workflow_category(
        case_id=str(getattr(workflow, "case_id", "") or ""),
        handler_id=getattr(workflow, "assignee_id", None),
        attorney_id=getattr(workflow, "attorney_assignee_id", None),
        manager_id=getattr(workflow, "inspector_id", None),
        hint_category=getattr(workflow, "category", None),
        hint_name_ref=getattr(workflow, "business_code", None),
        hint_name_free=getattr(workflow, "name", None),
    )


def _locked_assignment_request(request_id: int) -> WorkflowAssignmentRequest | None:
    return (
        WorkflowAssignmentRequest.query.filter(WorkflowAssignmentRequest.id == int(request_id))
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )


def _actor_can_respond(
    req: WorkflowAssignmentRequest,
    *,
    actor_id: int | None,
    action: str,
    allow_privileged: bool,
) -> bool:
    actor = _coerce_int(actor_id)
    if actor is None:
        return False
    if actor == int(req.target_user_id):
        return True
    if not allow_privileged:
        return False

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"accept", "accepted"}:
        return False

    workflow = req.workflow
    matter_id = str(getattr(workflow, "case_id", "") or "").strip() if workflow is not None else ""
    if not matter_id:
        return False

    user = db.session.get(User, actor)
    if user is None or getattr(user, "is_active", True) is False:
        return False

    try:
        from app.utils.permissions import can_access_matter

        return bool(can_access_matter(user, matter_id, action="assign_staff"))
    except Exception:
        return False


def respond_assignment_request(
    request_id: int,
    actor_id: int | None,
    action: str,
    reason: str | None = None,
    *,
    allow_privileged: bool = False,
) -> AssignmentResponseResult:
    req = _locked_assignment_request(int(request_id))
    if req is None:
        raise LookupError("assignment_request_not_found")

    normalized_action = str(action or "").strip().lower()
    actor = _coerce_int(actor_id)
    if not _actor_can_respond(
        req,
        actor_id=actor,
        action=normalized_action,
        allow_privileged=allow_privileged,
    ):
        raise AssignmentRequestForbidden("only_target_user_can_respond")

    if req.status != WorkflowAssignmentRequest.STATUS_PENDING:
        return AssignmentResponseResult(request=req, already_final=True)

    now = _now()
    workflow_changed = False

    if normalized_action in {"accept", "accepted"}:
        req.status = WorkflowAssignmentRequest.STATUS_ACCEPTED
        req.responded_at = now
        req.response_note = str(reason or "").strip() or None
        db.session.add(req)
        return AssignmentResponseResult(request=req, workflow_changed=False)

    if normalized_action not in {"reject", "rejected"}:
        raise AssignmentRequestInvalidAction("invalid_assignment_response_action")

    req.status = WorkflowAssignmentRequest.STATUS_REJECTED
    req.responded_at = now
    req.response_note = str(reason or "").strip() or None

    workflow = req.workflow
    if workflow is not None:
        field = assignment_field_for_role(req.role_code)
        current_value = _coerce_int(getattr(workflow, field, None))
        target_value = _coerce_int(req.target_user_id)
        previous_value = _coerce_int(req.previous_user_id)
        if current_value == target_value:
            setattr(workflow, field, previous_value)
            _set_workflow_category_after_assignment_change(workflow)
            linked_di = linked_docket_item_for_workflow(workflow)
            persist_manual_workflow_assignment_override(
                workflow=workflow,
                docket_item=linked_di,
                actor_id=actor,
            )
            db.session.add(workflow)
            workflow_changed = True

    db.session.add(req)
    return AssignmentResponseResult(request=req, workflow_changed=workflow_changed)


def _user_display_name(user: User | None) -> str:
    if user is None:
        return "-"
    return (
        str(getattr(user, "display_name", None) or "").strip()
        or str(getattr(user, "username", None) or "").strip()
        or str(getattr(user, "email", None) or "").strip()
        or f"User #{getattr(user, 'id', '')}"
    )


def serialize_assignment_request(
    req: WorkflowAssignmentRequest,
    *,
    current_user_id: int | None = None,
) -> dict[str, Any]:
    workflow = req.workflow
    matter = getattr(workflow, "matter", None) if workflow is not None else None
    workflow_id = int(getattr(req, "workflow_id", 0) or 0)
    matter_id = str(getattr(workflow, "case_id", "") or "").strip() if workflow is not None else ""
    current_uid = _coerce_int(current_user_id)
    return {
        "id": int(req.id),
        "workflow_id": workflow_id,
        "workflow_name": str(getattr(workflow, "name", "") or "").strip()
        or f"Workflow #{workflow_id}",
        "case_id": matter_id,
        "our_ref": str(getattr(matter, "our_ref", "") or "").strip(),
        "role_code": req.role_code,
        "role_label": role_label(req.role_code),
        "previous_user_id": req.previous_user_id,
        "previous_user_name": _user_display_name(req.previous_user),
        "target_user_id": req.target_user_id,
        "target_user_name": _user_display_name(req.target_user),
        "requested_by_id": req.requested_by_id,
        "requested_by_name": _user_display_name(req.requested_by),
        "status": req.status,
        "source": req.source,
        "requested_at": req.requested_at.isoformat() if req.requested_at else None,
        "responded_at": req.responded_at.isoformat() if req.responded_at else None,
        "response_note": req.response_note,
        "due_date": (
            getattr(workflow, "due_date", None).isoformat()
            if workflow is not None and getattr(workflow, "due_date", None)
            else None
        ),
        "can_respond": (
            req.status == WorkflowAssignmentRequest.STATUS_PENDING
            and current_uid is not None
            and int(req.target_user_id) == current_uid
        ),
        "workflow_url": f"/workflow/{workflow_id}" if workflow_id else "",
        "case_url": f"/case/{matter_id}" if matter_id else "",
    }


def pending_assignment_request_badges_by_workflow(
    workflow_ids: list[int] | set[int],
    *,
    current_user_id: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    ids = sorted(
        {int(value) for value in (_coerce_int(v) for v in workflow_ids) if value is not None}
    )
    if not ids:
        return {}
    current_uid = _coerce_int(current_user_id)
    rows = (
        WorkflowAssignmentRequest.query.filter(
            WorkflowAssignmentRequest.workflow_id.in_(ids),
            WorkflowAssignmentRequest.status == WorkflowAssignmentRequest.STATUS_PENDING,
        )
        .order_by(WorkflowAssignmentRequest.requested_at.desc())
        .all()
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        wf_id = int(row.workflow_id)
        out.setdefault(wf_id, []).append(
            {
                "id": int(row.id),
                "role_code": row.role_code,
                "role_label": role_label(row.role_code),
                "target_user_id": int(row.target_user_id),
                "target_user_name": _user_display_name(row.target_user),
                "is_for_current_user": (
                    current_uid is not None and int(row.target_user_id) == current_uid
                ),
                "status": row.status,
            }
        )
    return out
