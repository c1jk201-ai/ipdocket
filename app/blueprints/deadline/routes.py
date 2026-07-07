from __future__ import annotations

import json

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.deadline import bp
from app.extensions import db
from app.models.party import Party
from app.models.ip_records import DocketItem, Matter, MatterStaffAssignment
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.audit.entity_audit import load_audit_rows_by_meta_value
from app.services.deletion_manager import DeletionService
from app.services.docket_manual_state import (
    clear_docket_manual_abandoned,
    mark_docket_manual_abandoned,
)
from app.services.workflow.sync_requests import enqueue_docket_sync_for_item
from app.utils.docket_dates import (
    adjusted_legal_due_for_docket,
    done_state,
    effective_due_for_work,
    effective_due_text_expr,
    internal_due_for_docket,
)
from app.utils.docket_visibility import is_visible_by_date, visible_on_or_before
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_manage_case_globally
from app.utils.permissions import is_manager as is_manager_role
from app.utils.permissions import (
    is_manager_assigned_to_matter,
    managed_matter_ids_select,
    policy_accessible_matter_ids_select,
    resolve_role_scope,
)
from app.utils.workflow_deadline_labels import strip_workflow_deadline_title_suffix

try:
    from app.models.user import (
        ROLE_ADMIN,
        ROLE_LEAD_ATTORNEY,
        ROLE_MGMT_DIRECTOR,
        ROLE_MGMT_STAFF,
        ROLE_PARTNER_ATTORNEY,
        ROLE_PATENT_STAFF,
    )
except ImportError:
    ROLE_ADMIN = "admin"
    ROLE_MGMT_DIRECTOR = "mgmt_director"
    ROLE_MGMT_STAFF = "mgmt_staff"
    ROLE_LEAD_ATTORNEY = "lead_attorney"
    ROLE_PATENT_STAFF = "patent_staff"
    ROLE_PARTNER_ATTORNEY = "partner_attorney"

import re
from datetime import date, timedelta

from sqlalchemy import and_, case, func, or_

from app.utils.annuity_deadline_routing import is_annuity_status_red_deadline


def _parse_date_str(v) -> date | None:
    """Parse date string to date object."""
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        s = str(v).strip().split("T")[0]
        return date.fromisoformat(s)
    except Exception:
        return None


def _normalize_deadline_action(value: object) -> str | None:
    token = str(value or "").strip().lower()
    if not token:
        return None
    if token in {"done", "complete", "completed"}:
        return "done"
    if token in {"new", "open", "overdue", "pending", "reopen", "reopened"}:
        return "pending"
    if token in {"abandon", "abandoned", "cancelled", "canceled", "exclude", "excluded"}:
        return "cancelled"
    return None


def _normalize_deadline_ids(raw_ids: object) -> list[str]:
    values = raw_ids
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        docket_id = str(raw or "").strip()
        if not docket_id or docket_id in seen:
            continue
        seen.add(docket_id)
        out.append(docket_id)
    return out


def _deadline_can_update(
    docket_item: DocketItem,
    *,
    is_super: bool,
    is_manager: bool,
    staff_pid: str,
) -> bool:
    if is_super or is_manager:
        return True
    owner_pid = (getattr(docket_item, "owner_staff_party_id", None) or "").strip()
    return bool(staff_pid) and staff_pid == owner_pid


def _serialize_deadline_delete_payload(docket_item: DocketItem) -> dict[str, object]:
    return {
        "docket_id": str(docket_item.docket_id),
        "matter_id": str(docket_item.matter_id),
        "category": docket_item.category,
        "name_ref": docket_item.name_ref,
        "name_free": docket_item.name_free,
        "due_date": docket_item.due_date,
        "extended_due_date": docket_item.extended_due_date,
        "visible_from_date": docket_item.visible_from_date,
        "done_date": docket_item.done_date,
        "owner_staff_party_id": docket_item.owner_staff_party_id,
        "memo": docket_item.memo,
    }


def _create_deadline_deletion_log(docket_item: DocketItem) -> None:
    DeletionService().archive(
        docket_item,
        user_id=getattr(current_user, "id", None),
        tags=("manual", "deadline-route"),
    )


def _log_deadline_status_audit(
    docket_item: DocketItem,
    *,
    old_status: str,
    new_status: str,
    reason: str | None = None,
) -> None:
    try:
        from app.blueprints.billing_invoices.auth import log_audit

        payload = {
            "docket_id": docket_item.docket_id,
            "matter_id": docket_item.matter_id,
            "old_status": old_status,
            "new_status": new_status,
            "name": docket_item.name_free or docket_item.name_ref,
        }
        if reason and new_status == "cancelled":
            payload["reason"] = reason
        log_audit(
            "docket.status_change",
            "docket_item",
            None,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.routes.log_deadline_status_audit",
            log_key="deadline.routes.log_deadline_status_audit",
            log_window_seconds=300,
        )


def _log_deadline_delete_audit(audit_meta: dict[str, object]) -> None:
    try:
        from app.blueprints.billing_invoices.auth import log_audit

        log_audit(
            "docket.delete",
            "docket_item",
            None,
            json.dumps(audit_meta, ensure_ascii=False),
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.routes.log_deadline_delete_audit",
            log_key="deadline.routes.log_deadline_delete_audit",
            log_window_seconds=300,
        )


def _apply_deadline_status_update(
    docket_item: DocketItem,
    *,
    action: str,
    actor_id: int | None,
    reason: str = "",
    enqueue_sync: bool = True,
) -> tuple[str, str]:
    normalized = _normalize_deadline_action(action)
    if not normalized:
        raise ValueError("invalid status")

    previous_state, _previous_date = done_state(getattr(docket_item, "done_date", None))
    today_token = date.today().isoformat()

    if normalized == "done":
        docket_item.done_date = today_token
        clear_docket_manual_abandoned(docket_item)
    elif normalized == "cancelled":
        docket_item.done_date = f"AUTO_CANCELLED:{today_token}"
        mark_docket_manual_abandoned(docket_item, reason=reason or None, when=today_token)
    else:
        docket_item.done_date = None
        clear_docket_manual_abandoned(docket_item)

    if enqueue_sync:
        try:
            enqueue_docket_sync_for_item(docket_item=docket_item, actor_id=actor_id)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deadline.routes.apply_deadline_status_update.enqueue_sync",
                log_key="deadline.routes.apply_deadline_status_update.enqueue_sync",
                log_window_seconds=300,
            )

    return previous_state, normalized


# Shared due-date range filter (DB-side when possible).
def _apply_effective_due_date_range(q, start_date: date | None, end_date: date | None):
    try:
        dialect = getattr(db.engine.dialect, "name", "")
        due_text = effective_due_text_expr(DocketItem, dialect_name=dialect)
        if due_text is None:
            return q, None
        q = q.filter(due_text.isnot(None))

        if start_date:
            q = q.filter(due_text >= start_date.isoformat())
        if end_date:
            q = q.filter(due_text <= end_date.isoformat())

        q = q.order_by(due_text.asc(), DocketItem.docket_id.asc())
        return q, due_text
    except Exception:
        return q, None


def _due_in_range(di: DocketItem, start_date: date | None, end_date: date | None) -> bool:
    due = effective_due_for_work(
        getattr(di, "due_date", None),
        getattr(di, "extended_due_date", None),
    )
    if not due:
        return False
    if start_date and due < start_date:
        return False
    if end_date and due > end_date:
        return False
    return True


_VALID_DUE_AXES = {"all", "final", "internal"}


def _normalize_due_axis(value: object, *, default: str = "all") -> str:
    token = str(value or "").strip().lower()
    if token in _VALID_DUE_AXES:
        return token
    return default if default in _VALID_DUE_AXES else "all"


def _docket_date_text_expr(column, *, dialect_name: str | None = None):
    raw = func.nullif(func.trim(column), "")
    token = func.substr(raw, 1, 10)
    if dialect_name == "postgresql":
        is_date = token.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    else:
        is_date = token.like("____-__-__")
    return case((is_date, token), else_=None)


def _docket_due_text_exprs():
    dialect = getattr(db.engine.dialect, "name", "")
    base_due_text = _docket_date_text_expr(DocketItem.due_date, dialect_name=dialect)
    adjusted_due_text = _docket_date_text_expr(DocketItem.extended_due_date, dialect_name=dialect)
    final_due_text = case(
        (
            and_(
                base_due_text.isnot(None),
                adjusted_due_text.isnot(None),
                adjusted_due_text > base_due_text,
            ),
            adjusted_due_text,
        ),
        else_=base_due_text,
    )
    distinct_internal_due_text = case(
        (
            and_(
                adjusted_due_text.isnot(None),
                or_(base_due_text.is_(None), adjusted_due_text < base_due_text),
            ),
            adjusted_due_text,
        ),
        else_=None,
    )
    return final_due_text, distinct_internal_due_text


def _due_text_in_range(expr, start_date: date | None, end_date: date | None):
    conditions = [expr.isnot(None)]
    if start_date:
        conditions.append(expr >= start_date.isoformat())
    if end_date:
        conditions.append(expr <= end_date.isoformat())
    return and_(*conditions)


def _apply_docket_due_axis_range(
    q,
    start_date: date | None,
    end_date: date | None,
    *,
    due_axis: str,
):
    final_due_text, internal_due_text = _docket_due_text_exprs()

    if due_axis == "final":
        q = q.filter(_due_text_in_range(final_due_text, start_date, end_date))
        q = q.order_by(final_due_text.asc(), DocketItem.docket_id.asc())
        return q, final_due_text, internal_due_text, final_due_text

    if due_axis == "internal":
        q = q.filter(_due_text_in_range(internal_due_text, start_date, end_date))
        q = q.order_by(internal_due_text.asc(), DocketItem.docket_id.asc())
        return q, final_due_text, internal_due_text, internal_due_text

    all_due_condition = or_(
        _due_text_in_range(final_due_text, start_date, end_date),
        _due_text_in_range(internal_due_text, start_date, end_date),
    )
    primary_sort = case((internal_due_text.isnot(None), internal_due_text), else_=final_due_text)
    q = q.filter(all_due_condition)
    q = q.order_by(primary_sort.asc(), final_due_text.asc(), DocketItem.docket_id.asc())
    return q, final_due_text, internal_due_text, primary_sort


# Import category constants from central location
from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES


def _get_applicants_map(matter_ids: list) -> dict:
    """Get applicants for multiple matters in one query."""
    if not matter_ids:
        return {}
    try:
        from app.models.matter import MatterPartyRole

        # Directly join matter_party_role and party to bypass potentially broken view
        # Use case-insensitive check for role_code ('applicant' vs 'APPLICANT')
        dialect = getattr(db.engine.dialect, "name", "")
        agg_expr = (
            func.string_agg(Party.name_display, "; ")
            if dialect == "postgresql"
            else func.group_concat(Party.name_display, "; ")
        )
        rows = (
            db.session.query(MatterPartyRole.matter_id, agg_expr)
            .join(Party, Party.party_id == MatterPartyRole.party_id)
            .filter(MatterPartyRole.matter_id.in_(list(matter_ids)))
            .filter(func.lower(MatterPartyRole.role_code) == "applicant")
            .group_by(MatterPartyRole.matter_id)
            .all()
        )
        return dict(rows)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline._get_applicants_map",
            log_key="deadline._get_applicants_map",
            log_window_seconds=300,
        )
        return {}


_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_OA_MAIN_REF_RE = re.compile(r"^NOTICE:OA:([^:]+)$", re.IGNORECASE)
_OA_HANDLER_REF_RE = re.compile(r"^NOTICE:OA:([^:]+):HDL$", re.IGNORECASE)
_OA_MGMT_REF_RE = re.compile(r"^MGMT:NOTICE:OA:([^:]+)$", re.IGNORECASE)
_OA_USPTO_REF_RE = re.compile(r"^USPTO:([^:]+)$", re.IGNORECASE)
_DOCKET_CATEGORY_LABELS = {
    "ADMIN": "",
    "DEADLINE": "Task Deadline",
    "DOCKET": "Task Deadline",
    "EXAM": "Examination",
    "FILING": "Filing",
    "USPTO_NOTICE": "USPTO Notice",
    "USPTO_OA": "USPTO OA",
    "LEGAL": "Statutory Deadline",
    "MANAGEMENT": "",
    "MGMT": "",
    "MGMT_WORK": "/Task",
    "NOTICE": "Notice",
    "REG": "Registration",
    "SLA": "",
    "V2_LIMIT": "Legacy ",
    "WORK": "Task",
    "WORK_MGMT": "/Task",
}


def _calendar_event_oa_role(name_ref: str | None) -> tuple[str | None, str | None]:
    ref = (name_ref or "").strip()
    if not ref:
        return (None, None)

    match = _OA_MAIN_REF_RE.match(ref)
    if match:
        return ("main", (match.group(1) or "").strip())

    match = _OA_HANDLER_REF_RE.match(ref)
    if match:
        return ("handler", (match.group(1) or "").strip())

    match = _OA_MGMT_REF_RE.match(ref)
    if match:
        return ("mgmt", (match.group(1) or "").strip())

    match = _OA_USPTO_REF_RE.match(ref)
    if match:
        return ("uspto", (match.group(1) or "").strip())

    return (None, None)


def _dedupe_calendar_event_rows(rows: list) -> list:
    """
    Collapse OA helper docket rows for month-calendar readability.

    We keep the representative row visible for the current user's filtered result:
    - Prefer NOTICE:OA:<oa_id> when present
    - Otherwise keep MGMT:NOTICE:OA:<oa_id>
    - Hide helper rows such as USPTO:<oa_id> and NOTICE:OA:<oa_id>:HDL
    """

    if not rows:
        return []

    visible_main_keys: set[tuple[str, str]] = set()
    visible_mgmt_keys: set[tuple[str, str]] = set()

    for row in rows:
        docket = getattr(row, "DocketItem", None)
        if docket is None:
            continue
        role, oa_id = _calendar_event_oa_role(getattr(docket, "name_ref", None))
        matter_id = (getattr(docket, "matter_id", None) or "").strip()
        if not oa_id or not matter_id:
            continue
        key = (matter_id, oa_id)
        if role == "main":
            visible_main_keys.add(key)
        elif role == "mgmt":
            visible_mgmt_keys.add(key)

    deduped = []
    for row in rows:
        docket = getattr(row, "DocketItem", None)
        if docket is None:
            deduped.append(row)
            continue

        role, oa_id = _calendar_event_oa_role(getattr(docket, "name_ref", None))
        matter_id = (getattr(docket, "matter_id", None) or "").strip()
        if not role or not oa_id or not matter_id:
            deduped.append(row)
            continue

        key = (matter_id, oa_id)
        if role == "handler" and key in visible_main_keys:
            continue
        if role == "mgmt" and key in visible_main_keys:
            continue
        if role == "uspto" and (key in visible_main_keys or key in visible_mgmt_keys):
            continue

        deduped.append(row)

    return deduped


def _effective_due_token_for_docket(docket: DocketItem | None) -> str:
    if docket is None:
        return ""
    due = effective_due_for_work(
        getattr(docket, "due_date", None),
        getattr(docket, "extended_due_date", None),
    )
    return due.isoformat() if due else ""


def _legacy_v2_limit_dedupe_key(docket: DocketItem | None) -> tuple[str, str, str] | None:
    if docket is None:
        return None

    category = (getattr(docket, "category", None) or "").strip().upper()
    name_ref = (getattr(docket, "name_ref", None) or "").strip()
    title = (getattr(docket, "name_free", None) or "").strip()
    matter_id = str(getattr(docket, "matter_id", "") or "").strip()
    due_token = _effective_due_token_for_docket(docket)
    if category != "V2_LIMIT" or name_ref or not title or not matter_id or not due_token:
        return None
    return (matter_id, title, due_token)


def _status_red_dedupe_key(docket: DocketItem | None) -> tuple[str, str, str] | None:
    if docket is None:
        return None

    name_ref = (getattr(docket, "name_ref", None) or "").strip()
    title = (getattr(docket, "name_free", None) or "").strip()
    matter_id = str(getattr(docket, "matter_id", "") or "").strip()
    due_token = _effective_due_token_for_docket(docket)
    if (
        not name_ref.upper().startswith("MGMT:STATUS_RED:")
        or not title
        or not matter_id
        or not due_token
    ):
        return None
    return (matter_id, title, due_token)


def _is_deadline_list_hidden_reference_row(
    docket: DocketItem | None,
    *,
    visible_status_red_keys: set[tuple[str, str, str]] | None = None,
) -> bool:
    if docket is None:
        return False

    docket_id = (getattr(docket, "docket_id", None) or "").strip()
    raw_id = (getattr(docket, "raw_id", None) or "").strip()
    if docket_id.upper().startswith("WF-") or raw_id.upper().startswith("WF-"):
        return True

    name_ref = (getattr(docket, "name_ref", None) or "").strip()
    title = (getattr(docket, "name_free", None) or "").strip()
    if is_annuity_status_red_deadline(name_ref=name_ref, title=title):
        return True

    legacy_key = _legacy_v2_limit_dedupe_key(docket)
    if legacy_key and legacy_key in (visible_status_red_keys or set()):
        return True

    return False


def _dedupe_deadline_list_rows(rows: list) -> list:
    rows = _dedupe_calendar_event_rows(rows)
    visible_status_red_keys = {
        key
        for row in rows
        for key in [_status_red_dedupe_key(getattr(row, "DocketItem", None))]
        if key is not None
    }
    return [
        row
        for row in rows
        if not _is_deadline_list_hidden_reference_row(
            getattr(row, "DocketItem", None),
            visible_status_red_keys=visible_status_red_keys,
        )
    ]


_DOCKET_META_LABELS = {
    "auto": "Auto Create",
    "deadline_code": "Deadline ",
    "events": "",
    "locked": "Auto  ",
    "policy_id": " ID",
    "source": "",
    "status_red": "Status RED",
    "status_red_related_date": "Status RED reference date",
    "trigger": "",
}
_DOCKET_META_ORDER = (
    "auto",
    "locked",
    "trigger",
    "status_red",
    "status_red_related_date",
    "policy_id",
    "deadline_code",
    "source",
    "events",
)
_RELATED_DOCKET_STATUS_ORDER = {
    "overdue": 0,
    "new": 1,
    "done": 2,
    "cancelled": 3,
    "expired": 4,
}
_MATTER_STAFF_ROLE_GROUPS = {
    "attorney": "attorney",
    "retainer": "attorney",
    "handler": "handler",
    "staff": "handler",
    "draftsman": "handler",
    "manager": "manager",
    "mgmt": "manager",
}
_MATTER_STAFF_ROLE_LABELS = {
    "attorney": "Responsible attorney",
    "handler": "Handler",
    "manager": "Manager",
}


def _norm_date_str(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.strip("[](){}<>")
    m = _DATE_RE.search(s)
    if not m:
        return s
    d = m.group(1)
    if s.startswith(d) and len(s) > 10 and s[10] in ("T", " "):
        return s
    return d


def _docket_due_dates(docket_item: DocketItem) -> tuple[str | None, str | None]:
    final_due = adjusted_legal_due_for_docket(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    internal_due = internal_due_for_docket(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    return _norm_date_str(final_due), _norm_date_str(internal_due)


def _date_str_in_range(value: str | None, start_date: date | None, end_date: date | None) -> bool:
    parsed = _parse_date_str(value)
    if not parsed:
        return False
    if start_date and parsed < start_date:
        return False
    if end_date and parsed > end_date:
        return False
    return True


def _docket_status(item: DocketItem) -> str:
    state, _ = done_state(item.done_date)
    if state == "done":
        return "done"
    if state == "cancelled":
        return "cancelled"
    if state == "expired":
        return "expired"
    try:
        due_dt = effective_due_for_work(
            getattr(item, "due_date", None),
            getattr(item, "extended_due_date", None),
        )
    except Exception:
        return "new"
    if not due_dt:
        return "new"
    return "overdue" if due_dt < date.today() else "new"


def _safe_json_dict(value: str | None) -> dict | None:
    raw = (value or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _display_docket_category(category: str | None) -> str:
    raw = (category or "").strip()
    if not raw:
        return "-"
    return _DOCKET_CATEGORY_LABELS.get(raw.upper(), raw)


def _display_meta_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "" if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        scalar_items = [
            str(v).strip() for v in value if not isinstance(v, (dict, list)) and str(v).strip()
        ]
        if scalar_items:
            return ", ".join(scalar_items)
        return f"{len(value)}items Item" if value else None
    if isinstance(value, dict):
        return f"{len(value)}items " if value else None
    text = str(value).strip()
    return text or None


def _memo_metadata_rows(payload: dict | None) -> list[dict[str, str]]:
    if not payload:
        return []

    rows: list[dict[str, str]] = []
    emitted: set[str] = set()

    def _emit(key: str) -> None:
        if key in emitted:
            return
        value = _display_meta_value(payload.get(key))
        if not value:
            return
        emitted.add(key)
        rows.append(
            {
                "key": key,
                "label": _DOCKET_META_LABELS.get(key, key.replace("_", " ").title()),
                "value": value,
            }
        )

    for key in _DOCKET_META_ORDER:
        _emit(key)
    for key in payload.keys():
        _emit(str(key))

    return rows


def _append_staff_row(
    rows: list[dict[str, str]], *, party_id: str | None, name: str | None
) -> None:
    pid = (party_id or "").strip()
    label = (name or "").strip()
    if not pid and not label:
        return
    row = {"party_id": pid or "", "name": label or pid}
    if row in rows:
        return
    rows.append(row)


def _load_matter_staff(matter_id: str) -> dict[str, list[dict[str, str]]]:
    buckets = {"attorney": [], "handler": [], "manager": []}
    if not matter_id:
        return buckets

    try:
        rows = (
            db.session.query(
                MatterStaffAssignment.staff_role_code,
                MatterStaffAssignment.staff_party_id,
                Party.name_display,
            )
            .outerjoin(Party, Party.party_id == MatterStaffAssignment.staff_party_id)
            .filter(MatterStaffAssignment.matter_id == str(matter_id))
            .order_by(
                func.coalesce(MatterStaffAssignment.seq, 1).asc(),
                MatterStaffAssignment.msa_id.asc(),
            )
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline._load_matter_staff",
            log_key="deadline._load_matter_staff",
            log_window_seconds=300,
        )
        return buckets

    for role_code, party_id, name in rows:
        bucket = _MATTER_STAFF_ROLE_GROUPS.get(str(role_code or "").strip().lower())
        if not bucket:
            continue
        _append_staff_row(buckets[bucket], party_id=str(party_id or ""), name=str(name or ""))
    return buckets


def _workflow_user_name(user) -> str | None:
    if not user:
        return None
    return (
        str(getattr(user, "display_name", None) or "").strip()
        or str(getattr(user, "username", None) or "").strip()
        or str(getattr(user, "email", None) or "").strip()
        or None
    )


def _docket_origin_label(item: DocketItem, memo_payload: dict | None) -> str:
    raw_id = (getattr(item, "raw_id", None) or "").strip()
    if raw_id.startswith("LimitHistory:"):
        return "Legacy LimitHistory"
    if memo_payload and memo_payload.get("auto"):
        return "Auto Create"
    if (item.name_ref or "").strip():
        return " Deadline"
    return "User Deadline"


def _can_view_docket(item: DocketItem, *, user_role: str, staff_pid: str) -> bool:
    flags = resolve_role_scope(user_role)
    # Business super roles can view any docket item (still gated by can_access_matter at call sites).
    if flags.get("show_all_mgmt") and flags.get("show_all_work"):
        return True

    # Owners can always view their own docket items, even if category is outside known sets (legacy).
    if staff_pid and (item.owner_staff_party_id or "").strip() == staff_pid:
        return True

    cat_upper = (item.category or "").strip().upper()

    # Case managers can view WORK docket items for matters they manage,
    # even if they are not the owner (Manager WORK Deadline  ).
    if cat_upper in WORK_CATEGORIES and staff_pid:
        try:
            if is_manager_assigned_to_matter(current_user, str(item.matter_id)):
                return True
        except Exception as exc:
            # Do not silently swallow errors in access-control checks.
            report_swallowed_exception(
                exc,
                context="deadline._can_view_docket.is_manager_assigned_to_matter",
            )

    if cat_upper in MGMT_CATEGORIES:
        if flags["show_all_mgmt"]:
            return True
        if flags["show_own_mgmt"] and staff_pid:
            return (item.owner_staff_party_id or "").strip() == staff_pid

    if cat_upper in WORK_CATEGORIES:
        if flags["show_all_work"]:
            return True
        if flags["show_own_work"] and staff_pid:
            return (item.owner_staff_party_id or "").strip() == staff_pid

    return False


@bp.route("/")
@login_required
def index():
    return redirect(url_for("deadlines.calendar_month"))


@bp.route("/calendar/month")
@login_required
def calendar_month():
    return render_template("deadline/index.html", page="calendar_month")


@bp.route("/list")
@login_required
def list_view():
    mode = (request.args.get("mode") or "").strip().lower()
    if mode in ("internal", "close", "1", "true", "yes"):
        # Backward-compatible alias: keep old links working, but use a distinct URL for clarity.
        args = request.args.to_dict(flat=True)
        args.pop("mode", None)
        return redirect(url_for("deadlines.internal_close", **args))
    return render_template("deadline/index.html", page="list")


@bp.route("/internal")
@login_required
def internal_close():
    return render_template("deadline/index.html", page="internal")


@bp.route("/item/<string:docket_id>")
@login_required
def docket_detail(docket_id: str):
    d = DocketItem.query.get(docket_id)
    if not d or getattr(d, "is_deleted", False):
        abort(404)

    user_role = (current_user.role or "").strip()
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
    if not _can_view_docket(d, user_role=user_role, staff_pid=staff_pid):
        abort(403)
    from app.utils.permissions import can_access_matter

    if not can_access_matter(current_user, str(d.matter_id), action="view"):
        abort(403)

    matter = Matter.query.get(str(d.matter_id))
    if not matter:
        abort(404)

    owner = None
    if (d.owner_staff_party_id or "").strip():
        owner = Party.query.get(d.owner_staff_party_id)

    applicants = _get_applicants_map([matter.matter_id]).get(matter.matter_id)
    done_state_label, done_date = done_state(d.done_date)
    today = date.today()
    memo_payload = _safe_json_dict(d.memo)
    legal_due_date, internal_due_date = _docket_due_dates(d)
    visible_from_date = _norm_date_str(d.visible_from_date)
    effective_due = effective_due_for_work(d.due_date, d.extended_due_date)
    effective_due_date = effective_due.isoformat() if effective_due else None
    days_until_due = (effective_due - today).days if effective_due else None
    due_basis = None
    if internal_due_date:
        due_basis = "Internal/ Due date "
    elif legal_due_date:
        due_basis = "Final Due date "

    is_visible_now = is_visible_by_date(d, today=today)
    is_system_docket = bool((d.name_ref or "").strip()) or bool(
        memo_payload and memo_payload.get("auto")
    )
    is_auto_docket = bool(memo_payload and memo_payload.get("auto"))
    is_locked = bool(memo_payload and memo_payload.get("locked"))
    is_legacy_reference = (
        str(d.category or "").strip().upper() == "V2_LIMIT"
        and not legal_due_date
        and not internal_due_date
    )

    linked_workflows: list[dict[str, str | int | None]] = []
    try:
        workflow_rows = (
            Workflow.query.filter(
                Workflow.case_id == str(matter.matter_id),
                Workflow.business_code.like(f"DOCKET:{d.docket_id}%"),
            )
            .order_by(Workflow.id.asc())
            .all()
        )
        for wf in workflow_rows:
            linked_workflows.append(
                {
                    "id": int(wf.id),
                    "title": strip_workflow_deadline_title_suffix(wf.name) or "Task",
                    "status": (wf.status or "").strip() or "-",
                    "due_date": wf.due_date.isoformat() if getattr(wf, "due_date", None) else None,
                    "completed_date": (
                        wf.completed_date.isoformat()
                        if getattr(wf, "completed_date", None)
                        else None
                    ),
                    "assignee_name": _workflow_user_name(getattr(wf, "assignee", None)),
                    "attorney_name": _workflow_user_name(getattr(wf, "attorney_assignee", None)),
                    "manager_name": _workflow_user_name(getattr(wf, "inspector", None)),
                }
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.docket_detail.linked_workflows",
            log_key="deadline.docket_detail.linked_workflows",
            log_window_seconds=300,
        )

    linked_worklogs: list[dict[str, str | int | None]] = []
    try:
        worklog_rows = (
            WorkLog.query.filter(WorkLog.docket_id == str(d.docket_id))
            .order_by(WorkLog.updated_at.desc(), WorkLog.id.desc())
            .all()
        )
        owner_ids = sorted(
            {
                str(getattr(row, "owner_staff_party_id", "") or "").strip()
                for row in worklog_rows
                if str(getattr(row, "owner_staff_party_id", "") or "").strip()
            }
        )
        owner_name_map = {}
        if owner_ids:
            owner_name_map = {
                str(p.party_id): str(p.name_display or "").strip()
                for p in Party.query.filter(Party.party_id.in_(owner_ids)).all()
            }
        for wl in worklog_rows:
            owner_pid = str(getattr(wl, "owner_staff_party_id", "") or "").strip()
            linked_worklogs.append(
                {
                    "id": int(wl.id),
                    "status": (wl.status or "").strip() or "-",
                    "task_name": (
                        (wl.task_name or "").strip() or (wl.description or "").strip() or "Task"
                    ),
                    "due_date": wl.due_date.isoformat() if getattr(wl, "due_date", None) else None,
                    "description": (wl.description or "").strip() or None,
                    "owner_name": (
                        owner_name_map.get(owner_pid)
                        or (wl.snapshot_handler or "").strip()
                        or (wl.snapshot_attorney or "").strip()
                        or (wl.snapshot_manager or "").strip()
                        or None
                    ),
                }
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.docket_detail.linked_worklogs",
            log_key="deadline.docket_detail.linked_worklogs",
            log_window_seconds=300,
        )

    case_staff = _load_matter_staff(str(matter.matter_id))
    snapshot_staff = [
        {"label": "Responsible attorney", "name": (d.snapshot_attorney or "").strip()},
        {"label": "Handler", "name": (d.snapshot_handler or "").strip()},
        {"label": "Manager", "name": (d.snapshot_manager or "").strip()},
    ]
    snapshot_staff = [row for row in snapshot_staff if row["name"]]

    related_deadlines: list[dict[str, str | None]] = []
    try:
        sibling_rows = DocketItem.query.filter(DocketItem.matter_id == str(matter.matter_id)).all()
        for sibling in sibling_rows:
            if str(getattr(sibling, "docket_id", "") or "").strip() == str(d.docket_id):
                continue
            if bool(getattr(sibling, "is_deleted", False)):
                continue
            sibling_effective_due = effective_due_for_work(
                getattr(sibling, "due_date", None),
                getattr(sibling, "extended_due_date", None),
            )
            related_deadlines.append(
                {
                    "docket_id": str(getattr(sibling, "docket_id", "") or "").strip(),
                    "title": (
                        str(getattr(sibling, "name_free", "") or "").strip()
                        or str(getattr(sibling, "name_ref", "") or "").strip()
                        or "(Title None)"
                    ),
                    "category_label": _display_docket_category(getattr(sibling, "category", None)),
                    "effective_due": (
                        sibling_effective_due.isoformat() if sibling_effective_due else None
                    ),
                    "status": _docket_status(sibling),
                }
            )
        related_deadlines.sort(
            key=lambda row: (
                _RELATED_DOCKET_STATUS_ORDER.get(str(row.get("status") or "new"), 9),
                row.get("effective_due") is None,
                row.get("effective_due") or "9999-12-31",
                row.get("title") or "",
            )
        )
        related_deadlines = related_deadlines[:6]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.docket_detail.related_deadlines",
            log_key="deadline.docket_detail.related_deadlines",
            log_window_seconds=300,
        )

    deadline_audit_rows = load_audit_rows_by_meta_value(
        target_type="docket_item",
        meta_key="docket_id",
        meta_value=str(d.docket_id),
        limit=12,
    )

    return render_template(
        "deadline/detail.html",
        docket=d,
        matter=matter,
        owner=owner,
        applicants=applicants,
        status=_docket_status(d),
        done_state=done_state_label,
        done_date=done_date,
        due_date=legal_due_date,
        internal_due_date=internal_due_date,
        visible_from_date=visible_from_date,
        effective_due_date=effective_due_date,
        days_until_due=days_until_due,
        due_basis=due_basis,
        display_category=_display_docket_category(d.category),
        is_visible_now=is_visible_now,
        is_system_docket=is_system_docket,
        is_auto_docket=is_auto_docket,
        is_locked=is_locked,
        is_legacy_reference=is_legacy_reference,
        linked_workflows=linked_workflows,
        linked_worklogs=linked_worklogs,
        case_staff=case_staff,
        case_staff_role_labels=_MATTER_STAFF_ROLE_LABELS,
        snapshot_staff=snapshot_staff,
        memo_payload=memo_payload,
        memo_metadata_rows=_memo_metadata_rows(memo_payload),
        memo_plain=(None if memo_payload else ((d.memo or "").strip() or None)),
        related_deadlines=related_deadlines,
        source_label=_docket_origin_label(d, memo_payload),
        deadline_audit_rows=deadline_audit_rows,
    )


# --- JSON APIs ---


@bp.route("/api/deadlines", methods=["GET", "POST"])
@login_required
def api_deadlines():
    if request.method == "POST":
        if not can_manage_case_globally(current_user):
            return jsonify({"error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}
        matter_id = str(data.get("matter_id") or data.get("case_id") or "").strip()
        title = (data.get("title") or "").strip()
        due_raw = (data.get("due_date") or "").strip() or None
        internal_raw = (data.get("internal_due_date") or "").strip() or None
        if not matter_id or not title or not (due_raw or internal_raw):
            return (
                jsonify({"error": "matter_id, title and due_date or internal_due_date required"}),
                400,
            )

        matter = Matter.query.get(matter_id)
        if not matter:
            return jsonify({"error": "invalid matter_id"}), 400

        visible_from_raw = (data.get("visible_from_date") or "").strip() or None
        if due_raw and not _parse_date_str(due_raw):
            return jsonify({"error": "invalid due_date"}), 400
        if internal_raw and not _parse_date_str(internal_raw):
            return jsonify({"error": "invalid internal_due_date"}), 400
        if visible_from_raw and not _parse_date_str(visible_from_raw):
            return jsonify({"error": "invalid visible_from_date"}), 400
        effective_due = internal_raw or due_raw
        if visible_from_raw and effective_due and visible_from_raw > effective_due:
            return (
                jsonify({"error": "visible_from_date must be on or before effective due date"}),
                400,
            )
        # Determine category based on assignee (none provided here, so defaults to WORK)
        from app.utils.task_classification import determine_category_by_staff_role

        save_category = determine_category_by_staff_role(str(matter.matter_id), assignee_id=None)

        di = DocketItem(
            matter_id=str(matter.matter_id),
            category=save_category,
            name_free=title,
            due_date=due_raw,
            extended_due_date=internal_raw,
            visible_from_date=visible_from_raw,
            memo=(data.get("notes") or None),
        )
        db.session.add(di)
        db.session.commit()

        try:
            import json

            from app.blueprints.billing_invoices.auth import log_audit

            log_audit(
                "docket.create",
                "docket_item",
                None,
                json.dumps(
                    {
                        "docket_id": di.docket_id,
                        "matter_id": di.matter_id,
                        "title": di.name_free or di.name_ref,
                        "category": di.category,
                        "due_date": di.due_date,
                        "internal_due_date": di.extended_due_date,
                        "visible_from_date": di.visible_from_date,
                        "notes": di.memo,
                        "owner_staff_party_id": di.owner_staff_party_id,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception as e:
            current_app.logger.warning(f"Failed to log docket create: {e}")

        return jsonify({"id": di.docket_id, "matter_id": str(di.matter_id)}), 201
    include_done = (request.args.get("include_done") or "").lower() in (
        "1",
        "true",
        "yes",
    )

    mine_only = (request.args.get("mine") or "").lower() in ("1", "true", "yes")
    category = (request.args.get("category") or "").strip()
    filter_mode = (request.args.get("filter") or "").strip().lower()
    owner_filter = (request.args.get("owner") or "").strip()
    date_filter = _parse_date_str(request.args.get("date"))
    due_axis = _normalize_due_axis(request.args.get("due_axis"), default="all")

    # Pagination (GET only). Defaults are tuned to keep list views fast.
    def _int_arg(key: str, default: int) -> int:
        try:
            raw = (request.args.get(key) or "").strip()
            if not raw:
                return int(default)
            return int(raw)
        except Exception:
            return int(default)

    page = max(1, _int_arg("page", 1))
    per_page = _int_arg("per_page", _int_arg("limit", 200))
    per_page = max(1, min(500, per_page))
    offset = (page - 1) * per_page

    def _apply_filters(q):
        today = date.today()

        # Security: restrict to matters the current user can view.
        try:
            accessible_matter_ids = policy_accessible_matter_ids_select(current_user)
            q = q.filter(DocketItem.matter_id.in_(accessible_matter_ids))
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deadline.routes.api_deadlines.accessible_matter_ids",
                log_key="deadline.routes.api_deadlines.accessible_matter_ids",
                log_window_seconds=300,
            )
            from sqlalchemy import false

            return q.filter(false())

        # Hide soft-deleted rows (if present).
        if hasattr(DocketItem, "is_deleted"):
            q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        if hasattr(Matter, "is_deleted"):
            q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))

        dialect = getattr(db.engine.dialect, "name", "")
        due_text = effective_due_text_expr(DocketItem, dialect_name=dialect)
        final_due_text, internal_due_text = _docket_due_text_exprs()
        if due_axis == "final":
            primary_due_text = final_due_text
        elif due_axis == "internal":
            primary_due_text = internal_due_text
        else:
            primary_due_text = due_text

        if primary_due_text is not None:
            q = q.filter(primary_due_text.isnot(None))
        else:
            q = q.filter(or_(final_due_text.isnot(None), internal_due_text.isnot(None)))

        effective_due_date = func.date(primary_due_text) if primary_due_text is not None else None
        q = q.filter(visible_on_or_before(DocketItem, target_date=today))

        # Handle new 'filter' param
        if filter_mode == "done":
            pass  # include everything, will filter for done laterNew No, usually done items have done_date
            # The logic below for include_done checks "is_(None) | == ''"

        if filter_mode == "todo":
            q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))
        elif filter_mode == "done":
            q = q.filter(DocketItem.done_date.isnot(None), DocketItem.done_date != "")
        elif filter_mode == "overdue":
            q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))
            if effective_due_date is not None:
                q = q.filter(effective_due_date < today)
        elif filter_mode == "due7":
            # 7 days from today
            q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))
            next7 = today + timedelta(days=7)
            if effective_due_date is not None:
                q = q.filter(effective_due_date >= today, effective_due_date <= next7)
        elif filter_mode == "internal":
            q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))
            q = q.filter(internal_due_text.isnot(None))
        elif filter_mode == "all":
            pass  # No filtering on done/not done
        else:
            # Fallback to old boolean flag
            if not include_done and not filter_mode:
                q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))

        if category:
            q = q.filter(DocketItem.category == category)

        # Additional filters (deep-linking)
        if date_filter:
            target = date_filter.isoformat()
            if due_axis == "final":
                q = q.filter(final_due_text == target)
            elif due_axis == "internal":
                q = q.filter(internal_due_text == target)
            else:
                q = q.filter(or_(final_due_text == target, internal_due_text == target))

        if owner_filter and not mine_only:
            q = q.filter(DocketItem.owner_staff_party_id == owner_filter)

        # [MODIFIED] Role-Based Visibility Logic
        user_role = (current_user.role or "").strip()
        staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()

        flags = resolve_role_scope(user_role)
        show_all_mgmt = flags["show_all_mgmt"]
        show_all_work = flags["show_all_work"]
        show_own_mgmt = flags["show_own_mgmt"]
        show_own_work = flags["show_own_work"]

        # If "mine_only" is explicitly requested, we narrow down from what they are allowed to see
        # But if not requested, we must still restrict to what they are allowed to see.

        # Business super roles can see all docket items (still limited to accessible matters above).
        if not (show_all_mgmt and show_all_work):
            visibility_conditions = []
            if staff_pid:
                # Always include "my" docket items regardless of category (legacy safety net).
                visibility_conditions.append(DocketItem.owner_staff_party_id == staff_pid)

            cat_upper = func.upper(DocketItem.category)
            if show_all_mgmt:
                visibility_conditions.append(cat_upper.in_(MGMT_CATEGORIES))
            elif show_own_mgmt and staff_pid:
                from sqlalchemy import and_

                visibility_conditions.append(
                    and_(
                        cat_upper.in_(MGMT_CATEGORIES),
                        DocketItem.owner_staff_party_id == staff_pid,
                    )
                )

            if show_all_work:
                visibility_conditions.append(cat_upper.in_(WORK_CATEGORIES))
            elif show_own_work and staff_pid:
                from sqlalchemy import and_

                visibility_conditions.append(
                    and_(
                        cat_upper.in_(WORK_CATEGORIES),
                        DocketItem.owner_staff_party_id == staff_pid,
                    )
                )

            # Case managers can view WORK docket items for matters they manage.
            if staff_pid and not show_all_work:
                from sqlalchemy import and_

                try:
                    managed_ids = managed_matter_ids_select(current_user)
                    visibility_conditions.append(
                        and_(cat_upper.in_(WORK_CATEGORIES), DocketItem.matter_id.in_(managed_ids))
                    )
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="deadline._apply_filters.managed_matter_ids_select",
                    )

            if visibility_conditions:
                q = q.filter(or_(*visibility_conditions))
            else:
                # See nothing
                from sqlalchemy import false

                q = q.filter(false())

        if mine_only:
            if staff_pid:
                q = q.filter(DocketItem.owner_staff_party_id == staff_pid)
            else:
                q = q.filter(DocketItem.owner_staff_party_id == "__none__")
        return q

    # Best-effort join to Party for assignee display
    rows = None
    joined_party = True
    try:
        q = db.session.query(DocketItem, Matter, Party).join(
            Matter, DocketItem.matter_id == Matter.matter_id
        )
        q = q.outerjoin(Party, Party.party_id == DocketItem.owner_staff_party_id)
        q = _apply_filters(q)
        if due_axis == "final":
            due_text = _docket_due_text_exprs()[0]
        elif due_axis == "internal":
            due_text = _docket_due_text_exprs()[1]
        else:
            due_text = effective_due_text_expr(
                DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
            )
        rows = (
            q.order_by(due_text.asc(), DocketItem.docket_id.asc())
            .limit(min(max(per_page * 3, per_page + 32), 1500))
            .offset(offset)
            .all()
        )
    except Exception:
        joined_party = False
        q = db.session.query(DocketItem, Matter).join(
            Matter, DocketItem.matter_id == Matter.matter_id
        )
        q = _apply_filters(q)
        if due_axis == "final":
            due_text = _docket_due_text_exprs()[0]
        elif due_axis == "internal":
            due_text = _docket_due_text_exprs()[1]
        else:
            due_text = effective_due_text_expr(
                DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
            )
        rows = (
            q.order_by(due_text.asc(), DocketItem.docket_id.asc())
            .limit(min(max(per_page * 3, per_page + 32), 1500))
            .offset(offset)
            .all()
        )

    rows = _dedupe_deadline_list_rows(rows or [])
    has_more = bool(len(rows) > per_page)
    if has_more:
        rows = rows[:per_page]

    # Get applicants map separately to avoid duplicate rows
    matter_ids = list({r.Matter.matter_id for r in rows})
    applicants_map = _get_applicants_map(matter_ids)
    is_super = can_manage_case_globally(current_user)
    is_manager = is_manager_role(current_user)
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()

    out = []
    if joined_party:
        for r in rows:
            final_due_date, internal_due_date = _docket_due_dates(r.DocketItem)
            can_update = _deadline_can_update(
                r.DocketItem,
                is_super=is_super,
                is_manager=is_manager,
                staff_pid=staff_pid,
            )
            out.append(
                {
                    "id": r.DocketItem.docket_id,
                    "case_id": r.Matter.matter_id,
                    "case_ref": r.Matter.our_ref,
                    "case_title": r.Matter.right_name,
                    "applicants": applicants_map.get(r.Matter.matter_id),
                    "case_manager_id": None,
                    "case_attorney_id": None,
                    "title": r.DocketItem.name_free or r.DocketItem.name_ref or "",
                    "type": r.DocketItem.category,
                    "due_date": final_due_date,
                    "internal_due_date": internal_due_date,
                    "visible_from_date": _norm_date_str(r.DocketItem.visible_from_date),
                    "status": _docket_status(r.DocketItem),
                    "assigned_to": r.DocketItem.owner_staff_party_id,
                    "assigned_to_name": (r.Party.name_display if r.Party else None),
                    "priority": None,
                    "notes": r.DocketItem.memo,
                    "can_update": can_update,
                    "selectable": can_update,
                    "url": url_for(
                        "case_work.case_detail",
                        case_id=r.Matter.matter_id,
                        docket_id=r.DocketItem.docket_id,
                        _anchor="sec-deadlines",
                    ),
                    "detail_url": url_for(
                        "deadlines.docket_detail", docket_id=r.DocketItem.docket_id
                    ),
                    "case_deadlines_url": url_for(
                        "case_work.case_detail",
                        case_id=r.Matter.matter_id,
                        docket_id=r.DocketItem.docket_id,
                        _anchor="sec-deadlines",
                    ),
                }
            )
    else:
        for r in rows:
            final_due_date, internal_due_date = _docket_due_dates(r.DocketItem)
            can_update = _deadline_can_update(
                r.DocketItem,
                is_super=is_super,
                is_manager=is_manager,
                staff_pid=staff_pid,
            )
            out.append(
                {
                    "id": r.DocketItem.docket_id,
                    "case_id": r.Matter.matter_id,
                    "case_ref": r.Matter.our_ref,
                    "case_title": r.Matter.right_name,
                    "applicants": applicants_map.get(r.Matter.matter_id),
                    "case_manager_id": None,
                    "case_attorney_id": None,
                    "title": r.DocketItem.name_free or r.DocketItem.name_ref or "",
                    "type": r.DocketItem.category,
                    "due_date": final_due_date,
                    "internal_due_date": internal_due_date,
                    "visible_from_date": _norm_date_str(r.DocketItem.visible_from_date),
                    "status": _docket_status(r.DocketItem),
                    "assigned_to": r.DocketItem.owner_staff_party_id,
                    "assigned_to_name": None,
                    "priority": None,
                    "notes": r.DocketItem.memo,
                    "can_update": can_update,
                    "selectable": can_update,
                    "url": url_for(
                        "case_work.case_detail",
                        case_id=r.Matter.matter_id,
                        docket_id=r.DocketItem.docket_id,
                        _anchor="sec-deadlines",
                    ),
                    "detail_url": url_for(
                        "deadlines.docket_detail", docket_id=r.DocketItem.docket_id
                    ),
                    "case_deadlines_url": url_for(
                        "case_work.case_detail",
                        case_id=r.Matter.matter_id,
                        docket_id=r.DocketItem.docket_id,
                        _anchor="sec-deadlines",
                    ),
                }
            )
    resp = jsonify(out)
    resp.headers["X-Page"] = str(page)
    resp.headers["X-Per-Page"] = str(per_page)
    resp.headers["X-Has-More"] = "1" if has_more else "0"
    if has_more:
        resp.headers["X-Next-Page"] = str(page + 1)
    return resp


@bp.route("/api/deadlines/bulk", methods=["POST"])
@login_required
def api_deadlines_bulk():
    payload = request.get_json(silent=True) or {}
    docket_ids = _normalize_deadline_ids(payload.get("ids") or payload.get("docket_ids"))
    action = _normalize_deadline_action(payload.get("action") or payload.get("status"))
    reason = str(payload.get("reason") or "").strip()

    if not docket_ids:
        return jsonify({"error": "no_valid_ids"}), 400
    if not action:
        return jsonify({"error": "invalid_action"}), 400

    is_super = can_manage_case_globally(current_user)
    is_manager = is_manager_role(current_user)
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
    actor_id = int(getattr(current_user, "id", 0) or 0) or None

    docket_query = DocketItem.query.filter(DocketItem.docket_id.in_(docket_ids))
    if hasattr(DocketItem, "is_deleted"):
        docket_query = docket_query.filter(
            or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
        )
    docket_rows = docket_query.all()
    docket_by_id = {str(row.docket_id): row for row in docket_rows}

    processed_ids: list[str] = []
    missing_ids: list[str] = []
    forbidden_ids: list[str] = []
    audit_entries: list[tuple[DocketItem, str, str, str]] = []

    for docket_id in docket_ids:
        docket_item = docket_by_id.get(docket_id)
        if not docket_item:
            missing_ids.append(docket_id)
            continue
        if not _deadline_can_update(
            docket_item,
            is_super=is_super,
            is_manager=is_manager,
            staff_pid=staff_pid,
        ):
            forbidden_ids.append(docket_id)
            continue
        try:
            old_status, new_status = _apply_deadline_status_update(
                docket_item,
                action=action,
                actor_id=actor_id,
                reason=reason,
            )
        except ValueError:
            db.session.rollback()
            return jsonify({"error": "invalid_action"}), 400
        processed_ids.append(docket_id)
        audit_entries.append((docket_item, old_status, new_status, reason))

    if not processed_ids:
        db.session.rollback()
        status_code = 403 if forbidden_ids else 404 if missing_ids else 400
        return (
            jsonify(
                {
                    "error": "no_permitted_deadlines",
                    "processed_count": 0,
                    "forbidden_ids": forbidden_ids,
                    "missing_ids": missing_ids,
                }
            ),
            status_code,
        )

    db.session.commit()

    for docket_item, old_status, new_status, audit_reason in audit_entries:
        _log_deadline_status_audit(
            docket_item,
            old_status=old_status,
            new_status=new_status,
            reason=audit_reason,
        )

    return jsonify(
        {
            "success": True,
            "action": action,
            "processed_count": len(processed_ids),
            "processed_ids": processed_ids,
            "forbidden_count": len(forbidden_ids),
            "forbidden_ids": forbidden_ids,
            "missing_count": len(missing_ids),
            "missing_ids": missing_ids,
        }
    )


@bp.route("/api/deadlines/<did>", methods=["PATCH", "DELETE"])
@login_required
def api_deadline_detail(did: str):
    d = DocketItem.query.get(did)
    if not d or getattr(d, "is_deleted", False):
        return jsonify({"error": "not found"}), 404

    is_super = can_manage_case_globally(current_user)
    is_manager = is_manager_role(current_user)
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
    is_owner = bool(staff_pid) and staff_pid == ((d.owner_staff_party_id or "").strip())

    if request.method == "DELETE":
        if not is_super:
            return jsonify({"error": "forbidden"}), 403
        audit_meta = {
            "docket_id": str(d.docket_id),
            "matter_id": str(d.matter_id),
            "title": d.name_free or d.name_ref,
            "category": d.category,
            "due_date": d.due_date,
            "internal_due_date": d.extended_due_date,
            "visible_from_date": d.visible_from_date,
            "done_date": d.done_date,
            "owner_staff_party_id": d.owner_staff_party_id,
            "memo": d.memo,
        }
        try:
            _create_deadline_deletion_log(d)
        except Exception as exc:
            db.session.rollback()
            report_swallowed_exception(
                exc,
                context="deadline.routes.api_deadline_detail.deletion_log",
                log_key="deadline.routes.api_deadline_detail.deletion_log",
                log_window_seconds=300,
            )
        db.session.delete(d)
        db.session.commit()
        _log_deadline_delete_audit(audit_meta)
        return jsonify({"success": True})

    # PATCH
    if not (is_super or is_manager or is_owner):
        return jsonify({"error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    pre_update = {
        "title": d.name_free or d.name_ref,
        "category": d.category,
        "due_date": d.due_date,
        "internal_due_date": d.extended_due_date,
        "visible_from_date": d.visible_from_date,
        "notes": d.memo,
    }

    if is_super:
        if "title" in data:
            d.name_free = (data.get("title") or "").strip() or d.name_free
        if "type" in data:
            d.category = (data.get("type") or "").strip() or d.category
        if "notes" in data:
            d.memo = data.get("notes")

    status_change: tuple[str, str] | None = None
    status_reason = ""
    if "status" in data:
        status = (data.get("status") or "").strip().lower()
        status_reason = str(data.get("reason") or "").strip()
        normalized_status = _normalize_deadline_action(status)
        if not normalized_status:
            return jsonify({"error": "invalid status"}), 400
        status_change = _apply_deadline_status_update(
            d,
            action=normalized_status,
            actor_id=int(getattr(current_user, "id", 0) or 0) or None,
            reason=status_reason,
            enqueue_sync=False,
        )

    if "due_date" in data and is_super:
        raw = (data.get("due_date") or "").strip()
        if raw and not _parse_date_str(raw):
            return jsonify({"error": "invalid due_date"}), 400
        d.due_date = raw or None
    if "internal_due_date" in data and is_super:
        raw = (data.get("internal_due_date") or "").strip()
        if raw and not _parse_date_str(raw):
            return jsonify({"error": "invalid internal_due_date"}), 400
        d.extended_due_date = raw or None
    if "visible_from_date" in data:
        if is_super:
            raw = (data.get("visible_from_date") or "").strip()
            if raw and not _parse_date_str(raw):
                return jsonify({"error": "invalid visible_from_date"}), 400
            d.visible_from_date = raw or None

    effective_due = (d.extended_due_date or "").strip() or (d.due_date or "").strip() or None
    visible_from = (d.visible_from_date or "").strip() or None
    if effective_due and visible_from and visible_from > effective_due:
        return jsonify({"error": "visible_from_date must be on or before effective due date"}), 400

    # Commit main DB first to release lock
    try:
        enqueue_docket_sync_for_item(docket_item=d, actor_id=current_user.id)
    except Exception as exc:
        # Best-effort: sync enqueue should not block status updates.
        report_swallowed_exception(
            exc,
            context="deadline.routes.update_docket.enqueue_sync",
            log_key="deadline.routes.update_docket.enqueue_sync",
            log_window_seconds=300,
        )

    db.session.commit()

    try:
        changes = {}
        if "title" in data:
            new_title = d.name_free or d.name_ref
            if pre_update["title"] != new_title:
                changes["title"] = {"from": pre_update["title"], "to": new_title}
        if "type" in data:
            if pre_update["category"] != d.category:
                changes["category"] = {
                    "from": pre_update["category"],
                    "to": d.category,
                }
        if "due_date" in data:
            if pre_update["due_date"] != d.due_date:
                changes["due_date"] = {
                    "from": pre_update["due_date"],
                    "to": d.due_date,
                }
        if "internal_due_date" in data:
            if pre_update["internal_due_date"] != d.extended_due_date:
                changes["internal_due_date"] = {
                    "from": pre_update["internal_due_date"],
                    "to": d.extended_due_date,
                }
        if "notes" in data:
            if pre_update["notes"] != d.memo:
                changes["notes"] = {"from": pre_update["notes"], "to": d.memo}
        if changes:
            import json

            from app.blueprints.billing_invoices.auth import log_audit

            log_audit(
                "docket.update",
                "docket_item",
                None,
                json.dumps(
                    {
                        "docket_id": d.docket_id,
                        "matter_id": d.matter_id,
                        "changes": changes,
                    },
                    ensure_ascii=False,
                ),
            )
    except Exception as e:
        current_app.logger.warning(f"Failed to log docket update: {e}")

    # Post-commit operations: audit_log and calendar sync (avoid DB lock)
    if status_change:
        old_status, new_status = status_change
        _log_deadline_status_audit(
            d,
            old_status=old_status,
            new_status=new_status,
            reason=status_reason,
        )

    return jsonify({"success": True})


@bp.route("/api/events")
@login_required
def api_events():
    # Provide FullCalendar-compatible events from deadlines
    include_done = (request.args.get("include_done") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    mine_only = (request.args.get("mine") or "").lower() in ("1", "true", "yes")
    due_axis = _normalize_due_axis(request.args.get("due_axis"), default="all")
    today = date.today()
    default_window_days = 60
    start_date = _parse_date_str(request.args.get("start")) or (
        today - timedelta(days=default_window_days)
    )
    end_date = _parse_date_str(request.args.get("end")) or (
        today + timedelta(days=default_window_days)
    )
    try:
        requested_limit = int(request.args.get("limit", 5000))
    except Exception:
        requested_limit = 5000
    limit = max(1, min(requested_limit, 5000))

    q = db.session.query(DocketItem, Matter).join(Matter, DocketItem.matter_id == Matter.matter_id)

    # Security: restrict to matters the current user can view.
    try:
        accessible_matter_ids = policy_accessible_matter_ids_select(current_user)
        q = q.filter(DocketItem.matter_id.in_(accessible_matter_ids))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.routes.api_events.accessible_matter_ids",
            log_key="deadline.routes.api_events.accessible_matter_ids",
            log_window_seconds=300,
        )
        from sqlalchemy import false

        q = q.filter(false())

    if hasattr(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    if hasattr(Matter, "is_deleted"):
        q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))

    due_text = effective_due_text_expr(
        DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
    )
    q = q.filter(due_text.isnot(None))
    if not include_done:
        q = q.filter((DocketItem.done_date.is_(None)) | (DocketItem.done_date == ""))

    # [MODIFIED] Role-Based Visibility Logic
    user_role = (current_user.role or "").strip()
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()

    flags = resolve_role_scope(user_role)
    show_all_mgmt = flags["show_all_mgmt"]
    show_all_work = flags["show_all_work"]
    show_own_mgmt = flags["show_own_mgmt"]
    show_own_work = flags["show_own_work"]

    visibility_conditions = []
    cat_upper = func.upper(DocketItem.category)
    if staff_pid:
        # Always include "my" docket items regardless of category (legacy safety net).
        visibility_conditions.append(DocketItem.owner_staff_party_id == staff_pid)
    if show_all_mgmt:
        visibility_conditions.append(cat_upper.in_(MGMT_CATEGORIES))
    elif show_own_mgmt:
        from sqlalchemy import and_

        visibility_conditions.append(
            and_(cat_upper.in_(MGMT_CATEGORIES), DocketItem.owner_staff_party_id == staff_pid)
        )

    if show_all_work:
        visibility_conditions.append(cat_upper.in_(WORK_CATEGORIES))
    elif show_own_work:
        from sqlalchemy import and_

        visibility_conditions.append(
            and_(cat_upper.in_(WORK_CATEGORIES), DocketItem.owner_staff_party_id == staff_pid)
        )

    # Case managers can view WORK docket items for matters they manage.
    if staff_pid and not show_all_work:
        from sqlalchemy import and_

        try:
            managed_ids = managed_matter_ids_select(current_user)
            visibility_conditions.append(
                and_(cat_upper.in_(WORK_CATEGORIES), DocketItem.matter_id.in_(managed_ids))
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deadline.api.calendar.managed_matter_ids_select",
            )

    if not (show_all_mgmt and show_all_work):
        if visibility_conditions:
            q = q.filter(or_(*visibility_conditions))
        else:
            from sqlalchemy import false

            q = q.filter(false())

    q, final_due_text, internal_due_text, _primary_due_text = _apply_docket_due_axis_range(
        q,
        start_date,
        end_date,
        due_axis=due_axis,
    )

    # Optional owner filter (deep-linking)
    owner_filter = (request.args.get("owner") or "").strip()
    if owner_filter and not mine_only:
        q = q.filter(DocketItem.owner_staff_party_id == owner_filter)
    if mine_only:
        if staff_pid:
            q = q.filter(DocketItem.owner_staff_party_id == staff_pid)
        else:
            q = q.filter(DocketItem.owner_staff_party_id == "__none__")
    rows = _dedupe_calendar_event_rows(q.limit(limit).all())
    if due_axis == "final":
        rows = [
            r
            for r in rows
            if _date_str_in_range(_docket_due_dates(r.DocketItem)[0], start_date, end_date)
        ]
    elif due_axis == "internal":
        rows = [
            r
            for r in rows
            if _date_str_in_range(_docket_due_dates(r.DocketItem)[1], start_date, end_date)
        ]
    else:
        rows = [
            r
            for r in rows
            if _date_str_in_range(_docket_due_dates(r.DocketItem)[0], start_date, end_date)
            or _date_str_in_range(_docket_due_dates(r.DocketItem)[1], start_date, end_date)
        ]

    def palette(status: str, axis: str) -> dict[str, str]:
        normalized = (status or "").strip().lower()
        if axis == "internal":
            color_map = {
                "new": {
                    "backgroundColor": "#ffedd5",
                    "borderColor": "#f59e0b",
                    "textColor": "#9a3412",
                },
                "overdue": {
                    "backgroundColor": "#fed7aa",
                    "borderColor": "#ea580c",
                    "textColor": "#9a3412",
                },
                "done": {
                    "backgroundColor": "#ccfbf1",
                    "borderColor": "#0d9488",
                    "textColor": "#115e59",
                },
                "cancelled": {
                    "backgroundColor": "#f3f4f6",
                    "borderColor": "#9ca3af",
                    "textColor": "#4b5563",
                },
                "expired": {
                    "backgroundColor": "#fde68a",
                    "borderColor": "#d97706",
                    "textColor": "#92400e",
                },
            }
        else:
            color_map = {
                "new": {
                    "backgroundColor": "#dbeafe",
                    "borderColor": "#2563eb",
                    "textColor": "#1d4ed8",
                },
                "overdue": {
                    "backgroundColor": "#fee2e2",
                    "borderColor": "#dc2626",
                    "textColor": "#b91c1c",
                },
                "done": {
                    "backgroundColor": "#dcfce7",
                    "borderColor": "#16a34a",
                    "textColor": "#166534",
                },
                "cancelled": {
                    "backgroundColor": "#f3f4f6",
                    "borderColor": "#9ca3af",
                    "textColor": "#4b5563",
                },
                "expired": {
                    "backgroundColor": "#fef3c7",
                    "borderColor": "#d97706",
                    "textColor": "#92400e",
                },
            }
        return color_map.get(
            normalized,
            {
                "backgroundColor": "#e5e7eb",
                "borderColor": "#9ca3af",
                "textColor": "#374151",
            },
        )

    events: list[dict[str, object]] = []
    for row in rows:
        docket = row.DocketItem
        matter = row.Matter
        docket_id = str(getattr(docket, "docket_id", "") or "").strip()
        if not docket_id:
            continue
        status = _docket_status(docket)
        final_due_date, internal_due_date = _docket_due_dates(docket)
        base_title = (f"[{matter.our_ref}] " if getattr(matter, "our_ref", None) else "") + (
            docket.name_free or docket.name_ref or ""
        )
        detail_url = url_for("deadlines.docket_detail", docket_id=docket_id)

        if due_axis in {"all", "final"} and _date_str_in_range(
            final_due_date, start_date, end_date
        ):
            final_palette = palette(status, "final")
            events.append(
                {
                    "id": f"{docket_id}:final",
                    "docket_id": docket_id,
                    "title": f"[Final] {base_title}",
                    "start": final_due_date,
                    "url": detail_url,
                    "due_axis": "final",
                    "due_axis_label": "Final Due date",
                    **final_palette,
                }
            )

        if due_axis in {"all", "internal"} and _date_str_in_range(
            internal_due_date, start_date, end_date
        ):
            internal_palette = palette(status, "internal")
            events.append(
                {
                    "id": f"{docket_id}:internal",
                    "docket_id": docket_id,
                    "title": f"[Internal] {base_title}",
                    "start": internal_due_date,
                    "url": detail_url,
                    "due_axis": "internal",
                    "due_axis_label": "Internal Due date",
                    **internal_palette,
                }
            )

    return jsonify(events)


@bp.route("/api/upcoming")
@login_required
def api_upcoming_deadlines():
    """
    Role-based upcoming deadlines API.

    Returns deadlines filtered by user role:
    - : All MGMT deadlines
    - Manager: Own MGMT deadlines
    - table: All WORK deadlines
    - PatentContact: Own WORK deadlines
    - Admin: All deadlines

    Query params:
        days: Number of days to look ahead (default: 30)
        type: Filter by 'mgmt' or 'work' (optional)
    """
    try:
        days_ahead = int(request.args.get("days", 30))
    except Exception:
        days_ahead = 30
    days_ahead = max(1, min(days_ahead, 365))
    type_filter = (request.args.get("type") or "").strip().lower()
    owner_filter = (request.args.get("owner") or "").strip()

    user_role = (current_user.role or "").strip()
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()

    today = date.today()
    future = today + timedelta(days=days_ahead)
    # Base query - not done, due within range
    q = (
        db.session.query(DocketItem, Matter)
        .join(Matter, DocketItem.matter_id == Matter.matter_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
    )

    # Security: restrict to matters the current user can view.
    try:
        accessible_matter_ids = policy_accessible_matter_ids_select(current_user)
        q = q.filter(DocketItem.matter_id.in_(accessible_matter_ids))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deadline.routes.api_upcoming.accessible_matter_ids",
            log_key="deadline.routes.api_upcoming.accessible_matter_ids",
            log_window_seconds=300,
        )
        from sqlalchemy import false

        q = q.filter(false())

    if hasattr(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    if hasattr(Matter, "is_deleted"):
        q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))

    q, due_text = _apply_effective_due_date_range(q, today, future)

    if owner_filter:
        q = q.filter(DocketItem.owner_staff_party_id == owner_filter)

    results = {"mgmt": [], "work": []}

    def _classify_category(cat: str) -> str:
        return "mgmt" if (cat or "").upper() in MGMT_CATEGORIES else "work"

    def _build_item(di: DocketItem, m: Matter) -> dict:
        due_str = _norm_date_str(di.extended_due_date) or _norm_date_str(di.due_date)
        try:
            due_date_obj = date.fromisoformat((due_str or "").split("T")[0])
            days_until = (due_date_obj - today).days
        except Exception:
            days_until = None

        return {
            "id": di.docket_id,
            "case_id": m.matter_id,
            "case_ref": m.our_ref,
            "case_title": m.right_name,
            "title": di.name_free or di.name_ref or "",
            "category": di.category,
            "category_type": _classify_category(di.category),
            "due_date": _norm_date_str(di.due_date),
            "extended_due_date": _norm_date_str(di.extended_due_date),
            "days_until": days_until,
            "assigned_to": di.owner_staff_party_id,
            "url": url_for(
                "case_work.case_detail",
                case_id=m.matter_id,
                docket_id=di.docket_id,
                _anchor="sec-deadlines",
            ),
            "detail_url": url_for("deadlines.docket_detail", docket_id=di.docket_id),
            "case_deadlines_url": url_for(
                "case_work.case_detail",
                case_id=m.matter_id,
                docket_id=di.docket_id,
                _anchor="sec-deadlines",
            ),
        }

    limit = 2000

    flags = resolve_role_scope(user_role)
    can_view_all_mgmt = flags["show_all_mgmt"]
    can_view_all_work = flags["show_all_work"]
    is_mgmt_staff = flags["show_own_mgmt"]
    is_work_staff = flags["show_own_work"]

    def _fetch_items(category_type: str) -> list[dict]:
        if category_type == "mgmt":
            if not (not type_filter or type_filter == "mgmt"):
                return []
            if can_view_all_mgmt:
                qq = q.filter(func.upper(DocketItem.category).in_(list(MGMT_CATEGORIES)))
            elif is_mgmt_staff and staff_pid:
                qq = q.filter(func.upper(DocketItem.category).in_(list(MGMT_CATEGORIES))).filter(
                    DocketItem.owner_staff_party_id == staff_pid
                )
            else:
                return []
        else:
            if not (not type_filter or type_filter == "work"):
                return []
            if can_view_all_work:
                qq = q.filter(func.upper(DocketItem.category).in_(list(WORK_CATEGORIES)))
            elif staff_pid:
                managed_ids = managed_matter_ids_select(current_user)
                base = q.filter(func.upper(DocketItem.category).in_(list(WORK_CATEGORIES)))
                if is_work_staff:
                    # Own + managed matters (if any)
                    qq = base.filter(
                        or_(
                            DocketItem.owner_staff_party_id == staff_pid,
                            DocketItem.matter_id.in_(managed_ids),
                        )
                    )
                else:
                    # MGMT-only roles: managed matters only
                    qq = base.filter(DocketItem.matter_id.in_(managed_ids))
            else:
                return []

        rows = _dedupe_deadline_list_rows(qq.limit(limit).all())
        out: list[dict] = []
        for di, m in rows:
            if due_text is None and not _due_in_range(di, today, future):
                continue
            out.append(_build_item(di, m))
        return out

    results["mgmt"] = _fetch_items("mgmt")
    results["work"] = _fetch_items("work")

    return jsonify(
        {
            "user_role": user_role,
            "days_ahead": days_ahead,
            "mgmt_count": len(results["mgmt"]),
            "work_count": len(results["work"]),
            "mgmt": results["mgmt"],
            "work": results["work"],
        }
    )


