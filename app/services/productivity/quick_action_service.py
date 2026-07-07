from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from flask import current_app

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.undo_action import UndoAction
from app.services.billing.invoice_prefill import (
    build_invoice_create_url,
    resolve_invoice_create_base_url,
)
from app.services.case.cascade_delete_service import delete_workflow_fk_children
from app.services.productivity.utils import (
    check_can_access_matter_id,
    get_today,
    get_user_id,
    has_attr_safe,
    set_if_attr,
)
from app.services.workflow.assignment_requests import sync_assignment_requests_for_changed_roles
from app.services.workflow.workflow_docket_autogen import (
    ensure_workflow_dockets,
    workflow_internal_due_from_template,
)
from app.utils.docket_dates import parse_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.task_classification import determine_category_by_staff_role

try:
    from app.models.workflow import Workflow
except ImportError:
    Workflow = None


def _parse_date_str(value: Any) -> Optional[date]:
    if value is None:
        return None
    return parse_date(value)


def quick_add_docket(
    *,
    matter_id: str,
    title: str,
    due_date: str,
    assignee_id: Optional[str] = None,
    priority: Optional[str] = None,
) -> dict:
    uid = get_user_id()
    mid = (matter_id or "").strip()
    if not mid:
        raise ValueError("matter_id required")
    if not check_can_access_matter_id(mid, action="edit_case"):
        raise PermissionError("forbidden")

    m = db.session.query(Matter).get(mid)
    if not m:
        raise ValueError("invalid matter_id")

    title = (title or "").strip() or "Deadline"
    due = _parse_date_str(due_date)
    if not due:
        raise ValueError("due_date must be YYYY-MM-DD")

    assignee_user_id: int | None = None
    owner_staff_party_id: str | None = None
    raw_assignee = (assignee_id or "").strip()
    if raw_assignee:
        if raw_assignee.isdigit():
            try:
                assignee_user_id = int(raw_assignee)
                from app.models.user import User

                user = User.query.get(assignee_user_id)
                if user and user.staff_party_id:
                    owner_staff_party_id = str(user.staff_party_id).strip() or None
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="quick_action_service.quick_add_docket.resolve_assignee_user",
                    log_key="quick_action_service.quick_add_docket.resolve_assignee_user",
                    log_window_seconds=300,
                )
        else:
            owner_staff_party_id = raw_assignee
            try:
                from app.models.user import User

                user = User.query.filter_by(staff_party_id=owner_staff_party_id).first()
                if user:
                    assignee_user_id = user.id
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="quick_action_service.quick_add_docket.resolve_assignee_staff",
                    log_key="quick_action_service.quick_add_docket.resolve_assignee_staff",
                    log_window_seconds=300,
                )

    category = determine_category_by_staff_role(
        mid,
        assignee_id=assignee_user_id,
        staff_party_id=owner_staff_party_id,
        name_free=title,
    )

    d = DocketItem()
    set_if_attr(d, "matter_id", mid)
    set_if_attr(d, "case_id", mid)
    set_if_attr(d, "matter_uuid", mid)
    set_if_attr(d, "category", category)
    set_if_attr(d, "name_free", title)
    set_if_attr(d, "name", title)
    set_if_attr(d, "title", title)
    set_if_attr(d, "docket_name", title)
    set_if_attr(d, "due_date", due)
    set_if_attr(d, "deadline_date", due)
    set_if_attr(d, "date", due)
    if priority:
        set_if_attr(d, "priority", priority)
        set_if_attr(d, "priority_level", priority)
    if owner_staff_party_id:
        set_if_attr(d, "owner_staff_party_id", owner_staff_party_id)
    if assignee_user_id is not None:
        try:
            set_if_attr(d, "assignee_id", int(assignee_user_id))
        except Exception as exc:
            # Best-effort: schema differs by deployment; assignee_id may not be int.
            report_swallowed_exception(
                exc,
                context="quick_action_service.quick_add_docket.assignee_id",
                log_key="quick_action_service.quick_add_docket.assignee_id",
                log_window_seconds=300,
            )
        try:
            set_if_attr(d, "owner_id", int(assignee_user_id))
        except Exception as exc:
            # Best-effort: schema differs by deployment; owner_id may not be int.
            report_swallowed_exception(
                exc,
                context="quick_action_service.quick_add_docket.owner_id",
                log_key="quick_action_service.quick_add_docket.owner_id",
                log_window_seconds=300,
            )

    db.session.add(d)
    db.session.flush()
    did = getattr(d, "docket_id", None) or getattr(d, "id", None)

    ua = UndoAction.create(
        user_id=uid,
        action_type="QUICKADD_DOCKET",
        snapshot={"inserts": [{"model": "DocketItem", "id": did}], "matter_id": mid},
        ttl_seconds=600,
    )
    db.session.commit()

    return {"id": did, "url": f"/case/{mid}#sec-due", "undo_token": ua.token}


def quick_add_workflow(
    *,
    matter_id: str,
    title: str,
    template_key: Optional[str] = None,
    legal_due_date: Optional[str] = None,
    assignee_id: Optional[str] = None,
    manager_assignee_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
    priority: Optional[str] = None,
) -> dict:
    if Workflow is None:
        raise ValueError("workflow not available")
    uid = get_user_id()
    mid = (matter_id or "").strip()
    if not mid:
        raise ValueError("matter_id required")
    if not check_can_access_matter_id(mid, action="edit_case"):
        raise PermissionError("forbidden")

    m = db.session.query(Matter).get(mid)
    if not m:
        raise ValueError("invalid matter_id")

    title = (title or "").strip() or "Task"
    legal_due = _parse_date_str(legal_due_date)
    tpl = (template_key or "").strip().upper() or None
    internal_due = workflow_internal_due_from_template(
        legal_due,
        template_key=tpl,
    )

    wf = Workflow()
    set_if_attr(wf, "matter_id", mid)
    set_if_attr(wf, "case_id", mid)
    set_if_attr(wf, "matter_uuid", mid)
    set_if_attr(wf, "title", title)
    set_if_attr(wf, "name", title)
    set_if_attr(wf, "subject", title)
    if priority:
        set_if_attr(wf, "priority", priority)
        set_if_attr(wf, "priority_level", priority)
    if assignee_id:
        try:
            set_if_attr(wf, "assignee_id", int(assignee_id))
            set_if_attr(wf, "owner_id", int(assignee_id))
        except Exception as exc:
            # Best-effort: schema differs by deployment; assignee/owner ids may not be int.
            report_swallowed_exception(
                exc,
                context="quick_action_service.quick_add_workflow.assignee_id",
                log_key="quick_action_service.quick_add_workflow.assignee_id",
                log_window_seconds=300,
            )
    manager_user_id = manager_assignee_id or reviewer_id
    if manager_user_id:
        try:
            set_if_attr(wf, "manager_assignee_id", int(manager_user_id))
            set_if_attr(wf, "manager_id", int(manager_user_id))
            set_if_attr(wf, "inspector_id", int(manager_user_id))
            # Backward compatibility for legacy schema aliases.
            set_if_attr(wf, "reviewer_id", int(manager_user_id))
            set_if_attr(wf, "checker_id", int(manager_user_id))
            set_if_attr(wf, "auditor_id", int(manager_user_id))
        except Exception as exc:
            # Best-effort: schema differs by deployment; manager/reviewer ids may not be int.
            report_swallowed_exception(
                exc,
                context="quick_action_service.quick_add_workflow.manager_assignee_id",
                log_key="quick_action_service.quick_add_workflow.manager_assignee_id",
                log_window_seconds=300,
            )
    if legal_due:
        set_if_attr(wf, "legal_due_date", legal_due)
        set_if_attr(wf, "law_due_date", legal_due)
        set_if_attr(wf, "due_date", internal_due or legal_due)
        set_if_attr(wf, "deadline", legal_due)
        set_if_attr(wf, "statutory_due_date", legal_due)

    db.session.add(wf)
    db.session.flush()
    sync_assignment_requests_for_changed_roles(
        wf,
        {},
        requested_by_id=uid,
        source="productivity_quick_add_workflow",
    )

    created_dockets = ensure_workflow_dockets(
        wf, template_key=tpl, base_legal_due=legal_due, commit=False
    )
    inserts = [{"model": "Workflow", "id": getattr(wf, "id", None)}]
    for did in (created_dockets or {}).values():
        inserts.append({"model": "DocketItem", "id": did})

    ua = UndoAction.create(
        user_id=uid,
        action_type="QUICKADD_WORKFLOW",
        snapshot={"inserts": inserts, "matter_id": mid},
        ttl_seconds=600,
    )
    db.session.commit()

    return {
        "id": getattr(wf, "id", None),
        "created": {"workflows": 1, "dockets": len(created_dockets or {})},
        "undo_token": ua.token,
    }


def quick_add_invoice(*, matter_id: str) -> dict:
    mid = (matter_id or "").strip()
    if not mid:
        raise ValueError("matter_id required")
    if not check_can_access_matter_id(mid, action="invoice"):
        raise PermissionError("forbidden")
    m = db.session.query(Matter).get(mid)
    if not m:
        raise ValueError("invalid matter_id")

    url = resolve_invoice_create_base_url(config=current_app.config)
    return {
        "url": build_invoice_create_url(
            url,
            matter=m,
            matter_id=mid,
            our_ref=(getattr(m, "our_ref", None) or "").strip(),
        )
    }


def _extract_text(file_bytes: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        # Try pypdf/PyPDF2
        PdfReader = None
        try:
            from pypdf import PdfReader as _PdfReader  # type: ignore

            PdfReader = _PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader as _PdfReader  # type: ignore

                PdfReader = _PdfReader
            except ImportError:
                PdfReader = None
        if PdfReader is None:
            return ""
        try:
            reader = PdfReader(io.BytesIO(file_bytes))
            chunks = []
            for p in getattr(reader, "pages", []) or []:
                try:
                    chunks.append(p.extract_text() or "")
                except Exception:
                    continue
            return "\n".join(chunks)
        except Exception:
            return ""

    # non-pdf: treat as text
    try:
        return file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""


_DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})\s*\s*(\d{1,2})\s*\s*(\d{1,2})\s*\b"),
]


def _pick_due_date(text: str) -> Optional[date]:
    if not text:
        return None

    candidates: list[date] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                yyyy = int(m.group(1))
                mm = int(m.group(2))
                dd = int(m.group(3))
                candidates.append(date(yyyy, mm, dd))
            except (TypeError, ValueError, OverflowError):
                continue

    if not candidates:
        return None

    today = get_today()
    # "   " due_date(Notice/ Due date  )
    future = sorted([d for d in candidates if d >= today])
    if future:
        return future[0]
    #    Recent 
    return sorted(candidates)[-1]


def _classify_doc(text: str) -> dict:
    t = (text or "").upper()
    if "OFFICE ACTION" in t or "OA" in t:
        return {"workflow_title": "OA ", "deadline_name": "OA Statutory Due date"}
    if "NOTICE" in t or "Notice" in t:
        return {"workflow_title": "Notice ", "deadline_name": "Statutory Due date"}
    if "RENEWAL" in t or "Renewal" in t:
        return {"workflow_title": "Renewal/Updated", "deadline_name": "Renewal Due date"}
    return {"workflow_title": "Task", "deadline_name": "Statutory Due date"}


def doc_suggest_from_upload(
    *, file_bytes: bytes, filename: str, matter_id: Optional[str]
) -> list[dict]:
    """
    Document Upload -> 'Task + Deadline'  .
    - Auto Create X (User Confirm  apply)
    - LLM    /Extend 
    """
    text = _extract_text(file_bytes, filename)
    due = _pick_due_date(text)
    meta = _classify_doc(text)

    if due is None:
        #   ,  Template(User  Input)
        return [
            {"kind": "workflow", "title": meta["workflow_title"], "template": "GENERIC"},
            {
                "kind": "docket",
                "title": meta["deadline_name"],
                "due_date": None,
                "deadline_type": "LEGAL",
                "priority": 10,
            },
        ]

    # Default internal deadlines for draft and submit reminders.
    draft = due - timedelta(days=7)
    submit = due

    return [
        {
            "kind": "workflow",
            "title": meta["workflow_title"],
            "template": "GENERIC",
            "legal_due_date": due.isoformat(),
        },
        {
            "kind": "docket",
            "title": meta["deadline_name"],
            "due_date": due.isoformat(),
            "deadline_type": "LEGAL",
            "priority": 10,
        },
        {
            "kind": "docket",
            "title": "Draft Due date",
            "due_date": draft.isoformat(),
            "deadline_type": "DRAFT",
            "priority": 0,
        },
        {
            "kind": "docket",
            "title": " Due date",
            "due_date": submit.isoformat(),
            "deadline_type": "SUBMISSION",
            "priority": 0,
        },
    ]


def apply_doc_suggestions(*, matter_id: str, suggestions: list[dict]) -> dict:
    """
       Create Undo  .
    - MVP: workflow() + docket Create
    - Undo:  applyfrom Create  Delete(1)
    """
    uid = get_user_id()
    mid = (matter_id or "").strip()
    if not mid:
        raise ValueError("matter_id required")
    if not check_can_access_matter_id(mid, action="edit_case"):
        raise PermissionError("forbidden")

    inserts: list[dict] = []
    created = {"workflows": 0, "dockets": 0}

    # Validate matter exists (soft)
    try:
        _ = db.session.query(Matter).get(mid)
    except Exception as exc:
        # Best-effort: do not block apply flow due to validation DB hiccups.
        report_swallowed_exception(
            exc,
            context="quick_action_service.apply_doc_suggestions.validate_matter",
            log_key="quick_action_service.apply_doc_suggestions.validate_matter",
            log_window_seconds=300,
        )

    # Create workflow first (optional)
    wf_id = None
    wf_title = None
    for s in suggestions:
        if (s or {}).get("kind") == "workflow":
            wf_title = (s.get("title") or "Task").strip()
            if Workflow is None:
                break
            try:
                wf = Workflow()
                set_if_attr(wf, "matter_id", mid)
                set_if_attr(wf, "title", wf_title)
                set_if_attr(wf, "name", wf_title)
                set_if_attr(wf, "status", "OPEN")
                set_if_attr(wf, "owner_id", uid)
                set_if_attr(wf, "assignee_id", uid)
                set_if_attr(wf, "legal_due_date", s.get("legal_due_date"))
                db.session.add(wf)
                db.session.flush()
                wf_id = getattr(wf, "id", None)
                inserts.append({"model": "Workflow", "id": wf_id})
                created["workflows"] += 1
            except Exception as exc:
                try:
                    db.session.rollback()
                except Exception as rollback_exc:
                    report_swallowed_exception(
                        rollback_exc,
                        context="quick_action_service.apply_doc_suggestions.create_workflow.rollback",
                        log_key="quick_action_service.apply_doc_suggestions.create_workflow.rollback",
                        log_window_seconds=300,
                    )
                report_swallowed_exception(
                    exc,
                    context="quick_action_service.apply_doc_suggestions.create_workflow",
                    log_key="quick_action_service.apply_doc_suggestions.create_workflow",
                    log_window_seconds=300,
                )
                raise RuntimeError("Failed to create workflow from suggestions") from exc
            break

    # Create dockets
    for s in suggestions:
        if (s or {}).get("kind") != "docket":
            continue
        title = (s.get("title") or "Deadline").strip()
        due_s = (s.get("due_date") or "").strip() or None
        due = None
        if due_s:
            try:
                due = datetime.strptime(due_s, "%Y-%m-%d").date()
            except ValueError:
                due = None

        try:
            d = DocketItem()
            set_if_attr(d, "matter_id", mid)
            set_if_attr(d, "name", title)
            set_if_attr(d, "title", title)
            if due is not None and has_attr_safe(DocketItem, "due_date"):
                set_if_attr(d, "due_date", due)
            set_if_attr(d, "priority", int(s.get("priority") or 0))
            set_if_attr(d, "assignee_id", uid)
            set_if_attr(d, "user_id", uid)
            set_if_attr(d, "deadline_type", (s.get("deadline_type") or "").strip() or None)
            # workflow Link    
            if wf_id is not None:
                for fk in ("workflow_id", "wf_id", "related_workflow_id"):
                    if has_attr_safe(DocketItem, fk):
                        set_if_attr(d, fk, wf_id)
                        break
            db.session.add(d)
            db.session.flush()
            inserts.append({"model": "DocketItem", "id": getattr(d, "id", None)})
            created["dockets"] += 1
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="quick_action_service.apply_doc_suggestions.create_docket.rollback",
                    log_key="quick_action_service.apply_doc_suggestions.create_docket.rollback",
                    log_window_seconds=300,
                )
            report_swallowed_exception(
                exc,
                context="quick_action_service.apply_doc_suggestions.create_docket",
                log_key="quick_action_service.apply_doc_suggestions.create_docket",
                log_window_seconds=300,
            )
            raise RuntimeError("Failed to create docket from suggestions") from exc

    # Undo  Create(10)
    ua = UndoAction.create(
        user_id=uid,
        action_type="DOC_SUGGEST_APPLY",
        snapshot={"inserts": inserts, "matter_id": mid},
        ttl_seconds=600,
    )
    db.session.commit()

    return {
        "created": created,
        "undo_token": ua.token,
        "undo_ttl_seconds": 600,
        "workflow_title": wf_title,
    }


def undo_by_token(*, token: str) -> dict:
    uid = get_user_id()
    tok = (token or "").strip()
    if not tok:
        return {"undone": False, "reason": "empty token"}

    row = db.session.query(UndoAction).filter(UndoAction.token == tok).first()
    if not row:
        return {"undone": False, "reason": "not found"}
    if not row.is_active:
        return {"undone": False, "reason": "already used"}
    if row.user_id != str(uid):
        return {"undone": False, "reason": "forbidden"}
    if row.expires_at and row.expires_at < datetime.utcnow():
        return {"undone": False, "reason": "expired"}

    snap = row.snapshot()
    inserts = snap.get("inserts") or []
    deleted = {"Workflow": 0, "DocketItem": 0}

    #  (Extend )
    model_map = {"DocketItem": DocketItem}
    if Workflow is not None:
        model_map["Workflow"] = Workflow

    try:
        for it in reversed(inserts):
            mname = (it or {}).get("model")
            mid = (it or {}).get("id")
            if not mname or mid is None:
                continue
            cls = model_map.get(mname)
            if cls is None:
                continue
            obj = db.session.query(cls).get(mid)
            if obj is None:
                continue
            if mname == "Workflow":
                delete_workflow_fk_children(mid)
            db.session.delete(obj)
            deleted[mname] = deleted.get(mname, 0) + 1

        row.is_active = False
        row.undone_at = datetime.utcnow()
        db.session.commit()
        return {"undone": True, "deleted": deleted}
    except Exception as e:
        db.session.rollback()
        return {"undone": False, "reason": f"exception: {e}"}
