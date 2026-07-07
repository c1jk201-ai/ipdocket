from __future__ import annotations

import json
from datetime import datetime

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.blueprints.workflow import bp
from app.extensions import db
from app.models.permissions import Permissions
from app.models.ip_records import DocketItem, Matter, VMatterOverview
from app.models.user import User
from app.models.workflow import Workflow
from app.models.workflow_playbook import WorkflowPlaybookTemplate
from app.services.audit.entity_audit import (
    audit_json_value,
    diff_snapshots,
    load_audit_rows_for_target,
    record_entity_audit,
)
from app.services.case.cascade_delete_service import (
    delete_workflow_fk_children,
)
from app.services.core.staff_options import build_staff_assignment_lists
from app.services.deletion_manager import DeletionService
from app.services.workflow.assignment_requests import (
    sync_assignment_requests_for_changed_roles,
    workflow_assignment_state,
)
from app.services.workflow.playbook_service import (
    apply_template_to_workflow,
    create_or_update_template,
    list_templates,
    recommend_templates_for_workflow,
    template_to_checklist_text,
)
from app.services.workflow.status_sync import (
    linked_docket_item_for_workflow,
    workflow_display_values,
)
from app.services.workflow.status_transition import apply_workflow_status_transition
from app.services.workflow.sync_requests import enqueue_workflow_sync, enqueue_workflow_task_sync
from app.services.workflow.task_sync import (
    _current_staff_snapshot,
    _resolve_primary_staff_party_id_from_matter,
    persist_manual_workflow_assignment_override,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import (
    PERM_CASE_ASSIGN_ALL,
    PERM_CASE_ASSIGN_TEAM,
    can_access_matter,
    permission_required,
    require_matter_access,
)
from app.utils.policy_sql import policy_text as text
from app.utils.tc import apply_tc_scope_filter, normalize_tc_scope, parse_finite_float
from app.utils.task_distribution_rules import DistributionDecision, resolve_distribution_decision
from app.utils.timezone import today_local
from app.utils.url_helpers import safe_next_url
from app.utils.workflow_deadline_labels import workflow_deadline_label, workflow_deadline_title
from app.utils.workflow_semantics import (
    derive_workflow_category,
    normalize_workflow_category,
    workflow_primary_owner_user_id,
)

_AUTO_DOCKET_NOTE_MARKER = "Auto Create: DocketItem "
_WORKFLOW_AUDIT_FIELDS = (
    "case_id",
    "name",
    "status",
    "category",
    "priority",
    "business_code",
    "request_start_date",
    "legal_due_date",
    "source_docket_due_date",
    "source_docket_legal_due_date",
    "due_date",
    "draft_due_date",
    "draft_due_date2",
    "submit_due_date",
    "draft_sent_date",
    "submit_date",
    "completed_date",
    "difficulty",
    "page_count",
    "work_hours",
    "assignee_id",
    "attorney_assignee_id",
    "inspector_id",
    "created_by_id",
    "completed_by_id",
    "note",
    "send_memo",
)


def _workflow_audit_snapshot(wf: Workflow) -> dict[str, object]:
    return {field: audit_json_value(getattr(wf, field, None)) for field in _WORKFLOW_AUDIT_FIELDS}


def _record_workflow_audit(
    *,
    action: str,
    wf: Workflow,
    changes: dict[str, dict[str, object]] | None = None,
    snapshot: dict[str, object] | None = None,
    linked_docket_id: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    meta = {
        "workflow_id": getattr(wf, "id", None),
        "matter_id": getattr(wf, "case_id", None),
        "title": getattr(wf, "name", None),
    }
    if changes:
        meta["changes"] = changes
    if snapshot:
        meta["snapshot"] = snapshot
    if linked_docket_id:
        meta["linked_docket_id"] = linked_docket_id
    if extra:
        meta.update(extra)
    record_entity_audit(
        action=action,
        target_type="workflow",
        target_id=int(getattr(wf, "id", 0) or 0) or None,
        actor_id=getattr(current_user, "id", None),
        meta=meta,
    )


def _record_workflow_parse_failure(
    *,
    kind: str,
    raw_value: object,
    error: str,
    source: str,
) -> None:
    try:
        from app.services.automation.parse_failure import record_parse_failure
    except ImportError as exc:
        report_swallowed_exception(
            exc,
            context=f"{source}.import_record_parse_failure",
            log_key=f"{source}.import_record_parse_failure",
            log_window_seconds=300,
        )
        return

    try:
        record_parse_failure(kind=kind, raw_value=raw_value, error=error, source=source)
    except (RuntimeError, SQLAlchemyError) as exc:
        report_swallowed_exception(
            exc,
            context=f"{source}.record_parse_failure",
            log_key=f"{source}.record_parse_failure",
            log_window_seconds=300,
        )


def _dedupe_staff_rows(values: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen_keys: set[str] = set()
    for row in values or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        rname = str(row.get("name") or "").strip()
        if not rid and not rname:
            continue
        key = rid or rname
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append({"id": rid or None, "name": rname})
    return rows


def _merge_staff_rows(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for group in groups:
        for row in group or []:
            if not isinstance(row, dict):
                continue
            merged.append(
                {
                    "id": (str(row.get("id") or "").strip() or None),
                    "name": str(row.get("name") or "").strip(),
                }
            )
    return _dedupe_staff_rows(merged)


def _user_to_staff_row(user) -> dict | None:
    if not user:
        return None
    row_id = (
        str(getattr(user, "staff_party_id", None) or "").strip()
        or str(getattr(user, "id", None) or "").strip()
    )
    if not row_id:
        return None
    row_name = (
        str(getattr(user, "display_name", None) or "").strip()
        or str(getattr(user, "username", None) or "").strip()
        or row_id
    )
    return {"id": row_id, "name": row_name}


def _extract_task_source_from_docket_item(docket_item) -> str | None:
    if not docket_item:
        return None
    raw_source = (getattr(docket_item, "source", None) or "").strip()
    if raw_source:
        return raw_source.lower()

    memo = (getattr(docket_item, "memo", None) or "").strip()
    payload: dict[str, object] = {}
    if memo:
        try:
            parsed = json.loads(memo)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
            src = str(payload.get("source") or "").strip()
            if src:
                return src.lower()
            if isinstance(payload.get("events"), list):
                return "upload_automation"
            trigger = str(payload.get("trigger") or "").strip()
            if trigger in {"REGISTRATION_CERTIFICATE", "Notice of allowance", "Final rejection"}:
                return "uspto_notice"

    category = (getattr(docket_item, "category", None) or "").strip().upper()
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    if name_ref.startswith("USPTO:"):
        return "uspto_notice"
    if name_ref.startswith("USPTO_OA:"):
        return "upload_automation"
    if category == "USPTO_OA":
        return "upload_automation"
    return None


def _fetch_case_staff_rows(case_id: str | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"attorney": [], "handler": [], "manager": []}
    mid = (case_id or "").strip()
    if not mid:
        return out
    try:
        rows = db.session.execute(
            text(
                """
                    SELECT msa.staff_role_code, msa.staff_party_id, p.name_display
                    FROM matter_staff_assignment msa
                    JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                    JOIN party p ON p.party_id = ps.party_id
                    WHERE msa.matter_id = :mid
                      AND LOWER(TRIM(msa.staff_role_code)) IN (
                        'attorney', 'retainer',
                        'handler', 'staff', 'draftsman',
                        'manager', 'mgmt'
                      )
                    ORDER BY msa.seq ASC, msa.msa_id ASC
                    """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).fetchall()
        for role, spid, name in rows:
            rid = str(spid or "").strip() or None
            rname = str(name or "").strip()
            if not rid or not rname:
                continue
            role_norm = str(role or "").strip().lower()
            staff_row = {"id": rid, "name": rname}
            if role_norm in ("attorney", "retainer"):
                out["attorney"] = _merge_staff_rows(out["attorney"], [staff_row])
            elif role_norm in ("handler", "staff", "draftsman"):
                out["handler"] = _merge_staff_rows(out["handler"], [staff_row])
            elif role_norm in ("manager", "mgmt"):
                out["manager"] = _merge_staff_rows(out["manager"], [staff_row])
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="workflow.routes._fetch_case_staff_rows",
            log_key="workflow.routes._fetch_case_staff_rows",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except SQLAlchemyError as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="workflow.routes._fetch_case_staff_rows.rollback",
                log_key="workflow.routes._fetch_case_staff_rows.rollback",
                log_window_seconds=300,
            )
    return out


def _normalize_distribution_role(role_code: str | None) -> str | None:
    role = (role_code or "").strip().lower()
    if role in ("manager", "mgmt"):
        return "manager"
    if role in ("attorney", "retainer"):
        return "attorney"
    if role in ("handler", "staff", "draftsman"):
        return "handler"
    if role in ("owner", "fallback"):
        return "owner"
    return None


def _user_is_manager_like(user) -> bool:
    if user is None:
        return False
    try:
        checker = getattr(user, "is_mgmt_role", None)
        if callable(checker):
            return bool(checker())
    except (AttributeError, SQLAlchemyError, TypeError) as exc:
        report_swallowed_exception(
            exc,
            context="workflow.routes._user_is_manager_like",
            log_key="workflow.routes._user_is_manager_like",
            log_window_seconds=300,
        )
    role = str(getattr(user, "role", "") or "").strip().lower()
    return role in {"manager", "mgmt_staff", "mgmt_director"}


def _decision_is_manager_only(decision: DistributionDecision | None) -> bool:
    if not decision or decision.distribute_to != "role_set":
        return False
    roles = {
        normalized
        for normalized in (_normalize_distribution_role(r) for r in (decision.role_codes or ()))
        if normalized
    }
    if not roles:
        return True
    return roles.issubset({"manager"})


def _display_user_name(user) -> str:
    if not user:
        return ""
    return (
        str(getattr(user, "display_name", None) or "").strip()
        or str(getattr(user, "username", None) or "").strip()
        or str(getattr(user, "email", None) or "").strip()
        or f"User #{getattr(user, 'id', '-')}"
    )


def _build_workflow_assignment_rows(wf: Workflow) -> list[dict[str, object]]:
    assignments: list[dict[str, object]] = []
    for role_code, label, short_label, user in (
        ("handler", "Handler", "Process", getattr(wf, "assignee", None)),
        ("attorney", "Responsible attorney", "", getattr(wf, "attorney_assignee", None)),
        ("manager", "Manager", "", getattr(wf, "inspector", None)),
    ):
        staff_row = _user_to_staff_row(user)
        assignments.append(
            {
                "role_code": role_code,
                "label": label,
                "short_label": short_label,
                "id": staff_row.get("id") if staff_row else None,
                "name": _display_user_name(user),
                "user_id": getattr(user, "id", None) if user is not None else None,
            }
        )
    return assignments


def _build_workflow_deadline_summary(
    wf: Workflow,
    *,
    linked_docket_item: DocketItem | None = None,
) -> dict[str, object]:
    today = today_local()
    display = workflow_display_values(wf, linked_docket_item=linked_docket_item)
    legal_due = display.get("legal_due_date")
    due_date = display.get("due_date")
    reference_date = due_date or legal_due
    reference_label = (
        "Internal Due date" if due_date and legal_due and due_date != legal_due else "Final Due date"
    )
    status = str(getattr(wf, "status", "") or "").strip().upper()
    completed_date = getattr(wf, "completed_date", None)

    if completed_date:
        summary = {
            "tone": "success" if status == "COMPLETED" else "secondary",
            "label": "Done" if status == "COMPLETED" else "",
            "detail": f" {completed_date.isoformat()}",
            "reference_date": reference_date,
            "reference_label": reference_label,
        }
        if reference_date:
            delta = (reference_date - completed_date).days
            if delta > 0:
                summary["label"] = f"{delta} "
                summary["detail"] = f"{reference_label} {delta}  Process"
                summary["tone"] = "success"
            elif delta == 0:
                summary["label"] = "Deadline  Process"
                summary["detail"] = f"{reference_label}  "
                summary["tone"] = "primary"
            else:
                delay_days = abs(delta)
                summary["label"] = f"{delay_days}  "
                summary["detail"] = f"{reference_label} {delay_days}  "
                summary["tone"] = "danger"
        return summary

    if reference_date:
        delta = (reference_date - today).days
        if delta > 0:
            tone = "warning" if delta <= 3 else "primary"
            label = f"D-{delta}"
            detail = f"{reference_label} {delta} "
        elif delta == 0:
            tone = "danger"
            label = "D-Day"
            detail = f"{reference_label} "
        else:
            delay_days = abs(delta)
            tone = "danger"
            label = f"{delay_days} "
            detail = f"{reference_label} {delay_days} "
        return {
            "tone": tone,
            "label": label,
            "detail": detail,
            "reference_date": reference_date,
            "reference_label": reference_label,
        }

    return {
        "tone": "secondary",
        "label": "Deadline Unspecified",
        "detail": "Internal Due date  Final Due date none.",
        "reference_date": None,
        "reference_label": None,
    }


def _resolve_flow_owner_rows_for_workflow(
    *,
    wf: Workflow,
    linked_docket_item,
    attorneys_list: list[dict],
    handlers_list: list[dict],
    managers_list: list[dict],
) -> tuple[list[dict], DistributionDecision]:
    category = (
        getattr(linked_docket_item, "category", None)
        if linked_docket_item is not None
        else getattr(wf, "category", None)
    )
    name_ref = (
        getattr(linked_docket_item, "name_ref", None) if linked_docket_item is not None else None
    )
    name_free = (
        getattr(linked_docket_item, "name_free", None)
        if linked_docket_item is not None
        else getattr(wf, "name", None)
    )
    source = _extract_task_source_from_docket_item(linked_docket_item)
    decision = resolve_distribution_decision(
        category=category,
        name_ref=name_ref,
        name_free=name_free,
        source=source,
    )

    owner_user = (
        getattr(wf, "assignee", None)
        or getattr(wf, "attorney_assignee", None)
        or getattr(wf, "inspector", None)
    )
    owner_row = _user_to_staff_row(owner_user)
    handler_row = _user_to_staff_row(getattr(wf, "assignee", None))
    attorney_row = _user_to_staff_row(getattr(wf, "attorney_assignee", None))
    manager_row = _user_to_staff_row(getattr(wf, "inspector", None))

    owners_by_role: dict[str, list[dict]] = {
        "owner": _merge_staff_rows([owner_row] if owner_row else []),
        "handler": _merge_staff_rows(handlers_list, [handler_row] if handler_row else []),
        "attorney": _merge_staff_rows(attorneys_list, [attorney_row] if attorney_row else []),
        "manager": _merge_staff_rows(managers_list, [manager_row] if manager_row else []),
    }

    if decision.distribute_to == "all_staff":
        rows = _merge_staff_rows(
            owners_by_role["owner"],
            owners_by_role["handler"],
            owners_by_role["attorney"],
            owners_by_role["manager"],
        )
        return rows, decision

    if decision.distribute_to == "role_set":
        target_roles = {
            normalized
            for normalized in (_normalize_distribution_role(r) for r in decision.role_codes)
            if normalized
        }
        if not target_roles:
            target_roles = {"manager"}
        rows: list[dict] = []
        for role_key in ("owner", "handler", "attorney", "manager"):
            if role_key in target_roles:
                role_rows = owners_by_role.get(role_key, [])
                if not role_rows and role_key in {"handler", "attorney"}:
                    role_rows = owners_by_role.get("owner", [])
                if not role_rows and role_key == "manager" and _user_is_manager_like(owner_user):
                    role_rows = owners_by_role.get("owner", [])
                rows = _merge_staff_rows(rows, role_rows)
        return rows, decision

    if decision.distribute_to == "none":
        return [], decision

    # owner(default): keep owner semantics as a single task owner if possible.
    rows = owners_by_role["owner"]
    if not rows:
        rows = owners_by_role["handler"]
    if not rows:
        rows = owners_by_role["attorney"]
    if not rows:
        rows = owners_by_role["manager"]
    return rows, decision


def _ensure_auto_docket_note_marker(wf: Workflow) -> None:
    """Preserve the auto-generated marker for DOCKET-backed workflows.

    The marker is used by Worklog merging/cleanup and should not be removable via UI note edits.
    """
    business_code = (getattr(wf, "business_code", None) or "").strip()
    if not business_code.upper().startswith("DOCKET:"):
        return
    note = (getattr(wf, "note", None) or "").strip()
    if _AUTO_DOCKET_NOTE_MARKER in note:
        return
    wf.note = f"{note} {_AUTO_DOCKET_NOTE_MARKER}".strip() if note else _AUTO_DOCKET_NOTE_MARKER


def _parse_date(value: str | None):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        _record_workflow_parse_failure(
            kind="date",
            raw_value=value,
            error=str(exc),
            source="workflow.routes._parse_date",
        )
        return None


def _workflow_internal_due_date_raw(form) -> str | None:
    raw = form.get("internal_due_date")
    if raw is not None:
        return raw
    return form.get("due_date")


def _uses_two_deadline_fields(form) -> bool:
    return form.get("internal_due_date") is not None


def _apply_workflow_due_dates(
    wf: Workflow,
    *,
    legal_due_date_raw: str | None,
    internal_due_date_raw: str | None,
    clear_legacy_deadlines: bool = False,
) -> None:
    previous_legal_due_date = getattr(wf, "legal_due_date", None)
    if legal_due_date_raw is not None:
        wf.legal_due_date = _parse_date(legal_due_date_raw)

    if internal_due_date_raw is not None:
        wf.due_date = _parse_date(internal_due_date_raw) or getattr(wf, "legal_due_date", None)
    elif legal_due_date_raw is not None and (
        getattr(wf, "due_date", None) is None
        or getattr(wf, "due_date", None) == previous_legal_due_date
    ):
        wf.due_date = getattr(wf, "legal_due_date", None)

    if clear_legacy_deadlines:
        wf.draft_due_date = None
        wf.draft_due_date2 = None
        wf.submit_due_date = None


def _parse_int(value: str | None):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError) as exc:
        _record_workflow_parse_failure(
            kind="int",
            raw_value=s,
            error=str(exc),
            source="workflow.routes._parse_int",
        )
        return None


def _parse_float(value: str | None):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    parsed = parse_finite_float(s)
    if parsed is None:
        _record_workflow_parse_failure(
            kind="float",
            raw_value=s,
            error="invalid or non-finite float",
            source="workflow.routes._parse_float",
        )
    return parsed


def _can_manage_others() -> bool:
    if not current_user.is_authenticated:
        return False
    try:
        if callable(getattr(current_user, "can_view_all_work_deadlines", None)):
            if current_user.can_view_all_work_deadlines():
                return True
        if callable(getattr(current_user, "can_view_all_mgmt_deadlines", None)):
            if current_user.can_view_all_mgmt_deadlines():
                return True
        if callable(getattr(current_user, "has_permission", None)):
            if current_user.has_permission(PERM_CASE_ASSIGN_ALL) or current_user.has_permission(
                PERM_CASE_ASSIGN_TEAM
            ):
                return True
    except (AttributeError, RuntimeError, SQLAlchemyError, TypeError):
        return False
    return False


def _workflow_delete_snapshot(wf: Workflow) -> dict[str, object]:
    return {
        "id": int(wf.id),
        "case_id": str(wf.case_id) if wf.case_id else None,
        "name": wf.name,
        "status": "Abandoned",
        "category": getattr(wf, "category", None),
        "assignee_id": getattr(wf, "assignee_id", None),
        "attorney_assignee_id": getattr(wf, "attorney_assignee_id", None),
        "inspector_id": getattr(wf, "inspector_id", None),
    }


def _parse_user_id(value: str | None) -> int | None:
    parsed = _parse_int(value)
    if parsed is None:
        return None
    if parsed <= 0:
        return None
    return parsed


def _resolve_case_role_user_ids(case_id: str | None) -> dict[str, int | None]:
    out: dict[str, int | None] = {"manager": None, "attorney": None, "handler": None}
    mid = (case_id or "").strip()
    if not mid:
        return out

    try:
        rows = db.session.execute(
            text(
                """
                SELECT LOWER(TRIM(msa.staff_role_code)) AS role_code, u.id AS user_id, msa.msa_id
                FROM matter_staff_assignment msa
                JOIN users u ON u.staff_party_id = msa.staff_party_id
                WHERE msa.matter_id = :mid
                  AND LOWER(TRIM(msa.staff_role_code)) IN (
                    'manager','mgmt','attorney','retainer','handler','staff','draftsman'
                  )
                  AND COALESCE(u.is_active, FALSE) = TRUE
                ORDER BY msa.msa_id ASC, u.id ASC
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()

        for role_code, user_id, _ in rows:
            role = (role_code or "").strip().lower()
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            if role in {"manager", "mgmt"} and out["manager"] is None:
                out["manager"] = uid
                continue
            if role in {"attorney", "retainer"} and out["attorney"] is None:
                out["attorney"] = uid
                continue
            if role in {"handler", "staff", "draftsman"} and out["handler"] is None:
                out["handler"] = uid
                continue
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context=f"workflow.routes._resolve_case_role_user_ids(case_id={mid})",
            log_key="workflow.routes._resolve_case_role_user_ids",
            log_window_seconds=300,
        )
    return out


def _merge_workflow_assignees_with_case_defaults(
    *,
    case_id: str | None,
    handler_id: int | None,
    attorney_id: int | None,
    manager_id: int | None,
    fallback_handler_id: int | None = None,
) -> tuple[int | None, int | None, int | None]:
    defaults = _resolve_case_role_user_ids(case_id)
    if handler_id is None and attorney_id is None and manager_id is None:
        return (
            defaults.get("handler") or fallback_handler_id,
            defaults.get("attorney"),
            defaults.get("manager"),
        )

    has_other_roles = attorney_id is not None or manager_id is not None
    # Allow 2-person assignment (attorney + manager) without forcing a handler.
    if handler_id is None and has_other_roles:
        resolved_handler = None
    else:
        resolved_handler = handler_id or defaults.get("handler") or fallback_handler_id
    # Respect explicit blanks once at least one role was provided by the caller.
    return resolved_handler, attorney_id, manager_id


def _normalize_manual_workflow_category(category: str | None) -> str | None:
    return normalize_workflow_category(category)


def _should_use_manual_workflow_category(form) -> bool:
    raw_flag = form.get("category_manual")
    if raw_flag is None:
        return form.get("category") is not None
    return str(raw_flag or "").strip().lower() in {"1", "true", "yes", "on"}


def _requested_manual_workflow_category(form) -> str | None:
    if not _should_use_manual_workflow_category(form):
        return None
    return form.get("category")


def _derive_workflow_category(
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
    return derive_workflow_category(
        case_id=case_id,
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
        manual_category=manual_category,
        hint_category=hint_category,
        hint_name_ref=hint_name_ref,
        hint_name_free=hint_name_free,
        source=source,
    )


def _workflow_assignee_filter_for_user(user_id: int):
    return or_(
        Workflow.assignee_id == user_id,
        Workflow.attorney_assignee_id == user_id,
        Workflow.inspector_id == user_id,
    )


def _workflow_editors(wf: Workflow) -> set[int]:
    ids: set[int] = set()
    for raw in (
        getattr(wf, "assignee_id", None),
        getattr(wf, "attorney_assignee_id", None),
        getattr(wf, "inspector_id", None),
        getattr(wf, "created_by_id", None),
    ):
        try:
            uid = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            uid = None
        if uid and uid > 0:
            ids.add(uid)
    return ids


def _workflow_has_user(wf: Workflow, user_id: int | None) -> bool:
    if not user_id:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in set(getattr(wf, "assigned_user_ids", []) or [])


def _tc_scope_value(*, from_form: bool = False) -> str:
    default_scope = (
        (current_app.config.get("STATS_TC_SCOPE_DEFAULT") or "candidate").strip().lower()
    )
    raw = request.form.get("tc_scope") if from_form else request.args.get("tc_scope")
    return normalize_tc_scope(raw, default=default_scope)


def _apply_tc_scope_filter(q, scope: str):
    return apply_tc_scope_filter(q, scope)


@bp.route("/list")
@login_required
def list_tasks():
    # Deadline view logic
    q = db.session.query(Workflow).join(Matter, Workflow.case_id == Matter.matter_id)
    # Exclude annuity (Renewal) workflows: they are handled in the annuity module.
    q = q.filter(or_(Workflow.business_code.is_(None), Workflow.business_code.notlike("ANNUITY:%")))

    if current_user.is_authenticated and not _can_manage_others():
        q = q.filter(_workflow_assignee_filter_for_user(current_user.id))

    q = q.filter(Workflow.status.notin_(["Completed", "Abandoned"]))

    def _parse_int(name: str, default: int) -> int:
        try:
            return int(str(request.args.get(name) or "").strip() or default)
        except (TypeError, ValueError):
            return default

    per_page_options = [20, 50, 100, 200, 500]
    per_page = _parse_int("per_page", 50)
    if per_page not in per_page_options:
        per_page = 50
    page = max(1, _parse_int("page", 1))

    total = q.with_entities(func.count()).scalar() or 0
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > pages:
        page = pages

    base_args = {k: v for k, v in request.args.items() if v is not None}
    base_args["per_page"] = per_page
    prev_url = url_for(request.endpoint, **{**base_args, "page": page - 1}) if page > 1 else None
    next_url = (
        url_for(request.endpoint, **{**base_args, "page": page + 1}) if page < pages else None
    )

    workflows = (
        q.order_by(Matter.our_ref.desc(), Workflow.due_date.asc(), Workflow.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
        .all()
    )
    return render_template(
        "workflow/list.html",
        workflows=workflows,
        page=page,
        pages=pages,
        per_page=per_page,
        per_page_options=per_page_options,
        total=total,
        prev_url=prev_url,
        next_url=next_url,
    )


@bp.route("/<int:workflow_id>")
@login_required
def detail(workflow_id: int):
    wf = Workflow.query.get_or_404(workflow_id)
    require_matter_access(str(wf.case_id), action="view")
    matter = Matter.query.get(str(wf.case_id)) if wf.case_id else None
    matter_overview = VMatterOverview.query.get(str(wf.case_id)) if wf.case_id else None
    matter_display = matter_overview or matter
    can_edit_workflow = False
    can_assign_staff = False
    if wf.case_id:
        try:
            can_edit_workflow = can_access_matter(
                current_user,
                str(wf.case_id),
                action="edit_case",
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.detail.can_access_matter(edit_case)",
                log_key="workflow.routes.detail.can_access_matter(edit_case)",
                log_window_seconds=300,
            )
        try:
            can_assign_staff = can_access_matter(
                current_user,
                str(wf.case_id),
                action="assign_staff",
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.detail.can_access_matter(assign_staff)",
                log_key="workflow.routes.detail.can_access_matter(assign_staff)",
                log_window_seconds=300,
            )
    staff_users_all = []
    if can_assign_staff:
        try:
            staff_users_all = list(build_staff_assignment_lists().get("all_users") or [])
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.detail.build_staff_assignment_lists",
                log_key="workflow.routes.detail.build_staff_assignment_lists",
                log_window_seconds=300,
            )
    linked_di = linked_docket_item_for_workflow(wf)
    linked_docket_id = getattr(linked_di, "docket_id", None) if linked_di is not None else None
    workflow_display = workflow_display_values(wf, linked_docket_item=linked_di)
    case_staff = _fetch_case_staff_rows(str(wf.case_id))
    flow_owners, decision = _resolve_flow_owner_rows_for_workflow(
        wf=wf,
        linked_docket_item=linked_di,
        attorneys_list=list(case_staff.get("attorney") or []),
        handlers_list=list(case_staff.get("handler") or []),
        managers_list=list(case_staff.get("manager") or []),
    )
    manager_only = _decision_is_manager_only(decision)
    workflow_assignments = _build_workflow_assignment_rows(wf)
    workflow_deadline_summary = _build_workflow_deadline_summary(
        wf,
        linked_docket_item=linked_di,
    )
    playbook_templates = []
    if can_edit_workflow:
        try:
            playbook_templates = recommend_templates_for_workflow(wf, matter=matter)
        except SQLAlchemyError as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.detail.recommend_playbooks",
                log_key="workflow.routes.detail.recommend_playbooks",
                log_window_seconds=300,
            )

    raw_category = (getattr(wf, "category", None) or "").strip()
    display_category = "MGMT" if manager_only else (raw_category or "-")
    workflow_audit_rows = load_audit_rows_for_target(
        target_type="workflow",
        target_id=int(wf.id),
        limit=12,
    )
    return render_template(
        "workflow/detail.html",
        wf=wf,
        matter=matter,
        matter_display=matter_display,
        display_category=display_category,
        flow_owners=flow_owners,
        suppress_work_roles=manager_only,
        case_attorneys=list(case_staff.get("attorney") or []),
        case_handlers=list(case_staff.get("handler") or []),
        case_managers=list(case_staff.get("manager") or []),
        workflow_assignments=workflow_assignments,
        workflow_deadline_summary=workflow_deadline_summary,
        workflow_display_name=workflow_display.get("name"),
        workflow_display_due_date=workflow_display.get("due_date"),
        workflow_display_legal_due_date=workflow_display.get("legal_due_date"),
        workflow_display_internal_due_date=workflow_display.get("internal_due_date"),
        can_edit_workflow=can_edit_workflow,
        can_assign_staff=can_assign_staff,
        linked_docket_id=linked_docket_id,
        staff_users_all=staff_users_all,
        playbook_templates=playbook_templates,
        workflow_audit_rows=workflow_audit_rows,
    )


@bp.route("/playbooks")
@login_required
@permission_required(Permissions.MENU_ADMIN)
def playbooks():
    q = (request.args.get("q") or "").strip()
    doc_type = (request.args.get("doc_type") or "").strip()
    active = (request.args.get("active") or "1").strip()
    rows = list_templates(
        active_only=active != "all",
        q=q,
        doc_type=doc_type,
        limit=500,
    )
    return render_template(
        "workflow/playbooks.html",
        rows=rows,
        q=q,
        doc_type=doc_type,
        active=active,
        template_to_checklist_text=template_to_checklist_text,
    )


@bp.route("/playbooks/new", methods=["GET", "POST"])
@login_required
@permission_required(Permissions.MENU_ADMIN)
def playbook_new():
    if request.method == "POST":
        if not (request.form.get("name") or "").strip():
            flash("Playbook Name Input.", "warning")
            return redirect(url_for("workflow.playbook_new"))
        try:
            row = create_or_update_template(
                template=None,
                form=request.form,
                actor_id=getattr(current_user, "id", None),
            )
            db.session.commit()
            flash("Playbook Template Create.", "success")
            return redirect(url_for("workflow.playbook_edit", template_id=row.id))
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.exception("Playbook create failed: %s", exc)
            flash("Playbook Save In Progress Error .", "danger")
    return render_template(
        "workflow/playbook_form.html",
        template=None,
        checklist_text="",
        schedule={},
    )


@bp.route("/playbooks/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required(Permissions.MENU_ADMIN)
def playbook_edit(template_id: int):
    template = WorkflowPlaybookTemplate.query.get_or_404(template_id)
    if request.method == "POST":
        if not (request.form.get("name") or "").strip():
            flash("Playbook Name Input.", "warning")
            return redirect(url_for("workflow.playbook_edit", template_id=template_id))
        try:
            create_or_update_template(
                template=template,
                form=request.form,
                actor_id=getattr(current_user, "id", None),
            )
            db.session.commit()
            flash("Playbook Template Save.", "success")
            return redirect(url_for("workflow.playbook_edit", template_id=template_id))
        except SQLAlchemyError as exc:
            db.session.rollback()
            current_app.logger.exception("Playbook update failed: %s", exc)
            flash("Playbook Save In Progress Error .", "danger")
    return render_template(
        "workflow/playbook_form.html",
        template=template,
        checklist_text=template_to_checklist_text(template),
        schedule=template.schedule_json or {},
    )


@bp.route("/playbooks/<int:template_id>/toggle", methods=["POST"])
@login_required
@permission_required(Permissions.MENU_ADMIN)
def playbook_toggle(template_id: int):
    template = WorkflowPlaybookTemplate.query.get_or_404(template_id)
    template.is_active = not bool(template.is_active)
    template.updated_by_id = getattr(current_user, "id", None)
    template.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Playbook Template Status Change.", "success")
    return redirect(request.referrer or url_for("workflow.playbooks"))


@bp.route("/<int:workflow_id>/apply-playbook", methods=["POST"])
@login_required
def apply_playbook(workflow_id: int):
    wf = Workflow.query.get_or_404(workflow_id)
    if wf.case_id:
        require_matter_access(str(wf.case_id), action="edit_case")
    template_id = _parse_int(request.form.get("template_id"))
    if not template_id:
        flash("Apply Playbook Select.", "warning")
        return redirect(url_for("workflow.detail", workflow_id=workflow_id))
    template = WorkflowPlaybookTemplate.query.get_or_404(template_id)
    if not template.is_active:
        flash("No active playbook is available to apply.", "warning")
        return redirect(url_for("workflow.detail", workflow_id=workflow_id))
    try:
        result = apply_template_to_workflow(
            template=template,
            workflow=wf,
            actor_id=getattr(current_user, "id", None),
        )
        db.session.commit()
        flash(
            f"Playbook applied: {result.checklist_created} checklist item(s), {result.fields_updated} field(s) updated.",
            "success",
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception("Playbook apply failed: %s", exc)
        flash("Playbook application failed.", "danger")
    return redirect(url_for("workflow.detail", workflow_id=workflow_id))


@bp.route("/create", methods=["POST"])
@login_required
def create():
    matter_id = (request.form.get("matter_id") or request.form.get("case_id") or "").strip()
    name = request.form.get("name")
    internal_due_date = _workflow_internal_due_date_raw(request.form)
    use_two_deadline_fields = _uses_two_deadline_fields(request.form)
    assignee_id_raw = request.form.get("assignee_id")
    attorney_assignee_id_raw = request.form.get("attorney_assignee_id")
    note = request.form.get("note")
    status = request.form.get("status")
    business_code = request.form.get("business_code")
    priority = request.form.get("priority")
    send_memo = request.form.get("send_memo")
    assignment_mode = (request.form.get("assignment_mode") or "").strip().lower()
    if not assignment_mode:
        # Backward compatibility: if UI/client omitted the mode, default to the safer self mode.
        assignment_mode = "self"
    inspector_id_raw = request.form.get("inspector_id")
    request_start_date = request.form.get("request_start_date")
    legal_due_date = request.form.get("legal_due_date")
    draft_due_date = request.form.get("draft_due_date")
    draft_due_date2 = request.form.get("draft_due_date2")
    submit_due_date = request.form.get("submit_due_date")
    difficulty_raw = request.form.get("difficulty")
    page_count_raw = request.form.get("page_count")
    work_hours_raw = request.form.get("work_hours")

    if not matter_id or not name:
        flash("Required item .")
        return redirect(url_for("case_work.case_list"))

    self_assignment_mode = assignment_mode in {"self", "self_only", "my", "mine"}
    if self_assignment_mode:
        matter_id_str = str(matter_id)
        has_edit_case = can_access_matter(current_user, matter_id_str, action="edit_case")
        has_assign_staff = can_access_matter(current_user, matter_id_str, action="assign_staff")
        if not (has_edit_case or has_assign_staff):
            # Reuse the existing authorization error flow.
            require_matter_access(matter_id_str, action="assign_staff")
    else:
        require_matter_access(str(matter_id), action="assign_staff")

    handler_assignee_id = _parse_user_id(assignee_id_raw)
    attorney_assignee_id = _parse_user_id(attorney_assignee_id_raw)
    manager_assignee_id = _parse_user_id(inspector_id_raw)
    if self_assignment_mode:
        current_user_id = _parse_user_id(str(getattr(current_user, "id", "") or ""))
        if current_user_id is None:
            flash(" TaskRegistration Required User  Confirm  none.", "warning")
            return redirect(url_for("case_work.case_detail", case_id=matter_id))
        handler_assignee_id = current_user_id
        attorney_assignee_id = None
        manager_assignee_id = None
    else:
        assignment_fields_present = any(
            value is not None
            for value in (assignee_id_raw, attorney_assignee_id_raw, inspector_id_raw)
        )
        if not assignment_fields_present:
            handler_assignee_id, attorney_assignee_id, manager_assignee_id = (
                _merge_workflow_assignees_with_case_defaults(
                    case_id=str(matter_id),
                    handler_id=handler_assignee_id,
                    attorney_id=attorney_assignee_id,
                    manager_id=manager_assignee_id,
                    fallback_handler_id=getattr(current_user, "id", None),
                )
            )

    manual_category = _requested_manual_workflow_category(request.form)
    category = _derive_workflow_category(
        case_id=str(matter_id),
        handler_id=handler_assignee_id,
        attorney_id=attorney_assignee_id,
        manager_id=manager_assignee_id,
        manual_category=manual_category,
        hint_name_ref=business_code,
        hint_name_free=name,
    )

    wf = Workflow(case_id=str(matter_id), name=name, category=category)
    case = Matter.query.get(str(matter_id))
    if not case:
        flash("  Matter ID .", "warning")
        return redirect(url_for("case_work.case_list"))

    wf.request_start_date = _parse_date(request_start_date)
    wf.draft_due_date = _parse_date(draft_due_date)
    wf.draft_due_date2 = _parse_date(draft_due_date2)
    wf.submit_due_date = _parse_date(submit_due_date)
    _apply_workflow_due_dates(
        wf,
        legal_due_date_raw=legal_due_date,
        internal_due_date_raw=internal_due_date,
        clear_legacy_deadlines=use_two_deadline_fields,
    )

    wf.difficulty = _parse_float(difficulty_raw)
    wf.page_count = _parse_int(page_count_raw)
    parsed_work_hours = _parse_float(work_hours_raw)
    if work_hours_raw is not None and str(work_hours_raw).strip() and parsed_work_hours is None:
        flash("TC task value could not be saved.", "warning")
    if parsed_work_hours is not None and parsed_work_hours < 0:
        flash("TC(Task) 0  Input  exists.", "warning")
        parsed_work_hours = None
    wf.work_hours = parsed_work_hours

    wf.business_code = str(business_code).strip() or None if business_code is not None else None
    wf.send_memo = str(send_memo).strip() or None if send_memo is not None else None

    p = (priority or "").strip().lower()
    if p in ("normal", "important", "urgent"):
        wf.priority = p
    else:
        wf.priority = None

    if note is not None:
        wf.note = str(note).strip() or None

    if status:
        s = str(status).strip()
        if s in ("Pending", "In Progress", "Completed", "Abandoned"):
            wf.status = s
            if s in ("Completed", "Abandoned"):
                from datetime import date

                wf.completed_date = date.today()

    wf.created_by_id = current_user.id
    snap = _current_staff_snapshot(str(matter_id))
    wf.snapshot_attorney = snap.get("attorney") or None
    wf.snapshot_handler = snap.get("handler") or None
    wf.snapshot_manager = snap.get("manager") or None

    wf.assignee_id = handler_assignee_id
    wf.attorney_assignee_id = attorney_assignee_id
    wf.inspector_id = manager_assignee_id

    deferred_enabled = bool(current_app.config.get("DEFERRED_DOCKET_SYNC_ENABLED"))
    created_docket_ids: list[str] = []
    try:
        db.session.add(wf)
        db.session.flush()  # wf.id ( X)
        sync_assignment_requests_for_changed_roles(
            wf,
            {},
            requested_by_id=getattr(current_user, "id", None),
            source="workflow_create",
        )

        # (1) /   (after_commit) 
        if deferred_enabled:
            try:
                enqueue_workflow_sync(workflow_id=wf.id)
                enqueue_workflow_task_sync(workflow_id=wf.id, actor_id=current_user.id)
            except (RuntimeError, SQLAlchemyError) as e:
                current_app.logger.warning(
                    f"Deferred workflow sync enqueue failed for wf={wf.id}: {e}"
                )

        # (2) DocketItem Create best-effort + Failed ( pass )
        try:
            import uuid

            from app.models.ip_records import DocketItem
            from app.models.user import User

            normalized_wf_category = normalize_workflow_category(wf.category) or "WORK"
            owner_staff_party_id = None
            primary_owner_user_id = workflow_primary_owner_user_id(
                category=normalized_wf_category,
                handler_id=wf.assignee_id,
                attorney_id=wf.attorney_assignee_id,
                manager_id=wf.inspector_id,
            )
            if primary_owner_user_id:
                assignee_user = User.query.get(primary_owner_user_id)
                if assignee_user:
                    owner_staff_party_id = getattr(assignee_user, "staff_party_id", None)
            if not owner_staff_party_id and matter_id:
                owner_staff_party_id = _resolve_primary_staff_party_id_from_matter(
                    str(matter_id),
                    prefer_mgmt=(normalized_wf_category == "MGMT"),
                )

            deadline_types = []
            effective_due = getattr(wf, "due_date", None)
            legal_due = getattr(wf, "legal_due_date", None) or effective_due
            if legal_due:
                deadline_types.append(
                    {
                        "kind": "LEG",
                        "due_date": legal_due,
                        "category": "LEGAL",
                    }
                )
            if wf.draft_due_date:
                deadline_types.append(
                    {
                        "kind": "DRA",
                        "due_date": wf.draft_due_date,
                        "category": "DRAFT",
                    }
                )
            if wf.submit_due_date:
                deadline_types.append(
                    {
                        "kind": "SUB",
                        "due_date": wf.submit_due_date,
                        "category": "SUBMIT",
                    }
                )

            for dt in deadline_types:
                key = dt["kind"]
                canonical_id = f"WF-{wf.id}-{key}"
                docket_name = workflow_deadline_title(
                    name,
                    key,
                    legal_due_date=legal_due,
                    effective_due_date=effective_due,
                )
                deadline_label = (
                    workflow_deadline_label(
                        key,
                        legal_due_date=legal_due,
                        effective_due_date=effective_due,
                    )
                    or key
                )

                di_category = normalized_wf_category

                # 1) canonical exact, 2) legacy(random) reuse (canonical_id-XXXX)
                di = (
                    DocketItem.query.filter_by(
                        matter_id=str(matter_id), docket_id=canonical_id
                    ).first()
                    or DocketItem.query.filter(
                        DocketItem.matter_id == str(matter_id),
                        DocketItem.docket_id.like(f"{canonical_id}-%"),
                    )
                    .order_by(DocketItem.docket_id.asc())
                    .first()
                )
                if not di:
                    di = DocketItem(docket_id=canonical_id, matter_id=str(matter_id))

                # update fields (idempotent)
                di.category = di_category
                if hasattr(di, "raw_id"):
                    di.raw_id = canonical_id
                if hasattr(di, "is_deleted"):
                    di.is_deleted = False
                if hasattr(di, "deleted_at"):
                    di.deleted_at = None
                if hasattr(di, "deleted_by"):
                    di.deleted_by = None
                if hasattr(di, "delete_reason"):
                    di.delete_reason = None
                di.name_ref = docket_name
                di.name_free = docket_name
                di.due_date = (
                    dt["due_date"].isoformat()
                    if hasattr(dt["due_date"], "isoformat")
                    else str(dt["due_date"])
                )
                if key == "LEG" and effective_due and legal_due and effective_due != legal_due:
                    di.extended_due_date = (
                        effective_due.isoformat()
                        if hasattr(effective_due, "isoformat")
                        else str(effective_due)
                    )
                else:
                    di.extended_due_date = None
                di.owner_staff_party_id = owner_staff_party_id
                di.snapshot_attorney = wf.snapshot_attorney
                di.snapshot_handler = wf.snapshot_handler
                di.snapshot_manager = wf.snapshot_manager
                di.memo = f"{deadline_label} - {wf.note or ''}".strip(" -")

                # SAVEPOINT: /   outer   
                try:
                    with db.session.begin_nested():
                        db.session.add(di)
                        db.session.flush()
                        created_docket_ids.append(str(getattr(di, "docket_id", canonical_id)))
                except IntegrityError:
                    #       Search  
                    di2 = (
                        DocketItem.query.filter_by(
                            matter_id=str(matter_id), docket_id=canonical_id
                        ).first()
                        or DocketItem.query.filter(
                            DocketItem.matter_id == str(matter_id),
                            DocketItem.docket_id.like(f"{canonical_id}-%"),
                        )
                        .order_by(DocketItem.docket_id.asc())
                        .first()
                    )
                    if di2:
                        di2.category = di.category
                        if hasattr(di2, "raw_id"):
                            di2.raw_id = getattr(di, "raw_id", None)
                        if hasattr(di2, "is_deleted"):
                            di2.is_deleted = getattr(di, "is_deleted", False)
                        if hasattr(di2, "deleted_at"):
                            di2.deleted_at = getattr(di, "deleted_at", None)
                        if hasattr(di2, "deleted_by"):
                            di2.deleted_by = getattr(di, "deleted_by", None)
                        if hasattr(di2, "delete_reason"):
                            di2.delete_reason = getattr(di, "delete_reason", None)
                        di2.name_ref = di.name_ref
                        di2.name_free = di.name_free
                        di2.due_date = di.due_date
                        di2.extended_due_date = di.extended_due_date
                        di2.owner_staff_party_id = di.owner_staff_party_id
                        di2.snapshot_attorney = di.snapshot_attorney
                        di2.snapshot_handler = di.snapshot_handler
                        di2.snapshot_manager = di.snapshot_manager
                        di2.memo = di.memo
                        db.session.add(di2)
                        created_docket_ids.append(str(getattr(di2, "docket_id", canonical_id)))
            db.session.flush()
        except SQLAlchemyError as e:
            current_app.logger.warning(f"Failed to create DocketItem(s) for wf={wf.id}: {e}")

        _record_workflow_audit(
            action="workflow.create",
            wf=wf,
            snapshot=_workflow_audit_snapshot(wf),
            extra={"linked_docket_ids": sorted(set(created_docket_ids))},
        )
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        current_app.logger.exception(f"Workflow create failed: {e}")
        flash(" Create In Progress Error .", "danger")
        return redirect(url_for("case_work.case_detail", case_id=matter_id))

    flash(" Add.")
    return redirect(url_for("case_work.case_detail", case_id=matter_id))


@bp.route("/<int:workflow_id>/update_status", methods=["POST"])
@login_required
def update_status(workflow_id):
    wf = Workflow.query.get_or_404(workflow_id)
    new_status = request.form.get("status")
    next_url = safe_next_url(request.form.get("next"))
    matter_id = (
        request.form.get("matter_id") or request.form.get("case_id") or str(wf.case_id or "")
    ).strip() or None
    internal_due_date_raw = _workflow_internal_due_date_raw(request.form)
    use_two_deadline_fields = _uses_two_deadline_fields(request.form)
    assignee_id_raw = request.form.get("assignee_id")
    attorney_assignee_id_raw = request.form.get("attorney_assignee_id")
    category_raw = request.form.get("category")
    note = request.form.get("note")
    business_code = request.form.get("business_code")
    priority = request.form.get("priority")
    send_memo = request.form.get("send_memo")
    inspector_id_raw = request.form.get("inspector_id")
    request_start_date = request.form.get("request_start_date")
    legal_due_date = request.form.get("legal_due_date")
    draft_due_date = request.form.get("draft_due_date")
    draft_due_date2 = request.form.get("draft_due_date2")
    submit_due_date = request.form.get("submit_due_date")
    draft_sent_date = request.form.get("draft_sent_date")
    submit_date = request.form.get("submit_date")
    difficulty_raw = request.form.get("difficulty")
    page_count_raw = request.form.get("page_count")
    work_hours_raw = request.form.get("work_hours")

    assignment_update_requested = any(
        value is not None
        for value in (assignee_id_raw, attorney_assignee_id_raw, inspector_id_raw, category_raw)
    )
    non_assignment_update_requested = any(
        value is not None
        for value in (
            new_status,
            internal_due_date_raw,
            note,
            business_code,
            priority,
            send_memo,
            request_start_date,
            legal_due_date,
            draft_due_date,
            draft_due_date2,
            submit_due_date,
            draft_sent_date,
            submit_date,
            difficulty_raw,
            page_count_raw,
            work_hours_raw,
        )
    )
    required_action = (
        "assign_staff"
        if assignment_update_requested and not non_assignment_update_requested
        else "edit_case"
    )

    if wf.case_id:
        require_matter_access(str(wf.case_id), action=required_action)

    can_edit_case = False
    can_assign_staff = False
    if wf.case_id:
        try:
            can_edit_case = can_access_matter(
                current_user,
                str(wf.case_id),
                action="edit_case",
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.update_status.can_access_matter(edit_case)",
                log_key="workflow.routes.update_status.can_access_matter(edit_case)",
                log_window_seconds=300,
            )
        try:
            can_assign_staff = can_access_matter(
                current_user,
                str(wf.case_id),
                action="assign_staff",
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.update_status.can_access_matter(assign_staff)",
                log_key="workflow.routes.update_status.can_access_matter(assign_staff)",
                log_window_seconds=300,
            )

    # "Manage others" for workflow edits should be driven primarily by case-level assignment permission.
    # _can_manage_others() is a legacy/global heuristic; keep it as a fallback for old deployments.
    can_manage_others = bool(can_edit_case or can_assign_staff or _can_manage_others())
    if not can_manage_others:
        if current_user.id not in _workflow_editors(wf):
            flash("You do not have permission to edit this task.", "warning")
            if next_url:
                return redirect(next_url)
            return redirect(
                url_for("case_work.case_detail", case_id=str(wf.case_id))
                if wf.case_id
                else url_for("workflow.list_tasks")
            )

    linked_di = linked_docket_item_for_workflow(wf)
    assignment_request_before = workflow_assignment_state(wf)
    assignment_before = (
        wf.assignee_id,
        getattr(wf, "attorney_assignee_id", None),
        getattr(wf, "inspector_id", None),
        normalize_workflow_category(getattr(wf, "category", None)),
    )
    audit_before = _workflow_audit_snapshot(wf)

    if new_status:
        try:
            apply_workflow_status_transition(
                wf,
                new_status,
                actor_id=getattr(current_user, "id", None),
                note=note,
                linked_docket_item=linked_di,
            )
        except ValueError:
            flash("Invalid task status.", "warning")
            if next_url:
                return redirect(next_url)
            return redirect(
                url_for("case_work.case_detail", case_id=matter_id)
                if matter_id
                else url_for("workflow.list_tasks")
            )

    if note is not None:
        wf.note = str(note).strip() or None

    if business_code is not None:
        wf.business_code = str(business_code).strip() or None

    p = (priority or "").strip().lower()
    if priority is not None:
        wf.priority = p if p in ("normal", "important", "urgent") else None

    if send_memo is not None:
        wf.send_memo = str(send_memo).strip() or None

    if request_start_date is not None:
        wf.request_start_date = _parse_date(request_start_date)
    _apply_workflow_due_dates(
        wf,
        legal_due_date_raw=legal_due_date,
        internal_due_date_raw=internal_due_date_raw,
        clear_legacy_deadlines=use_two_deadline_fields,
    )
    if draft_due_date is not None:
        wf.draft_due_date = _parse_date(draft_due_date)
    if draft_due_date2 is not None:
        wf.draft_due_date2 = _parse_date(draft_due_date2)
    if submit_due_date is not None:
        wf.submit_due_date = _parse_date(submit_due_date)
    if draft_sent_date is not None:
        wf.draft_sent_date = _parse_date(draft_sent_date)
    if submit_date is not None:
        wf.submit_date = _parse_date(submit_date)

    if difficulty_raw is not None:
        wf.difficulty = _parse_float(difficulty_raw)
    if page_count_raw is not None:
        wf.page_count = _parse_int(page_count_raw)
    if work_hours_raw is not None:
        raw = str(work_hours_raw).strip()
        if not raw:
            wf.work_hours = None
        else:
            parsed_work_hours = _parse_float(work_hours_raw)
            if parsed_work_hours is None:
                flash("TC(Task) value   Existing value .", "warning")
            elif parsed_work_hours < 0:
                flash("TC(Task) 0  Input  exists.", "warning")
            else:
                wf.work_hours = parsed_work_hours

    inspector_id = _parse_user_id(inspector_id_raw) if inspector_id_raw is not None else None
    attorney_assignee_id = (
        _parse_user_id(attorney_assignee_id_raw) if attorney_assignee_id_raw is not None else None
    )
    assignee_id = _parse_user_id(assignee_id_raw) if assignee_id_raw is not None else None

    current_uid = getattr(current_user, "id", None)

    if inspector_id_raw is not None:
        if can_assign_staff:
            wf.inspector_id = inspector_id
        elif inspector_id == current_uid:
            wf.inspector_id = inspector_id
        elif inspector_id is None and wf.inspector_id == current_uid:
            wf.inspector_id = None

    if attorney_assignee_id_raw is not None:
        if can_assign_staff:
            wf.attorney_assignee_id = attorney_assignee_id
        elif attorney_assignee_id == current_uid:
            wf.attorney_assignee_id = attorney_assignee_id
        elif attorney_assignee_id is None and wf.attorney_assignee_id == current_uid:
            wf.attorney_assignee_id = None

    if assignee_id_raw is not None:
        if can_assign_staff:
            wf.assignee_id = assignee_id
        elif assignee_id == current_uid:
            wf.assignee_id = assignee_id
        elif assignee_id is None and wf.assignee_id == current_uid:
            wf.assignee_id = None

    wf.category = _derive_workflow_category(
        case_id=str(wf.case_id or matter_id or ""),
        handler_id=wf.assignee_id,
        attorney_id=wf.attorney_assignee_id,
        manager_id=wf.inspector_id,
        manual_category=_requested_manual_workflow_category(request.form),
        hint_category=wf.category,
        hint_name_ref=wf.business_code,
        hint_name_free=wf.name,
        source=_extract_task_source_from_docket_item(linked_di),
    )

    assignment_after = (
        wf.assignee_id,
        getattr(wf, "attorney_assignee_id", None),
        getattr(wf, "inspector_id", None),
        normalize_workflow_category(getattr(wf, "category", None)),
    )
    if linked_di is not None and assignment_after != assignment_before:
        persist_manual_workflow_assignment_override(
            workflow=wf,
            docket_item=linked_di,
            actor_id=getattr(current_user, "id", None),
        )
    sync_assignment_requests_for_changed_roles(
        wf,
        assignment_request_before,
        requested_by_id=getattr(current_user, "id", None),
        source="workflow_update",
    )

    _ensure_auto_docket_note_marker(wf)

    deferred_enabled = bool(current_app.config.get("DEFERRED_DOCKET_SYNC_ENABLED"))
    try:
        audit_after = _workflow_audit_snapshot(wf)
        audit_changes = diff_snapshots(audit_before, audit_after)
        if audit_changes:
            change_keys = set(audit_changes)
            audit_action = (
                "workflow.status_change"
                if change_keys.issubset({"status", "completed_date"})
                else "workflow.update"
            )
            _record_workflow_audit(
                action=audit_action,
                wf=wf,
                changes=audit_changes,
                linked_docket_id=(
                    str(getattr(linked_di, "docket_id", "") or "")
                    if linked_di is not None
                    else None
                ),
            )
        if deferred_enabled:
            try:
                enqueue_workflow_sync(workflow_id=wf.id)
                enqueue_workflow_task_sync(workflow_id=wf.id, actor_id=current_user.id)
            except (RuntimeError, SQLAlchemyError) as e:
                current_app.logger.warning(
                    f"Deferred workflow sync enqueue failed for wf={wf.id}: {e}"
                )

        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        current_app.logger.exception(f"Workflow update_status failed: {e}")
        flash("Status Change In Progress Error .", "danger")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("case_work.case_detail", case_id=matter_id))

    flash("Status Change.", "success")

    if next_url:
        return redirect(next_url)
    if matter_id:
        return redirect(url_for("case_work.case_detail", case_id=matter_id))
    return redirect(url_for("workflow.list_tasks"))


@bp.route("/<int:workflow_id>/delete", methods=["POST"])
@login_required
def delete_workflow(workflow_id):
    wf = Workflow.query.get_or_404(workflow_id)
    case_id = str(wf.case_id) if wf.case_id else None
    can_edit_case = False
    if case_id:
        require_matter_access(case_id, action="edit_case")
        try:
            can_edit_case = can_access_matter(
                current_user,
                case_id,
                action="edit_case",
            )
        except (RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="workflow.routes.delete_workflow.can_access_matter(edit_case)",
                log_key="workflow.routes.delete_workflow.can_access_matter(edit_case)",
                log_window_seconds=300,
            )
    case = Matter.query.get(case_id) if case_id else None
    if not (can_edit_case or _can_manage_others()):
        if current_user.id not in _workflow_editors(wf):
            flash("You do not have permission to delete this task.", "warning")
            return redirect(
                url_for("case_work.case_detail", case_id=case_id)
                if case
                else url_for("workflow.list_tasks")
            )
    linked_user_deadline_deleted = False
    linked_di: DocketItem | None = linked_docket_item_for_workflow(wf)
    try:
        DeletionService().archive(
            wf,
            user_id=getattr(current_user, "id", None),
            tags=("manual", "workflow-route"),
        )
        # Deleting a workflow linked to a user-created docket should also disable the source
        # docket row; otherwise periodic docket backfill recreates the workflow.
        if linked_di and not (getattr(linked_di, "name_ref", None) or "").strip():
            deleted_at = datetime.utcnow()
            if hasattr(linked_di, "is_deleted"):
                linked_di.is_deleted = True
            if hasattr(linked_di, "deleted_at"):
                linked_di.deleted_at = deleted_at
            if hasattr(linked_di, "deleted_by"):
                linked_di.deleted_by = getattr(current_user, "id", None)
            if hasattr(linked_di, "delete_reason"):
                linked_di.delete_reason = "workflow_delete_linked_user_deadline"
            db.session.add(linked_di)
            linked_user_deadline_deleted = True

        workflow_id_int = int(wf.id)
        _record_workflow_audit(
            action="workflow.delete",
            wf=wf,
            snapshot=_workflow_audit_snapshot(wf),
            linked_docket_id=(
                str(getattr(linked_di, "docket_id", "") or "") if linked_di is not None else None
            ),
            extra={"linked_user_deadline_deleted": linked_user_deadline_deleted},
        )
        delete_workflow_fk_children(workflow_id_int)
        db.session.delete(wf)
        db.session.flush()
        db.session.commit()

        if linked_user_deadline_deleted:
            flash("Task Link User Deadline Delete.", "success")
        else:
            flash("Task Delete.", "success")
    except SQLAlchemyError as e:
        db.session.rollback()
        flash(f"Delete In Progress Error: {e}", "danger")
    if case:
        return redirect(url_for("case_work.case_detail", case_id=case_id))
    return redirect(url_for("workflow.list_tasks"))


from app.blueprints.workflow import tc_routes  # noqa: E402,F401
