from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from flask import current_app, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.audit.entity_audit import (
    diff_snapshots,
    record_entity_change_audit,
    snapshot_attrs,
)
from app.services.billing.invoice_prefill import (
    build_invoice_create_url,
    resolve_invoice_create_base_url,
)
from app.services.core.staff_options import resolve_staff_party_id
from app.services.ops.operation_context import OperationContext
from app.services.workflow.assignment_requests import sync_assignment_requests_for_changed_roles
from app.services.workflow.status_transition import apply_workflow_status_transition
from app.services.workflow.sync_requests import enqueue_workflow_sync, enqueue_workflow_task_sync
from app.services.workflow.task_sync import persist_manual_workflow_assignment_override
from app.services.workflow.workflow_docket_autogen import (
    ensure_workflow_dockets,
    workflow_internal_due_from_template,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter, matter_action, require_matter_access
from app.utils.task_assignment_rules import is_manager_only_notice
from app.utils.url_helpers import safe_referrer_path
from app.utils.workflow_semantics import derive_workflow_category

# Reuse existing case blueprint object
from . import bp  # type: ignore

_WORKFLOW_ASSIGNMENT_PATCH_KEYS = frozenset(
    {
        "assignee_id",
        "owner_id",
        "handler_id",
        "attorney_assignee_id",
        "attorney_id",
        "manager_assignee_id",
        "manager_id",
        "inspector_id",
        "reviewer_id",
    }
)
_QUICK_WORKFLOW_AUDIT_FIELDS = (
    "case_id",
    "matter_id",
    "name",
    "title",
    "status",
    "category",
    "priority",
    "business_code",
    "code",
    "workflow_code",
    "legal_due_date",
    "law_due_date",
    "due_date",
    "deadline",
    "statutory_due_date",
    "completed_date",
    "completed_by_id",
    "work_hours",
    "assignee_id",
    "owner_id",
    "handler_id",
    "attorney_assignee_id",
    "attorney_id",
    "inspector_id",
    "manager_assignee_id",
    "manager_id",
    "reviewer_id",
)
_QUICK_DOCKET_AUDIT_FIELDS = (
    "docket_id",
    "id",
    "matter_id",
    "case_id",
    "matter_uuid",
    "category",
    "name_ref",
    "name_free",
    "name",
    "title",
    "docket_name",
    "due_date",
    "deadline_date",
    "date",
    "extended_due_date",
    "visible_from_date",
    "start_date",
    "show_from_date",
    "owner_staff_party_id",
    "assignee_id",
    "owner_id",
    "status",
    "state",
    "priority",
    "priority_level",
    "done_date",
    "memo",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
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


def _set_first_attr(obj: Any, names: list[str], value: Any) -> bool:
    for n in names:
        if hasattr(obj, n):
            try:
                setattr(obj, n, value)
                return True
            except Exception:
                continue
    return False


def _get_first_attr(obj: Any, names: list[str]) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _audit_int_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _audit_workflow_snapshot(wf: Workflow) -> dict[str, Any]:
    return snapshot_attrs(
        wf,
        [field for field in _QUICK_WORKFLOW_AUDIT_FIELDS if hasattr(wf, field)],
    )


def _audit_docket_snapshot(docket: DocketItem) -> dict[str, Any]:
    return snapshot_attrs(
        docket,
        [field for field in _QUICK_DOCKET_AUDIT_FIELDS if hasattr(docket, field)],
    )


def _audit_docket_meta(docket: DocketItem, *, matter_id: str | None = None) -> dict[str, Any]:
    return {
        "docket_id": str(getattr(docket, "docket_id", None) or getattr(docket, "id", "") or ""),
        "matter_id": str(matter_id or getattr(docket, "matter_id", "") or ""),
        "name": (
            str(
                getattr(docket, "name_free", None)
                or getattr(docket, "name", None)
                or getattr(docket, "title", None)
                or getattr(docket, "name_ref", None)
                or ""
            ).strip()
        ),
    }


def _json() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return {}


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _workflow_assignment_state(wf: Workflow) -> tuple[Any, Any, Any]:
    return (
        getattr(wf, "assignee_id", None),
        getattr(wf, "attorney_assignee_id", None),
        getattr(wf, "inspector_id", None),
    )


def _docket_owner_staff_party_id_from_patch(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    user_id = _coerce_positive_int(raw)
    if user_id is not None:
        user = db.session.get(User, user_id)
        staff_party_id = (getattr(user, "staff_party_id", None) or "").strip() if user else ""
        if staff_party_id:
            return staff_party_id

    try:
        resolved = resolve_staff_party_id(raw)
    except Exception:
        resolved = None
    return (resolved or raw).strip() or None


def _user_id_for_staff_party_id(staff_party_id: str | None) -> int | None:
    raw = (staff_party_id or "").strip()
    if not raw:
        return None
    user = User.query.filter(User.staff_party_id == raw, User.is_active.is_(True)).first()
    return int(user.id) if user and getattr(user, "id", None) else None


def _linked_workflow_for_docket_item(docket_item: DocketItem) -> Workflow | None:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    matter_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not docket_id or not matter_id:
        return None
    if docket_id.upper().startswith("WF-"):
        return None

    prefix = f"DOCKET:{docket_id}"
    candidates = (
        Workflow.query.filter(Workflow.case_id == matter_id)
        .filter(Workflow.business_code.like(f"{prefix}%"))
        .order_by(Workflow.id.asc())
        .all()
    )
    return next(
        (wf for wf in candidates if (getattr(wf, "business_code", None) or "").strip() == prefix),
        candidates[0] if candidates else None,
    )


def _derive_quick_workflow_category(
    *,
    matter_id: str,
    workflow_code: str | None,
    title: str | None,
    handler_uid: int | None,
    attorney_uid: int | None,
    manager_uid: int | None,
) -> str:
    if manager_uid and is_manager_only_notice(
        name_ref=workflow_code,
        name_free=title,
    ):
        return "MGMT"
    return derive_workflow_category(
        case_id=matter_id,
        handler_id=handler_uid,
        attorney_id=attorney_uid,
        manager_id=manager_uid,
        hint_name_ref=workflow_code,
        hint_name_free=title,
    )


def _require_bulk_case_access(items: list, *, action: str) -> bool:
    for item in items:
        matter_id = _get_first_attr(item, ["case_id", "matter_id", "matter_uuid"])
        if not matter_id:
            return False
        if not can_access_matter(current_user, str(matter_id), action=action):
            return False
    return True


@bp.post("/<string:matter_id>/quick/workflow")
@login_required
def quick_create_workflow(matter_id: str):
    """
    Create workflow + auto-generate LEGAL/DRAFT/SUBMIT dockets (idempotent) for convenience.
    Form POST (works with CSRF-protected form).
    """
    mid = (matter_id or "").strip()
    if not mid:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))

    m = Matter.query.get(mid)
    if not m:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))
    require_matter_access(mid, action="edit_case")

    form = request.form or {}
    code = (form.get("code") or form.get("workflow_code") or "").strip()
    title = (form.get("title") or form.get("name") or "").strip()
    priority = (form.get("priority") or "MEDIUM").strip()

    assignee_id = form.get("assignee_id") or form.get("owner_id") or ""
    attorney_assignee_id = form.get("attorney_assignee_id") or form.get("attorney_id") or ""
    manager_assignee_id = (
        form.get("inspector_id") or form.get("manager_assignee_id") or form.get("manager_id") or ""
    )
    reviewer_id = form.get("reviewer_id") or ""
    effective_manager_assignee_id = manager_assignee_id or reviewer_id
    legal_due = _as_date(form.get("legal_due_date") or form.get("due_date"))
    tpl = (form.get("template_key") or "").strip().upper() or None
    internal_due = workflow_internal_due_from_template(
        legal_due,
        template_key=tpl,
        workflow_code=code or None,
    )

    wf = Workflow()
    _set_first_attr(wf, ["matter_id", "case_id", "matter_uuid"], mid)
    if code:
        _set_first_attr(wf, ["code", "workflow_code", "business_code"], code)
    if title:
        _set_first_attr(wf, ["title", "name", "subject"], title)
    _set_first_attr(wf, ["priority", "priority_level"], priority)

    if assignee_id:
        try:
            _set_first_attr(
                wf, ["assignee_id", "owner_id", "user_id", "handler_id"], int(assignee_id)
            )
        except Exception as exc:
            # Best-effort: schema differs by deployment; ids may not be int.
            report_swallowed_exception(
                exc,
                context="case.quick_routes.quick_create_workflow.assignee_id",
                log_key="case.quick_routes.quick_create_workflow.assignee_id",
                log_window_seconds=300,
            )
    if attorney_assignee_id:
        try:
            _set_first_attr(
                wf,
                ["attorney_assignee_id", "attorney_id"],
                int(attorney_assignee_id),
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case.quick_routes.quick_create_workflow.attorney_assignee_id",
                log_key="case.quick_routes.quick_create_workflow.attorney_assignee_id",
                log_window_seconds=300,
            )
    if effective_manager_assignee_id:
        try:
            _set_first_attr(
                wf,
                ["inspector_id", "manager_assignee_id", "manager_id"],
                int(effective_manager_assignee_id),
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case.quick_routes.quick_create_workflow.manager_assignee_id",
                log_key="case.quick_routes.quick_create_workflow.manager_assignee_id",
                log_window_seconds=300,
            )
    if reviewer_id:
        try:
            # Backward compatibility for legacy reviewer-style fields.
            _set_first_attr(wf, ["reviewer_id", "checker_id", "auditor_id"], int(reviewer_id))
        except Exception as exc:
            # Best-effort: schema differs by deployment; ids may not be int.
            report_swallowed_exception(
                exc,
                context="case.quick_routes.quick_create_workflow.reviewer_id",
                log_key="case.quick_routes.quick_create_workflow.reviewer_id",
                log_window_seconds=300,
            )
    if legal_due:
        _set_first_attr(
            wf,
            ["legal_due_date", "law_due_date", "due_date", "deadline", "statutory_due_date"],
            legal_due,
        )
        _set_first_attr(wf, ["due_date"], internal_due or legal_due)

    try:
        handler_uid = _get_first_attr(wf, ["assignee_id", "owner_id", "user_id", "handler_id"])
        attorney_uid = _get_first_attr(wf, ["attorney_assignee_id", "attorney_id"])
        manager_uid = _get_first_attr(wf, ["inspector_id", "manager_assignee_id", "manager_id"])
        category = _derive_quick_workflow_category(
            matter_id=mid,
            workflow_code=code or None,
            title=title or None,
            handler_uid=int(handler_uid) if handler_uid else None,
            attorney_uid=int(attorney_uid) if attorney_uid else None,
            manager_uid=int(manager_uid) if manager_uid else None,
        )
        _set_first_attr(wf, ["category"], category)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.quick_routes.quick_create_workflow.category",
            log_key="case.quick_routes.quick_create_workflow.category",
            log_window_seconds=300,
        )

    db.session.add(wf)
    db.session.flush()
    try:
        sync_assignment_requests_for_changed_roles(
            wf,
            {},
            requested_by_id=getattr(current_user, "id", None),
            source="case_quick_create",
        )
        ensure_workflow_dockets(wf, template_key=tpl, base_legal_due=legal_due, commit=False)
        record_entity_change_audit(
            action="workflow.create",
            target_type="workflow",
            target_id=_audit_int_id(getattr(wf, "id", None)),
            actor_id=getattr(current_user, "id", None),
            after=_audit_workflow_snapshot(wf),
            meta={
                "matter_id": mid,
                "source": "case.quick.workflow",
                "template_key": tpl,
            },
            title=title or code or f"Workflow #{getattr(wf, 'id', '')}",
            include_snapshots=True,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return redirect(safe_referrer_path() or url_for("case_work.case_detail", case_id=mid))


@bp.post("/<string:matter_id>/quick/docket")
@login_required
def quick_create_docket(matter_id: str):
    mid = (matter_id or "").strip()
    if not mid:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))
    m = Matter.query.get(mid)
    if not m:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))
    require_matter_access(mid, action="edit_case")

    form = request.form or {}
    name = (form.get("name") or form.get("title") or "Deadline").strip()
    due = _as_date(form.get("due_date"))
    visible_from = _as_date(form.get("visible_from_date"))
    assignee_id = form.get("assignee_id") or ""
    priority = (form.get("priority") or "MEDIUM").strip()

    if not due:
        return redirect(safe_referrer_path() or url_for("case_work.case_detail", case_id=mid))

    d = DocketItem()
    _set_first_attr(d, ["matter_id", "case_id", "matter_uuid"], mid)
    _set_first_attr(d, ["name_free", "name", "title", "docket_name"], name)
    _set_first_attr(d, ["due_date", "deadline_date", "date"], due)
    if visible_from:
        _set_first_attr(d, ["visible_from_date", "start_date", "show_from_date"], visible_from)
    _set_first_attr(d, ["priority", "priority_level"], priority)

    if assignee_id:
        # DocketItem usually uses owner_staff_party_id (text)
        try:
            _set_first_attr(
                d, ["owner_staff_party_id", "assignee_id", "owner_id"], str(assignee_id)
            )
        except Exception as exc:
            # Best-effort: schema differs by deployment; owner fields may not exist.
            report_swallowed_exception(
                exc,
                context="case.quick_routes.quick_create_docket.owner_fields",
                log_key="case.quick_routes.quick_create_docket.owner_fields",
                log_window_seconds=300,
            )

    # _set_first_attr(d, ["source", "origin"], "CASE_QUICK") # source might not exist on DocketItem
    db.session.add(d)
    try:
        db.session.flush()
        after = _audit_docket_snapshot(d)
        record_entity_change_audit(
            action="docket.create",
            target_type="docket_item",
            target_id=_audit_int_id(getattr(d, "id", None)),
            actor_id=getattr(current_user, "id", None),
            after=after,
            meta={
                **_audit_docket_meta(d, matter_id=mid),
                "source": "case.quick.docket",
            },
            title=name,
            include_snapshots=True,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return redirect(safe_referrer_path() or url_for("case_work.case_detail", case_id=mid))


def _invoice_create_url() -> str:
    return resolve_invoice_create_base_url(config=current_app.config)


@bp.post("/<string:matter_id>/quick/invoice")
@matter_action("invoice")
@login_required
def quick_create_invoice(matter_id: str):
    """
    Best-effort "1-click invoice":
      - redirect to invoice create page with ipm_case_id/ipm_case_ref.
    If you already have a Python service for creating invoice drafts, you can wire it here later.
    """
    mid = (matter_id or "").strip()
    m = Matter.query.get(mid)
    if not m:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))
    require_matter_access(mid, action="invoice")
    return redirect(
        build_invoice_create_url(
            _invoice_create_url(),
            matter=m,
            matter_id=mid,
            our_ref=(_get_first_attr(m, ["our_ref", "ref", "internal_ref"]) or "").strip(),
        )
    )


@bp.post("/<string:matter_id>/quick/invoice/from-worklogs")
@matter_action("invoice")
@login_required
def quick_create_invoice_from_worklogs(matter_id: str):
    mid = (matter_id or "").strip()
    m = Matter.query.get(mid)
    if not m:
        return redirect(safe_referrer_path() or url_for("case_work.case_list"))
    require_matter_access(mid, action="invoice")

    # ids from form checkbox
    ids = request.form.getlist("worklog_id")
    ids = [x for x in ids if str(x).strip().isdigit()]
    ids_csv = ",".join(ids)

    return redirect(
        build_invoice_create_url(
            _invoice_create_url(),
            matter=m,
            matter_id=mid,
            our_ref=(_get_first_attr(m, ["our_ref", "ref", "internal_ref"]) or "").strip(),
            worklog_ids=ids_csv,
        )
    )


# ---------------------------
# Quick Edit JSON APIs
# ---------------------------


def _apply_workflow_patch(wf: Workflow, patch: dict) -> None:
    # Generic keys => candidate attribute names (duck-typed to fit existing schema)
    mapping: dict[str, list[str]] = {
        "status": ["status", "progress_state", "state"],
        "priority": ["priority", "priority_level"],
        "assignee_id": ["assignee_id", "owner_id", "user_id", "handler_id"],
        "attorney_assignee_id": ["attorney_assignee_id", "attorney_id"],
        "attorney_id": ["attorney_assignee_id", "attorney_id"],
        "manager_assignee_id": [
            "manager_assignee_id",
            "manager_id",
            "inspector_id",
            "reviewer_id",
            "checker_id",
            "auditor_id",
        ],
        "manager_id": ["manager_assignee_id", "manager_id", "inspector_id"],
        "inspector_id": ["inspector_id", "manager_assignee_id", "reviewer_id"],
        "handler_id": ["assignee_id", "owner_id", "user_id", "handler_id"],
        "reviewer_id": ["reviewer_id", "checker_id", "auditor_id", "inspector_id"],
        "owner_id": ["owner_id", "assignee_id", "user_id", "handler_id"],
        "title": ["title", "name", "subject"],
        "code": ["code", "workflow_code", "business_code"],
        "legal_due_date": [
            "legal_due_date",
            "law_due_date",
            "due_date",
            "deadline",
            "statutory_due_date",
        ],
    }
    for k, v in (patch or {}).items():
        if k not in mapping:
            continue
        val: Any = v
        if k.endswith("_id"):
            try:
                val = int(str(v))
            except Exception:
                continue
        if k.endswith("_date"):
            val = _as_date(v)
            if not val:
                continue
        _set_first_attr(wf, mapping[k], val)


def _apply_docket_patch(d: DocketItem, patch: dict) -> None:
    mapping: dict[str, list[str]] = {
        "name": ["name_free", "name", "title", "docket_name"],
        "due_date": ["due_date", "deadline_date", "date"],
        "priority": ["priority", "priority_level"],
        "assignee_id": ["owner_staff_party_id", "assignee_id", "owner_id", "user_id", "handler_id"],
        "status": ["status", "state"],
    }
    for k, v in (patch or {}).items():
        if k not in mapping:
            continue
        val: Any = v
        if k == "assignee_id":
            val = _docket_owner_staff_party_id_from_patch(v)
        elif k.endswith("_id"):
            try:
                val = int(str(v))
            except Exception:
                continue
        if k.endswith("_date"):
            val = _as_date(v)
            if not val:
                continue
        _set_first_attr(d, mapping[k], val)


@bp.patch("/api/workflows/bulk")
@login_required
def api_workflows_bulk_patch():
    payload = _json()
    ids = payload.get("ids") or []
    patch = payload.get("patch") or {}
    if not isinstance(ids, list) or not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    safe_ids = [int(x) for x in ids if str(x).isdigit()]
    if not safe_ids:
        return jsonify({"ok": False, "error": "empty ids"}), 400

    count = 0
    q = Workflow.query.filter(Workflow.id.in_(safe_ids))
    rows = q.all()
    if rows and not _require_bulk_case_access(rows, action="edit_case"):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        with OperationContext(
            action="workflow.bulk_patch",
            risk_level="MEDIUM",
            undo_supported=True,
            undo_deadline_at=datetime.utcnow() + timedelta(days=7),
            targets_json={"workflow_ids": safe_ids},
            summary_json={"patch": patch},
            preop_backup_required=False,
        ) as op:
            status_patch_requested = "status" in patch
            workflow_patch = {k: v for k, v in patch.items() if k != "status"}
            actor_id = int(getattr(current_user, "id", 0) or 0) or None
            for wf in rows:
                before = _audit_workflow_snapshot(wf)
                assignment_before = _workflow_assignment_state(wf)
                _apply_workflow_patch(wf, workflow_patch)
                if status_patch_requested:
                    apply_workflow_status_transition(
                        wf,
                        patch.get("status"),
                        actor_id=actor_id,
                        note=getattr(wf, "note", None),
                    )
                    if getattr(wf, "id", None):
                        try:
                            enqueue_workflow_sync(workflow_id=int(wf.id))
                            enqueue_workflow_task_sync(
                                workflow_id=int(wf.id),
                                actor_id=actor_id,
                            )
                        except Exception as exc:
                            report_swallowed_exception(
                                exc,
                                context="case.quick.workflow_bulk_patch.enqueue_status_sync",
                                log_key="case.quick.workflow_bulk_patch.enqueue_status_sync",
                                log_window_seconds=300,
                            )
                if (
                    _WORKFLOW_ASSIGNMENT_PATCH_KEYS.intersection(patch.keys())
                    and _workflow_assignment_state(wf) != assignment_before
                ):
                    persist_manual_workflow_assignment_override(
                        workflow=wf,
                        actor_id=int(getattr(current_user, "id", 0) or 0) or None,
                    )
                    sync_assignment_requests_for_changed_roles(
                        wf,
                        assignment_before,
                        requested_by_id=int(getattr(current_user, "id", 0) or 0) or None,
                        source="case_quick_workflow_bulk_patch",
                    )
                op.add_change(
                    entity_type="Workflow",
                    entity_id=str(wf.id),
                    change_type="patch",
                    patch=patch,
                )
                after = _audit_workflow_snapshot(wf)
                changes = diff_snapshots(before, after)
                if changes:
                    record_entity_change_audit(
                        action=(
                            "workflow.status_change"
                            if set(changes.keys()).issubset(
                                {"status", "completed_date", "completed_by_id"}
                            )
                            else "workflow.update"
                        ),
                        target_type="workflow",
                        target_id=_audit_int_id(getattr(wf, "id", None)),
                        actor_id=getattr(current_user, "id", None),
                        changes=changes,
                        meta={
                            "matter_id": str(
                                _get_first_attr(wf, ["case_id", "matter_id", "matter_uuid"]) or ""
                            ),
                            "source": "case.quick.workflow_bulk_patch",
                            "patch_keys": sorted(str(k) for k in patch.keys()),
                        },
                        title=str(_get_first_attr(wf, ["title", "name", "subject"]) or ""),
                    )
                count += 1
            op.commit()
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc) or "invalid_status"}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("case.quick_routes.workflow_bulk_patch_failed")
        return jsonify({"ok": False, "error": "bulk_patch_failed"}), 500
    return jsonify({"ok": True, "updated": count})


@bp.patch("/api/dockets/bulk")
@login_required
def api_dockets_bulk_patch():
    payload = _json()
    ids = payload.get("ids") or []
    patch = payload.get("patch") or {}
    if not isinstance(ids, list) or not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    # Docket IDs are strings (UUIDs)
    # Filter only valid-looking strings to prevent injection if raw SQL was used (but ORM is safe)
    safe_ids = [str(x) for x in ids if x]
    if not safe_ids:
        return jsonify({"ok": False, "error": "empty ids"}), 400

    count = 0
    if hasattr(DocketItem, "docket_id"):
        q = DocketItem.query.filter(DocketItem.docket_id.in_(safe_ids))
    else:
        # Fallback if somehow using integer ID model
        q = DocketItem.query.filter(DocketItem.id.in_(safe_ids))
    rows = q.all()
    if rows and not _require_bulk_case_access(rows, action="edit_case"):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    try:
        with OperationContext(
            action="docket.bulk_patch",
            risk_level="MEDIUM",
            undo_supported=True,
            undo_deadline_at=datetime.utcnow() + timedelta(days=7),
            targets_json={"docket_ids": safe_ids},
            summary_json={"patch": patch},
            preop_backup_required=False,
        ) as op:
            for d in rows:
                before = _audit_docket_snapshot(d)
                owner_before = (getattr(d, "owner_staff_party_id", None) or "").strip()
                _apply_docket_patch(d, patch)
                if "assignee_id" in patch:
                    owner_after = (getattr(d, "owner_staff_party_id", None) or "").strip()
                    if owner_after != owner_before:
                        linked_workflow = _linked_workflow_for_docket_item(d)
                        linked_workflow_before = (
                            _audit_workflow_snapshot(linked_workflow)
                            if linked_workflow is not None
                            else None
                        )
                        linked_assignment_before = (
                            _workflow_assignment_state(linked_workflow)
                            if linked_workflow is not None
                            else None
                        )
                        if linked_workflow is not None:
                            assignee_user_id = _user_id_for_staff_party_id(owner_after)
                            if owner_after and assignee_user_id is None:
                                linked_workflow = None
                            else:
                                linked_workflow.assignee_id = assignee_user_id
                        if linked_workflow is not None:
                            linked_workflow.category = derive_workflow_category(
                                case_id=str(getattr(d, "matter_id", "") or ""),
                                handler_id=getattr(linked_workflow, "assignee_id", None),
                                attorney_id=getattr(linked_workflow, "attorney_assignee_id", None),
                                manager_id=getattr(linked_workflow, "inspector_id", None),
                                hint_category=getattr(linked_workflow, "category", None),
                                hint_name_ref=getattr(d, "name_ref", None),
                                hint_name_free=(getattr(d, "name_free", None) or "").strip()
                                or getattr(d, "name_ref", None),
                            )
                            persist_manual_workflow_assignment_override(
                                workflow=linked_workflow,
                                actor_id=int(getattr(current_user, "id", 0) or 0) or None,
                            )
                            sync_assignment_requests_for_changed_roles(
                                linked_workflow,
                                linked_assignment_before,
                                requested_by_id=int(getattr(current_user, "id", 0) or 0) or None,
                                source="case_quick_docket_bulk_patch",
                            )
                            db.session.add(linked_workflow)
                            linked_changes = diff_snapshots(
                                linked_workflow_before or {},
                                _audit_workflow_snapshot(linked_workflow),
                            )
                            if linked_changes:
                                record_entity_change_audit(
                                    action="workflow.update",
                                    target_type="workflow",
                                    target_id=_audit_int_id(getattr(linked_workflow, "id", None)),
                                    actor_id=getattr(current_user, "id", None),
                                    changes=linked_changes,
                                    meta={
                                        "matter_id": str(getattr(d, "matter_id", "") or ""),
                                        "docket_id": str(
                                            getattr(d, "docket_id", None)
                                            or getattr(d, "id", "")
                                            or ""
                                        ),
                                        "source": "case.quick.docket_bulk_patch.linked_workflow",
                                    },
                                    title=str(
                                        _get_first_attr(
                                            linked_workflow,
                                            ["title", "name", "subject"],
                                        )
                                        or ""
                                    ),
                                )
                entity_id = getattr(d, "docket_id", None) or getattr(d, "id", None)
                op.add_change(
                    entity_type="DocketItem",
                    entity_id=str(entity_id),
                    change_type="patch",
                    patch=patch,
                )
                after = _audit_docket_snapshot(d)
                changes = diff_snapshots(before, after)
                if changes:
                    record_entity_change_audit(
                        action=(
                            "docket.status_change"
                            if set(changes.keys()) <= {"status", "state"}
                            else "docket.update"
                        ),
                        target_type="docket_item",
                        target_id=_audit_int_id(getattr(d, "id", None)),
                        actor_id=getattr(current_user, "id", None),
                        changes=changes,
                        meta={
                            **_audit_docket_meta(d),
                            "source": "case.quick.docket_bulk_patch",
                            "patch_keys": sorted(str(k) for k in patch.keys()),
                        },
                        title=_audit_docket_meta(d).get("name") or "Deadline",
                    )
                count += 1
            op.commit()
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("case.quick_routes.docket_bulk_patch_failed")
        return jsonify({"ok": False, "error": "bulk_patch_failed"}), 500
    return jsonify({"ok": True, "updated": count})
