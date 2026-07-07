from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any

from flask import current_app, has_app_context
from sqlalchemy import and_, bindparam, case, func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterCustomField, MatterEvent
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.workflow.status_sync import sync_workflow_due_dates_from_docket_source
from app.services.workflow.task_sync_memo import (
    _coerce_user_id,
    _docket_memo_json,
    _manual_assignment_override_for_docket,
)
from app.services.workflow.task_sync_constants import (
    _AUTO_CLEANUP_NOTE_MARKERS,
    _DUPLICATE_OA_DEADLINE_CLOSE_REASON,
    _USPTO_OA_NAME_REF_PREFIX,
    _MANUAL_WORKFLOW_ASSIGNMENT_KEY,
    _OA_MAIN_NOTICE_REF_RE,
    _OA_TITLE_PREFIX_RE,
    _OWNER_FALLBACK_NOTE_MARKER,
    _OWNER_RECOVERED_NOTE_MARKER,
    _OWNER_UNASSIGNED_NOTE_MARKER,
    _STATUS_RED_CORE_DEADLINE_SOURCES,
    _TRUTHY_STATUS_TOKENS,
)
from app.utils.annuity_deadline_routing import is_annuity_status_red_deadline
from app.utils.docket_dates import (
    adjusted_legal_due_for_docket,
    done_state,
    effective_due_for_legal,
    effective_due_for_work,
)
from app.utils.docket_dates import parse_date as _parse_date_value
from app.utils.docket_dates import parse_done_date
from app.utils.docket_visibility import is_visible_by_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.status_red_visibility import is_non_action_status_red_label
from app.utils.task_assignment_rules import (
    resolve_assignees_for_docket,
    resolve_distribution_decision_for_docket,
    resolve_user_id_by_staff_party_id,
)
from app.utils.workflow_deadline_labels import (
    strip_workflow_deadline_title_suffix,
    workflow_deadline_kind_from_docket_id,
    workflow_deadline_label,
    workflow_deadline_title,
)
from app.utils.workflow_semantics import (
    derive_workflow_category,
    normalize_workflow_category,
    workflow_primary_owner_user_id,
)

_BUSINESS_CODE_PREFIX = "DOCKET:"
_WF_DOCKET_RE = re.compile(r"^WF-(\d+)-", re.IGNORECASE)
_UNSET = object()

logger = logging.getLogger(__name__)

_TASK_SYNC_CACHE_KEY = "_task_sync_cache"


def _get_task_sync_cache() -> dict:
    cache = db.session.info.get(_TASK_SYNC_CACHE_KEY)
    if cache is None:
        cache = {
            "staff_snapshot_by_case": {},
            "completion_signals_by_case": {},
        }
        db.session.info[_TASK_SYNC_CACHE_KEY] = cache
    return cache


def _parse_date(value) -> date | None:
    return _parse_date_value(value)


def _staff_party_id_for_user_id(user_id: int | None) -> str | None:
    user_id = _coerce_user_id(user_id)
    if user_id is None:
        return None
    user = User.query.get(user_id)
    if not user or not bool(getattr(user, "is_active", False)):
        return None
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    return staff_party_id or None


def persist_manual_workflow_assignment_override(
    *,
    workflow: Workflow,
    docket_item: DocketItem | None = None,
    actor_id: int | None = None,
) -> DocketItem | None:
    """Persist a manual assignment override on the source DocketItem.

    Docket-backed workflows are normally regenerated from the source docket and
    case staff assignments. When a user edits the workflow assignees directly,
    store that explicit choice on the source docket so the next Docket -> Workflow
    sync does not restore the computed defaults.
    """
    if workflow is None:
        return None

    source = docket_item or _linked_source_docket_item_for_workflow(workflow)
    if source is None:
        return None

    memo = _docket_memo_json(source)
    if memo is None:
        legacy_memo = (getattr(source, "memo", None) or "").strip()
        memo = {"memo_text": legacy_memo} if legacy_memo else {}

    handler_id = _coerce_user_id(getattr(workflow, "assignee_id", None))
    attorney_id = _coerce_user_id(getattr(workflow, "attorney_assignee_id", None))
    manager_id = _coerce_user_id(getattr(workflow, "inspector_id", None))

    memo[_MANUAL_WORKFLOW_ASSIGNMENT_KEY] = {
        "enabled": True,
        "workflow_id": _coerce_user_id(getattr(workflow, "id", None)),
        "handler_id": handler_id,
        "attorney_assignee_id": attorney_id,
        "manager_assignee_id": manager_id,
        "category": (getattr(workflow, "category", None) or "").strip() or None,
        "actor_id": _coerce_user_id(actor_id),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    source.memo = json.dumps(memo, ensure_ascii=False, sort_keys=True)

    owner_user_id = workflow_primary_owner_user_id(
        category=getattr(workflow, "category", None),
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
    )
    owner_staff_party_id = _staff_party_id_for_user_id(owner_user_id)
    if owner_staff_party_id:
        source.owner_staff_party_id = owner_staff_party_id

    db.session.add(source)
    return source


def _workflow_generated_docket_items(workflow_id: int | None) -> list[DocketItem]:
    wf_id = int(workflow_id or 0)
    if wf_id <= 0:
        return []
    prefix = f"WF-{wf_id}-"
    filters = [DocketItem.docket_id.like(f"{prefix}%")]
    if hasattr(DocketItem, "raw_id"):
        filters.append(func.coalesce(DocketItem.raw_id, "").like(f"{prefix}%"))
    return DocketItem.query.filter(or_(*filters)).all()


def _workflow_generated_docket_kind(docket_item: DocketItem | None) -> str | None:
    if docket_item is None:
        return None
    return workflow_deadline_kind_from_docket_id(getattr(docket_item, "docket_id", None)) or (
        workflow_deadline_kind_from_docket_id(getattr(docket_item, "raw_id", None))
    )


def _linked_source_docket_item_for_workflow(workflow: Workflow | None) -> DocketItem | None:
    if workflow is None:
        return None

    business_code = (getattr(workflow, "business_code", None) or "").strip()
    if not business_code.upper().startswith(_BUSINESS_CODE_PREFIX):
        return None

    docket_id = business_code[len(_BUSINESS_CODE_PREFIX) :].split(":", 1)[0].strip()
    if not docket_id:
        return None
    if _workflow_id_from_docket_id(docket_id) is not None:
        # Workflow-generated docket rows use WF-<workflow_id>-<kind> identifiers.
        # They are derived outputs of sync_from_workflow, not upstream source dockets.
        return None

    docket_item = DocketItem.query.filter_by(docket_id=docket_id).first()
    if docket_item is None:
        return None
    if (
        str(getattr(docket_item, "matter_id", "") or "").strip()
        != str(getattr(workflow, "case_id", "") or "").strip()
    ):
        return None
    if hasattr(docket_item, "is_deleted") and bool(getattr(docket_item, "is_deleted", False)):
        return None
    return docket_item


def _reactivate_workflow_generated_docket(docket_item: DocketItem) -> None:
    if hasattr(docket_item, "is_deleted"):
        docket_item.is_deleted = False
    if hasattr(docket_item, "deleted_at"):
        docket_item.deleted_at = None
    if hasattr(docket_item, "deleted_by"):
        docket_item.deleted_by = None
    if hasattr(docket_item, "delete_reason"):
        docket_item.delete_reason = None


def _soft_delete_workflow_generated_docket(docket_item: DocketItem, *, reason: str) -> None:
    if hasattr(docket_item, "is_deleted"):
        docket_item.is_deleted = True
    if hasattr(docket_item, "deleted_at"):
        docket_item.deleted_at = datetime.utcnow()
    if hasattr(docket_item, "deleted_by"):
        docket_item.deleted_by = None
    if hasattr(docket_item, "delete_reason"):
        docket_item.delete_reason = reason


def _bind_docket_item(docket_item: DocketItem | None) -> DocketItem | None:
    if docket_item is None:
        return None

    try:
        state = sa_inspect(docket_item)
    except Exception:
        state = None

    if state is not None and bool(getattr(state, "persistent", False)):
        return docket_item

    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    if docket_id:
        bound = db.session.get(DocketItem, docket_id)
        if bound is not None:
            return bound

    if state is not None and (
        bool(getattr(state, "transient", False)) or bool(getattr(state, "pending", False))
    ):
        db.session.add(docket_item)
        return docket_item

    try:
        return db.session.merge(docket_item)
    except Exception:
        return docket_item


def _is_done(docket_item: DocketItem) -> bool:
    """Check if docket item is done (includes AUTO_CANCELLED/AUTO_EXPIRED)."""
    state, _ = done_state(docket_item.done_date)
    return state in ("done", "cancelled", "expired")


def _is_auto_cancelled(docket_item: DocketItem) -> bool:
    """Check if docket item was auto-cancelled."""
    state, _ = done_state(docket_item.done_date)
    return state == "cancelled"


def _is_auto_expired(docket_item: DocketItem) -> bool:
    """Check if docket item was auto-expired."""
    state, _ = done_state(docket_item.done_date)
    return state == "expired"


def _effective_due(docket_item: DocketItem) -> date | None:
    return effective_due_for_work(docket_item.due_date, docket_item.extended_due_date)


def _legal_due(docket_item: DocketItem) -> date | None:
    return adjusted_legal_due_for_docket(
        docket_item.due_date,
        docket_item.extended_due_date,
    )


_TERM_EXPIRY_STATUS_RED_LABELS = frozenset(
    {
        "Termexpired",
        "TermExpiry",
        "TermExpiration",
    }
)


def _normalize_term_expiry_status_red_label(label: str | None) -> str:
    return re.sub(r"\s+", "", str(label or "").strip())


def _is_term_expiry_status_red_docket(docket_item: DocketItem) -> bool:
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    name_free = (getattr(docket_item, "name_free", None) or "").strip()
    ref_label = (
        name_ref.split(":", 2)[-1] if name_ref.upper().startswith("MGMT:STATUS_RED:") else ""
    )
    return (
        _normalize_term_expiry_status_red_label(ref_label) in _TERM_EXPIRY_STATUS_RED_LABELS
        or _normalize_term_expiry_status_red_label(name_free) in _TERM_EXPIRY_STATUS_RED_LABELS
    )


def _normalize_matter_division_for_term_expiry_skip(value: str | None) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    if upper in {"DOM", "INC", "OUT"}:
        return upper
    if upper in {"DOMESTIC"}:
        return "DOM"
    if upper in {"INCOMING", "INBOUND"}:
        return "INC"
    if upper in {"OUTGOING", "OUTBOUND", "FOREIGN"}:
        return "OUT"
    if raw in {"Domestic", ""}:
        return "DOM"
    if raw in {"", "Matter", ""}:
        return "INC"
    if raw in {"Foreign", "", "", ""}:
        return "OUT"
    return ""


def _infer_matter_profile_for_term_expiry_skip(matter: Matter | None) -> tuple[str, str]:
    if matter is None:
        return "", ""

    division = _normalize_matter_division_for_term_expiry_skip(getattr(matter, "right_group", None))
    matter_type = (getattr(matter, "matter_type", None) or "").strip().upper()
    if matter_type in {"PATENT", "UTILITY", "DESIGN", "TRADEMARK", "PCT"}:
        return division, matter_type

    our_ref = (getattr(matter, "our_ref", None) or "").strip().upper()
    if len(our_ref) >= 4 and our_ref[:2].isdigit():
        code = our_ref[2:4]
        if code.startswith("P"):
            matter_type = "PATENT"
        elif code.startswith("U"):
            matter_type = "UTILITY"
        elif code.startswith("D"):
            matter_type = "DESIGN"
        elif code.startswith("T"):
            matter_type = "TRADEMARK"
    return division, matter_type


def _is_renewal_managed_term_expiry_docket(docket_item: DocketItem) -> bool:
    if not _is_term_expiry_status_red_docket(docket_item):
        return False

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not case_id:
        return False

    matter = Matter.query.get(case_id)
    if matter is None:
        return False

    try:
        from app.services.annuity.annuity_management import (
            is_annuity_management_disabled_for_matter,
        )
    except Exception:
        return False

    if is_annuity_management_disabled_for_matter(case_id):
        return False

    division, matter_type = _infer_matter_profile_for_term_expiry_skip(matter)
    return division == "DOM" and matter_type in {"PATENT", "UTILITY", "DESIGN", "TRADEMARK"}


def _delete_open_worklogs_for_docket(docket_item: DocketItem) -> int:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    if not docket_id:
        return 0
    rows = WorkLog.query.filter_by(docket_id=docket_id).all()
    deleted = 0
    for wl in rows:
        status = (getattr(wl, "status", None) or "").strip().lower()
        if status in {"completed", "abandoned"}:
            continue
        db.session.delete(wl)
        deleted += 1
    return deleted


def _delete_all_worklogs_for_docket(docket_item: DocketItem) -> int:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    if not docket_id:
        return 0
    rows = WorkLog.query.filter_by(docket_id=docket_id).all()
    for wl in rows:
        db.session.delete(wl)
    return len(rows)


def _cleanup_worklogs_for_skipped_docket(docket_item: DocketItem) -> int:
    if _is_non_work_status_red_docket(docket_item) or _distribution_decision_is_none_for_docket(
        docket_item
    ):
        return _delete_all_worklogs_for_docket(docket_item)
    return _delete_open_worklogs_for_docket(docket_item)


def _uspto_oa_dispatch_token(docket_item: DocketItem) -> str | None:
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    if not name_ref:
        return None
    upper = name_ref.upper()
    if not upper.startswith(_USPTO_OA_NAME_REF_PREFIX):
        return None

    parts = name_ref.split(":")
    if len(parts) < 4:
        return None
    token = (parts[2] or "").strip()
    if not token:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", token):
        return None
    return token


def _is_open_active_docket(docket_item: DocketItem) -> bool:
    done_raw = (getattr(docket_item, "done_date", None) or "").strip()
    if done_raw:
        return False
    if bool(getattr(docket_item, "is_deleted", False)):
        return False
    return True


def _is_uspto_oa_opinion_deadline_docket(docket_item: DocketItem) -> bool:
    if not docket_item:
        return False
    category = (getattr(docket_item, "category", None) or "").strip().upper()
    if category != "USPTO_OA":
        return False
    if _uspto_oa_dispatch_token(docket_item) is None:
        return False
    title_compact = re.sub(r"\s+", "", (getattr(docket_item, "name_free", None) or ""))
    if title_compact and "Deadline" not in title_compact:
        return False
    return True


def _uspto_oa_opinion_deadline_rank(docket_item: DocketItem) -> tuple[date, date, str]:
    legal_due = effective_due_for_legal(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    dispatch_due = _parse_date(_uspto_oa_dispatch_token(docket_item))
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    return (
        legal_due or date.min,
        dispatch_due or date.min,
        docket_id,
    )


def _enforce_latest_open_uspto_oa_opinion_deadline(*, docket_item: DocketItem) -> bool:
    """Keep only the latest open USPTO_OA opinion-deadline docket per matter.

    Returns True when the provided docket_item itself was auto-cancelled as stale.
    """
    if not _is_uspto_oa_opinion_deadline_docket(docket_item):
        return False
    if not _is_open_active_docket(docket_item):
        return False

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not case_id:
        return False

    rows = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(DocketItem.category == "USPTO_OA")
        .all()
    )
    candidates = [
        row
        for row in rows
        if _is_uspto_oa_opinion_deadline_docket(row) and _is_open_active_docket(row)
    ]
    if len(candidates) <= 1:
        return False

    keep = max(candidates, key=_uspto_oa_opinion_deadline_rank)
    stale_rows = [
        row
        for row in candidates
        if (getattr(row, "docket_id", None) or "").strip() != keep.docket_id
    ]
    if not stale_rows:
        return False

    today_token = f"AUTO_CANCELLED:{date.today().isoformat()}"
    stale_ids: set[str] = set()
    for stale in stale_rows:
        stale_id = (getattr(stale, "docket_id", None) or "").strip()
        if not stale_id:
            continue
        stale_ids.add(stale_id)
        try:
            _reconcile_distributed_workflows(
                docket_item=stale,
                target_assignee_ids=set(),
                delete_non_target=True,
            )
        except Exception:
            logger.exception(
                "Failed to reconcile stale USPTO_OA workflows for matter=%s docket=%s",
                case_id,
                stale_id,
            )
        try:
            _delete_open_worklogs_for_docket(stale)
        except Exception:
            logger.exception(
                "Failed to cleanup stale USPTO_OA worklogs for matter=%s docket=%s",
                case_id,
                stale_id,
            )
        stale.done_date = today_token
        db.session.add(stale)

    current_id = (getattr(docket_item, "docket_id", None) or "").strip()
    return current_id in stale_ids


def _has_manual_abandon_note_for_docket(*, docket_item: DocketItem) -> bool:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not docket_id or not case_id:
        return False

    prefix = f"{_BUSINESS_CODE_PREFIX}{docket_id}"
    rows = (
        Workflow.query.filter_by(case_id=case_id)
        .filter(Workflow.business_code.like(f"{prefix}%"))
        .all()
    )
    for wf in rows:
        note = (getattr(wf, "note", None) or "").strip()
        if "[]" in note:
            return True
    return False


def _should_restore_auto_cancelled_uspto_oa(docket_item: DocketItem) -> bool:
    """Restore suspicious USPTO_OA AUTO_CANCELLED rows that still have future due dates.

    Guardrail:
    - Only for legacy USPTO_OA auto-generated rows.
    - Skip when explicit manual abandon note exists.
    - Skip when another same-dispatch USPTO_OA row is already open.
    """
    if not docket_item:
        return False

    state, _ = done_state(getattr(docket_item, "done_date", None))
    if state != "cancelled":
        return False

    category = (getattr(docket_item, "category", None) or "").strip().upper()
    dispatch_token = _uspto_oa_dispatch_token(docket_item)
    if category != "USPTO_OA" or not dispatch_token:
        return False

    due = effective_due_for_legal(
        getattr(docket_item, "due_date", None),
        getattr(docket_item, "extended_due_date", None),
    )
    if not due:
        return False
    if due < date.today():
        return False

    if _has_manual_abandon_note_for_docket(docket_item=docket_item):
        return False

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    if not case_id or not docket_id:
        return False

    open_peer = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(
            and_(
                DocketItem.category == "USPTO_OA",
                DocketItem.name_ref.ilike(f"USPTO_OA:OFFICE_ACTION:{dispatch_token}:%"),
            )
        )
        .first()
    )
    if open_peer is not None:
        return False

    return True


def _effective_due_token_for_duplicate_match(docket_item: DocketItem) -> str:
    due = _effective_due(docket_item) or _legal_due(docket_item)
    return due.isoformat() if due else ""


def _matching_status_red_peer_for_legacy_v2_limit(
    *,
    docket_item: DocketItem,
    open_only: bool,
) -> DocketItem | None:
    if not docket_item:
        return None

    category = (getattr(docket_item, "category", None) or "").strip().upper()
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    due_token = _effective_due_token_for_duplicate_match(docket_item)

    if category != "V2_LIMIT" or name_ref or not case_id or not due_token:
        return None

    q = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(
            or_(
                func.trim(func.coalesce(DocketItem.due_date, "")) == due_token,
                func.trim(func.coalesce(DocketItem.extended_due_date, "")) == due_token,
            )
        )
        .filter(DocketItem.name_ref.ilike("MGMT:STATUS_RED:%"))
    )
    if open_only:
        q = q.filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))

    return q.order_by(
        case(
            (
                or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""),
                0,
            ),
            else_=1,
        ),
        DocketItem.docket_id.asc(),
    ).first()


def _legacy_v2_limit_duplicates_for_status_red(*, docket_item: DocketItem) -> list[DocketItem]:
    if not docket_item:
        return []

    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    due_token = _effective_due_token_for_duplicate_match(docket_item)

    if not name_ref.upper().startswith("MGMT:STATUS_RED:") or not case_id or not due_token:
        return []

    return (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(func.upper(func.trim(func.coalesce(DocketItem.category, ""))) == "V2_LIMIT")
        .filter(func.trim(func.coalesce(DocketItem.name_ref, "")) == "")
        .filter(
            or_(
                func.trim(func.coalesce(DocketItem.due_date, "")) == due_token,
                func.trim(func.coalesce(DocketItem.extended_due_date, "")) == due_token,
            )
        )
        .order_by(DocketItem.docket_id.asc())
        .all()
    )


def _auto_cancel_legacy_v2_limit_duplicate(
    *,
    docket_item: DocketItem,
    done_value: str,
    replacement_docket_item: DocketItem | None = None,
) -> None:
    current_done = (getattr(docket_item, "done_date", None) or "").strip()

    if replacement_docket_item is not None:
        try:
            _relink_completed_legacy_v2_limit_workflows(
                legacy_docket_item=docket_item,
                replacement_docket_item=replacement_docket_item,
            )
        except Exception:
            logger.exception(
                "Failed to relink completed legacy V2_LIMIT workflows for matter=%s legacy=%s replacement=%s",
                getattr(docket_item, "matter_id", None),
                getattr(docket_item, "docket_id", None),
                getattr(replacement_docket_item, "docket_id", None),
            )

    try:
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=set(),
            delete_non_target=True,
        )
    except Exception:
        logger.exception(
            "Failed to reconcile duplicate legacy V2_LIMIT workflows for matter=%s docket=%s",
            getattr(docket_item, "matter_id", None),
            getattr(docket_item, "docket_id", None),
        )

    try:
        _delete_open_worklogs_for_docket(docket_item)
    except Exception:
        logger.exception(
            "Failed to cleanup duplicate legacy V2_LIMIT worklogs for matter=%s docket=%s",
            getattr(docket_item, "matter_id", None),
            getattr(docket_item, "docket_id", None),
        )

    if not current_done:
        docket_item.done_date = done_value
        db.session.add(docket_item)


def _merge_workflow_note_text(base: str | None, extra: str | None) -> str | None:
    base_txt = (base or "").strip()
    extra_txt = (extra or "").strip()
    if not extra_txt:
        return base_txt or None
    if not base_txt:
        return extra_txt
    if extra_txt in base_txt:
        return base_txt
    return f"{base_txt}\n{extra_txt}".strip()


def _relink_completed_legacy_v2_limit_workflows(
    *,
    legacy_docket_item: DocketItem,
    replacement_docket_item: DocketItem,
) -> int:
    legacy_docket_id = (getattr(legacy_docket_item, "docket_id", None) or "").strip()
    replacement_docket_id = (getattr(replacement_docket_item, "docket_id", None) or "").strip()
    case_id = str(getattr(legacy_docket_item, "matter_id", "") or "").strip()
    if not legacy_docket_id or not replacement_docket_id or not case_id:
        return 0

    source_workflows = (
        Workflow.query.filter(Workflow.case_id == case_id)
        .filter(Workflow.business_code.like(f"{_BUSINESS_CODE_PREFIX}{legacy_docket_id}%"))
        .filter(Workflow.status == "Completed")
        .order_by(Workflow.id.asc())
        .all()
    )
    if not source_workflows:
        return 0

    replacement_due = _effective_due(replacement_docket_item)
    replacement_legal_due = _legal_due(replacement_docket_item)
    moved = 0

    for wf in source_workflows:
        preferred_bc = _docket_business_code(
            replacement_docket_id, getattr(wf, "assignee_id", None)
        )
        canonical_bc = _docket_business_code(replacement_docket_id, None)
        candidate_bcs: list[str] = []
        for bc in (preferred_bc, canonical_bc):
            if bc and bc not in candidate_bcs:
                candidate_bcs.append(bc)
        if not candidate_bcs:
            continue

        target_wf = (
            Workflow.query.filter(Workflow.case_id == case_id)
            .filter(Workflow.business_code.in_(candidate_bcs))
            .filter(Workflow.id != wf.id)
            .order_by(
                case((Workflow.status == "Completed", 0), else_=1),
                Workflow.id.asc(),
            )
            .first()
        )
        completed_on = (
            getattr(wf, "completed_date", None)
            or parse_done_date(getattr(replacement_docket_item, "done_date", None))
            or date.today()
        )

        if target_wf is not None:
            target_wf.status = "Completed"
            target_wf.completed_date = completed_on
            target_wf.note = _merge_workflow_note_text(target_wf.note, wf.note)
            if replacement_due is not None:
                target_wf.due_date = replacement_due
            if replacement_legal_due is not None:
                target_wf.legal_due_date = replacement_legal_due
            db.session.add(target_wf)
            try:
                from app.services.workflow.status_sync import (
                    sync_linked_docket_done_date_from_workflow,
                )

                sync_linked_docket_done_date_from_workflow(
                    target_wf, completed_on=target_wf.completed_date
                )
            except Exception:
                logger.exception(
                    "Failed to sync replacement docket done_date from workflow=%s",
                    getattr(target_wf, "id", None),
                )
            wf_id = int(wf.id) if getattr(wf, "id", None) else None
            if wf_id:
                _delete_workflow_for_distribution_cleanup(workflow_id=wf_id)
            else:
                db.session.delete(wf)
            moved += 1
            continue

        wf.business_code = preferred_bc or canonical_bc
        wf.completed_date = completed_on
        if replacement_due is not None:
            wf.due_date = replacement_due
        if replacement_legal_due is not None:
            wf.legal_due_date = replacement_legal_due
        db.session.add(wf)
        try:
            from app.services.workflow.status_sync import sync_linked_docket_done_date_from_workflow

            sync_linked_docket_done_date_from_workflow(wf, completed_on=wf.completed_date)
        except Exception:
            logger.exception(
                "Failed to sync relinked docket done_date from workflow=%s",
                getattr(wf, "id", None),
            )
        moved += 1

    return moved


def _cleanup_duplicate_legacy_v2_limit_for_status_red(*, docket_item: DocketItem) -> int:
    duplicates = _legacy_v2_limit_duplicates_for_status_red(docket_item=docket_item)
    if not duplicates:
        return 0

    source_done = (getattr(docket_item, "done_date", None) or "").strip()
    done_value = (
        source_done
        if source_done.startswith("AUTO_CANCELLED:")
        else f"AUTO_CANCELLED:{date.today().isoformat()}"
    )

    cleaned = 0
    for duplicate in duplicates:
        _auto_cancel_legacy_v2_limit_duplicate(
            docket_item=duplicate,
            done_value=done_value,
            replacement_docket_item=docket_item,
        )
        cleaned += 1
    return cleaned


def _load_case_completion_signals(case_id: str) -> dict[str, date]:
    """
    Collect best-effort completion signals from matter_event + matter_custom_field.

    This is intentionally conservative and only includes keys used to auto-complete
    very specific payment/close type dockets.
    """
    cache = _get_task_sync_cache()
    bucket = cache["completion_signals_by_case"]
    cached = bucket.get(case_id)
    if cached is not None:
        return dict(cached)

    signals: dict[str, date] = {}
    key_aliases: dict[str, tuple[str, ...]] = {
        "complete": ("Done/End Date", "complete_date"),
        "abandon": ("/", "abandon_date"),
        "registration": ("Registration", "registration_date"),
        "reg_extension": ("Registration", "reg_extension_date"),
    }

    def _set_signal(kind: str, raw: object) -> None:
        parsed = _parse_date(raw)
        if not parsed:
            return
        existing = signals.get(kind)
        if not existing or parsed > existing:
            signals[kind] = parsed

    try:
        rows = (
            MatterEvent.query.filter(MatterEvent.matter_id == case_id)
            .filter(MatterEvent.event_at.isnot(None))
            .all()
        )
        for row in rows:
            key_raw = (getattr(row, "event_key", None) or "").strip()
            if not key_raw:
                continue
            for kind, aliases in key_aliases.items():
                if key_raw in aliases:
                    _set_signal(kind, getattr(row, "event_at", None))
    except Exception:
        logger.exception("Failed to load matter_event completion signals for %s", case_id)

    try:
        rows = MatterCustomField.query.filter(MatterCustomField.matter_id == case_id).all()
        for row in rows:
            data = getattr(row, "data", None) or {}
            if not isinstance(data, dict):
                continue
            for kind, aliases in key_aliases.items():
                for alias in aliases:
                    if alias in data:
                        _set_signal(kind, data.get(alias))
    except Exception:
        logger.exception("Failed to load matter_custom_field completion signals for %s", case_id)

    bucket[case_id] = dict(signals)
    return dict(signals)


def _infer_done_date_from_matter_signals(*, docket_item: DocketItem, task_name: str) -> date | None:
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not case_id:
        return None

    name = (task_name or "").strip()
    if not name:
        return None
    name_compact = re.sub(r"\s+", "", name)
    signals: dict[str, date] | None = None

    # Very specific rule: payment extension approval notices should be treated done
    # when we already have registration/extension completion signals on the matter.
    payment_extension_keywords = (
        "RegistrationPayment",
        "RegistrationFeePayment",
        "RegistrationExtension",
        "RegExtension",
        "등록료납부",
        "납부연장",
        "등록연장",
    )
    if any(re.sub(r"\s+", "", k) in name_compact for k in payment_extension_keywords):
        signals = signals or _load_case_completion_signals(case_id)
        return (
            signals.get("reg_extension") or signals.get("registration") or signals.get("complete")
        )

    if any(token in name_compact for token in ("Registration", "등록결정", "등록기한", "등록마감")):
        signals = _load_case_completion_signals(case_id)
        return signals.get("registration")

    return None


def _normalize_docket_task_title(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    # Normalize common operational suffixes so equivalent auto-generated rows
    # (e.g. RegistrationDeadline / RegistrationDeadline / RegistrationDeadline ) can be matched reliably.
    raw = re.sub(r"\s*\(\)\s*$", "", raw).strip()
    raw = re.sub(r"\s*\s*$", "", raw).strip()

    try:
        from app.services.matter.matter_auto_status import normalize_red_status

        raw = normalize_red_status(raw)
    except Exception:
        raw = raw.strip()

    return re.sub(r"\s+", "", raw).strip()


def _docket_task_family_key_from_values(name_ref: str | None, name_free: str | None) -> str:
    ref = (name_ref or "").strip()
    ref_upper = ref.upper()

    title_key = _normalize_docket_task_title(name_free or ref)
    if title_key:
        if "RegistrationDeadline" in title_key:
            return "PENALTY_REGISTRATION_DEADLINE"
        if "RegistrationDeadline" in title_key:
            return "REGISTRATION_DEADLINE"
        if title_key in {"ApplicationDeadline", "Application"}:
            return "APPLICATION_DEADLINE"
        if title_key in {"Deadline", "Deadline", ""}:
            return "EXAM_REQUEST_DEADLINE"
        return f"TITLE:{title_key}"

    compact_ref = re.sub(r"\s+", "", ref_upper)
    if compact_ref in {"Registration", "Registration()", "MGMT:REGISTRATION"}:
        return "REGISTRATION_DEADLINE"
    if compact_ref in {"Application", "Application()", "MGMT:FILING"}:
        return "APPLICATION_DEADLINE"
    if compact_ref in {"", "()", "MGMT:EXAM_REQUEST"}:
        return "EXAM_REQUEST_DEADLINE"
    if compact_ref:
        return f"REF:{compact_ref}"
    return ""


def _docket_task_family_key(docket_item: DocketItem) -> str:
    return _docket_task_family_key_from_values(
        getattr(docket_item, "name_ref", None),
        getattr(docket_item, "name_free", None),
    )


def _docket_memo_value(docket_item: DocketItem | None, key: str) -> str:
    raw_key = (key or "").strip()
    if not raw_key:
        return ""

    memo = _docket_memo_json(docket_item)
    if memo:
        value = memo.get(raw_key)
        if value is not None:
            return str(value or "").strip()

    raw_memo = (getattr(docket_item, "memo", None) or "").strip() if docket_item else ""
    if not raw_memo:
        return ""
    match = re.search(rf'"{re.escape(raw_key)}"\s*:\s*"([^"]*)"', raw_memo)
    if not match:
        return ""
    return (match.group(1) or "").strip()


def _is_consolidated_duplicate_of_current(
    *,
    candidate: DocketItem | None,
    current: DocketItem | None,
) -> bool:
    """Return True when candidate was auto-closed as a duplicate of current.

    Such rows must stay closed, but they are not evidence that the canonical
    current row should be closed again after a user reopens it.
    """
    if candidate is None or current is None:
        return False
    candidate_id = (getattr(candidate, "docket_id", None) or "").strip()
    current_id = (getattr(current, "docket_id", None) or "").strip()
    if not candidate_id or not current_id or candidate_id == current_id:
        return False

    close_reason = _docket_memo_value(candidate, "close_reason")
    if close_reason != _DUPLICATE_OA_DEADLINE_CLOSE_REASON:
        return False
    canonical_docket_id = _docket_memo_value(candidate, "canonical_docket_id")
    return canonical_docket_id == current_id


def _is_status_red_docket(docket_item: DocketItem) -> bool:
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    return name_ref.startswith("MGMT:STATUS_RED:")


def _normalize_status_red_label(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        from app.services.matter.matter_auto_status import normalize_red_status

        raw = normalize_red_status(raw)
    except Exception:
        raw = raw.strip()
    return re.sub(r"\s+", "", raw).strip()


def _status_red_label_from_docket(docket_item: DocketItem | None) -> str:
    if docket_item is None:
        return ""
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    if name_ref.upper().startswith("MGMT:STATUS_RED:"):
        return _normalize_status_red_label(name_ref.split(":", 2)[-1])
    return _normalize_status_red_label(getattr(docket_item, "name_free", None))


def _office_action_doc_label_from_docket(docket_item: DocketItem | None) -> str:
    if docket_item is None:
        return ""
    title = (getattr(docket_item, "name_free", None) or "").strip()
    if not title:
        return ""
    title = _OA_TITLE_PREFIX_RE.sub("", title).strip()
    title = re.sub(r"\s*\(\)\s*$", "", title).strip()
    return _normalize_status_red_label(title)


def _matter_custom_data_rows(case_id: str) -> list[dict]:
    mid = (case_id or "").strip()
    if not mid:
        return []
    try:
        rows = MatterCustomField.query.filter(MatterCustomField.matter_id == mid).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.matter_custom_data_rows",
            log_key="workflow.task_sync.matter_custom_data_rows",
            log_window_seconds=300,
        )
        return []

    out: list[dict] = []
    for row in rows:
        data = getattr(row, "data", None)
        if isinstance(data, dict):
            out.append(data)
    return out


def _custom_value_for_keys(custom_rows: list[dict], keys: tuple[str, ...]) -> object:
    for data in custom_rows:
        for key in keys:
            if key in data:
                value = data.get(key)
                if value is not None and str(value).strip():
                    return value
    return None


def _event_dates_for_keys(case_id: str, keys: tuple[str, ...]) -> list[date]:
    mid = (case_id or "").strip()
    key_set = {str(key or "").strip() for key in keys if str(key or "").strip()}
    if not mid or not key_set:
        return []
    try:
        rows = (
            MatterEvent.query.filter(MatterEvent.matter_id == mid)
            .filter(MatterEvent.event_key.in_(sorted(key_set)))
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.event_dates_for_keys",
            log_key="workflow.task_sync.event_dates_for_keys",
            log_window_seconds=300,
        )
        return []

    out: list[date] = []
    for row in rows:
        parsed = _parse_date(getattr(row, "event_at", None)) or _parse_date(
            getattr(row, "event_date", None)
        )
        if parsed:
            out.append(parsed)
    return out


def _custom_truthy_for_keys(custom_rows: list[dict], keys: tuple[str, ...]) -> bool:
    value = _custom_value_for_keys(custom_rows, keys)
    if value is None:
        return False
    return str(value or "").strip().upper() in _TRUTHY_STATUS_TOKENS


def _custom_date_for_keys(custom_rows: list[dict], keys: tuple[str, ...]) -> date | None:
    value = _custom_value_for_keys(custom_rows, keys)
    return _parse_date(value)


def _is_supported_open_core_status_red_deadline(docket_item: DocketItem) -> bool:
    """Keep real core deadline rows open even when another red status is current."""
    label = _status_red_label_from_docket(docket_item)
    spec = _STATUS_RED_CORE_DEADLINE_SOURCES.get(label)
    if not spec:
        return False

    try:
        from app.services.deadlines.mgmt_deadlines import _core_status_red_source_still_open

        return bool(_core_status_red_source_still_open(item=docket_item))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.supported_core_status_red.source_guard",
            log_key="workflow.task_sync.supported_core_status_red.source_guard",
            log_window_seconds=300,
        )

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    due = _legal_due(docket_item) or _effective_due(docket_item)
    if not case_id or due is None:
        return False
    if due < date.today():
        return False

    custom_rows = _matter_custom_data_rows(case_id)

    if _custom_truthy_for_keys(custom_rows, spec["done_truthy_custom_keys"]):
        return False
    if _custom_date_for_keys(custom_rows, spec["done_custom_keys"]):
        return False
    if _event_dates_for_keys(case_id, spec["done_event_keys"]):
        return False

    custom_due = _custom_date_for_keys(custom_rows, spec["deadline_custom_keys"])
    if custom_due == due:
        return True
    if due in _event_dates_for_keys(case_id, spec["deadline_event_keys"]):
        return True

    deadline_code = _docket_memo_value(docket_item, "deadline_code")
    return bool(deadline_code and deadline_code in spec["deadline_codes"])


def _is_supported_open_office_action_due_docket(docket_item: DocketItem) -> bool:
    """Keep canonical OA due rows open while the source OfficeAction is still open."""
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    match = _OA_MAIN_NOTICE_REF_RE.match(name_ref)
    if not match:
        return False

    due = _legal_due(docket_item) or _effective_due(docket_item)
    if due is None or due < date.today():
        return False

    try:
        from app.models.communication import OfficeAction
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.supported_open_office_action.import",
            log_key="workflow.task_sync.supported_open_office_action.import",
            log_window_seconds=300,
        )
        return False

    try:
        office_action = OfficeAction.query.get((match.group(1) or "").strip())
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.supported_open_office_action.query",
            log_key="workflow.task_sync.supported_open_office_action.query",
            log_window_seconds=300,
        )
        return False
    if office_action is None or (getattr(office_action, "done_date", None) or "").strip():
        return False

    office_action_legal_due = _parse_date(getattr(office_action, "due_date", None))
    office_action_effective_due = effective_due_for_work(
        getattr(office_action, "due_date", None),
        getattr(office_action, "extended_due_date", None),
    )
    return due in {office_action_legal_due, office_action_effective_due}


def _matching_open_oa_due_docket_for_status_red(docket_item: DocketItem) -> DocketItem | None:
    """Return the open NOTICE:OA main docket that already represents this OA response work."""
    if not docket_item or not _is_status_red_docket(docket_item):
        return None
    if not _is_open_active_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    status_red_label = _status_red_label_from_docket(docket_item)
    legal_due = _legal_due(docket_item)
    if not case_id or not docket_id or not status_red_label or legal_due is None:
        return None

    rows = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(DocketItem.name_ref.ilike("NOTICE:OA:%"))
        .all()
    )
    for row in rows:
        row_name_ref = (getattr(row, "name_ref", None) or "").strip()
        if not _OA_MAIN_NOTICE_REF_RE.match(row_name_ref):
            continue
        if _legal_due(row) != legal_due:
            continue
        if _office_action_doc_label_from_docket(row) != status_red_label:
            continue
        return row
    return None


def _matching_open_status_red_for_oa_due_docket(docket_item: DocketItem) -> DocketItem | None:
    """Return the open MGMT:STATUS_RED peer for a main NOTICE:OA docket."""
    if not docket_item:
        return None

    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    if not _OA_MAIN_NOTICE_REF_RE.match(name_ref):
        return None
    if not _is_open_active_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    office_action_label = _office_action_doc_label_from_docket(docket_item)
    legal_due = _legal_due(docket_item)
    if not case_id or not docket_id or not office_action_label or legal_due is None:
        return None

    rows = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(DocketItem.name_ref.ilike("MGMT:STATUS_RED:%"))
        .all()
    )
    for row in rows:
        if _legal_due(row) != legal_due:
            continue
        if _status_red_label_from_docket(row) != office_action_label:
            continue
        return row
    return None


def _should_delete_skipped_docket_workflows(docket_item: DocketItem) -> bool:
    if _is_renewal_managed_term_expiry_docket(docket_item):
        return True
    if _is_non_work_status_red_docket(docket_item):
        return True
    if _distribution_decision_is_none_for_docket(docket_item):
        return True
    return _matching_open_oa_due_docket_for_status_red(docket_item) is not None


_NON_WORK_STATUS_RED_LABELS = frozenset({"Application"})


def _is_non_work_status_red_docket(docket_item: DocketItem | None) -> bool:
    if _is_non_action_status_red_docket(docket_item):
        return True
    if docket_item is None or not _is_status_red_docket(docket_item):
        return False
    label = _status_red_label_from_docket(docket_item) or getattr(docket_item, "name_free", None)
    compact = re.sub(r"\s+", "", str(label or "")).strip()
    return compact in _NON_WORK_STATUS_RED_LABELS


def _is_non_action_status_red_docket(docket_item: DocketItem | None) -> bool:
    if docket_item is None or not _is_status_red_docket(docket_item):
        return False
    return is_non_action_status_red_label(
        _status_red_label_from_docket(docket_item) or getattr(docket_item, "name_free", None)
    )


def _distribution_decision_is_none_for_docket(docket_item: DocketItem | None) -> bool:
    if docket_item is None:
        return False
    try:
        decision = resolve_distribution_decision_for_docket(docket_item)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.task_sync.distribution_decision_is_none",
            log_key="workflow.task_sync.distribution_decision_is_none",
            log_window_seconds=300,
        )
        return False
    return decision.distribute_to == "none"


def _cleanup_duplicate_status_red_workflows_for_oa_due(*, docket_item: DocketItem) -> set[int]:
    peer = _matching_open_status_red_for_oa_due_docket(docket_item)
    if peer is None:
        return set()

    try:
        _delete_open_worklogs_for_docket(peer)
    except Exception:
        logger.exception(
            "Failed to cleanup duplicate status-red worklogs for matter=%s docket=%s",
            getattr(peer, "matter_id", None),
            getattr(peer, "docket_id", None),
        )

    return _cleanup_skipped_docket_workflows(
        peer,
        delete_auto_generated=True,
    )


def _current_status_red_label(case_id: str) -> str:
    if not case_id:
        return ""
    matter = Matter.query.get(case_id)
    if matter is None:
        return ""
    try:
        from app.services.matter.matter_auto_status import normalize_red_status

        return normalize_red_status(getattr(matter, "status_red", None))
    except Exception:
        return (getattr(matter, "status_red", None) or "").strip()


def _terminal_state_priority(state: str | None) -> int:
    normalized = (state or "").strip().lower()
    if normalized in {"done", "completed"}:
        return 3
    if normalized in {"cancelled", "abandoned"}:
        return 2
    if normalized == "expired":
        return 1
    return 0


def _terminal_done_value_from_peer_dockets(docket_item: DocketItem) -> str | None:
    if _is_supported_open_core_status_red_deadline(
        docket_item
    ) or _is_supported_open_office_action_due_docket(docket_item):
        return None
    if _is_system_pct_advisory_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    family_key = _docket_task_family_key(docket_item)
    due_token = _effective_due_token_for_duplicate_match(docket_item)
    if not case_id or not docket_id or not family_key or not due_token:
        return None

    rows = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(and_(DocketItem.done_date.isnot(None), func.trim(DocketItem.done_date) != ""))
        .filter(
            or_(
                func.trim(func.coalesce(DocketItem.due_date, "")) == due_token,
                func.trim(func.coalesce(DocketItem.extended_due_date, "")) == due_token,
            )
        )
        .all()
    )

    best_raw = None
    best_key: tuple[date, int, str] | None = None
    for row in rows:
        if _is_consolidated_duplicate_of_current(candidate=row, current=docket_item):
            continue
        if _docket_task_family_key(row) != family_key:
            continue
        raw_done = (getattr(row, "done_date", None) or "").strip()
        if not raw_done:
            continue
        parsed_done = parse_done_date(raw_done) or date.min
        rank = (parsed_done, _terminal_state_priority(done_state(raw_done)[0]), raw_done)
        if best_key is None or rank > best_key:
            best_key = rank
            best_raw = raw_done
    return best_raw


def _terminal_done_value_from_peer_workflows(docket_item: DocketItem) -> str | None:
    if _is_supported_open_core_status_red_deadline(
        docket_item
    ) or _is_supported_open_office_action_due_docket(docket_item):
        return None
    if _is_system_pct_advisory_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    family_key = _docket_task_family_key(docket_item)
    due = _effective_due(docket_item) or _legal_due(docket_item)
    if not case_id or not family_key or due is None:
        return None

    rows = (
        Workflow.query.filter(Workflow.case_id == case_id)
        .filter(Workflow.status.in_(("Completed", "Abandoned")))
        .filter(or_(Workflow.due_date == due, Workflow.legal_due_date == due))
        .all()
    )

    best_row: Workflow | None = None
    best_key: tuple[date, int, int] | None = None
    for row in rows:
        business_code = (getattr(row, "business_code", None) or "").strip().upper()
        if docket_id and business_code.startswith(f"{_BUSINESS_CODE_PREFIX}{docket_id}".upper()):
            # Same-docket workflows must not re-close an open docket during reopen sync.
            continue
        linked_source_docket = _linked_source_docket_item_for_workflow(row)
        if _is_consolidated_duplicate_of_current(
            candidate=linked_source_docket,
            current=docket_item,
        ):
            continue
        if _docket_task_family_key_from_values(None, getattr(row, "name", None)) != family_key:
            continue
        status = (getattr(row, "status", None) or "").strip()
        rank = (
            getattr(row, "completed_date", None) or date.min,
            _terminal_state_priority(status),
            int(getattr(row, "id", 0) or 0),
        )
        if best_key is None or rank > best_key:
            best_key = rank
            best_row = row
    if best_row is None:
        return None
    completed_on = getattr(best_row, "completed_date", None) or date.today()
    if (getattr(best_row, "status", None) or "").strip() == "Abandoned":
        return f"AUTO_CANCELLED:{completed_on.isoformat()}"
    return completed_on.isoformat()


def _is_system_pct_advisory_docket(docket_item: DocketItem | None) -> bool:
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    memo = _docket_memo_json(docket_item) or {}
    return name_ref == "MGMT:PCT_ADVISORY_19M" or (
        memo.get("deadline_code") == "PCT_ADVISORY_19M"
        and memo.get("template_id") == "PCT_ADVISORY_19M"
    )


def _pct_advisory_done_value_if_superseded(docket_item: DocketItem) -> str | None:
    label = _normalize_docket_task_title(
        getattr(docket_item, "name_free", None) or getattr(docket_item, "name_ref", None)
    )
    if label not in {
        "PCTDeadline",
        "Domestic Deadline 1  ",
        "DomesticDeadline19Deadline",
    }:
        return None

    if _is_system_pct_advisory_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    if not case_id or not docket_id:
        return None

    rows = (
        DocketItem.query.filter(DocketItem.matter_id == case_id)
        .filter(DocketItem.docket_id != docket_id)
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .order_by(DocketItem.docket_id.asc())
        .all()
    )
    for row in rows:
        row_label = _normalize_docket_task_title(
            getattr(row, "name_free", None) or getattr(row, "name_ref", None)
        )
        if row_label != "PCTDomesticDeadline":
            continue
        raw_done = (getattr(row, "done_date", None) or "").strip()
        if raw_done:
            return raw_done
        return date.today().isoformat()
    return None


def _stale_status_red_done_value(docket_item: DocketItem) -> str | None:
    if not _is_status_red_docket(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    current_label = _current_status_red_label(case_id)
    if not current_label:
        return None

    try:
        from app.services.matter.matter_auto_status import is_known_deadline_red_label

        actionable_keywords = ("Deadline", "Notice")
        if not is_known_deadline_red_label(current_label) and not any(
            token in current_label for token in actionable_keywords
        ):
            return None
    except Exception:
        return None

    docket_label = _normalize_docket_task_title(getattr(docket_item, "name_free", None))
    if not docket_label:
        return None
    if docket_label == _normalize_docket_task_title(current_label):
        return None
    if _is_supported_open_core_status_red_deadline(docket_item):
        return None
    return date.today().isoformat()


def _should_close_docket_from_case_terminal_state(docket_item: DocketItem) -> bool:
    family_key = _docket_task_family_key(docket_item)
    if family_key in {
        "APPLICATION_DEADLINE",
        "EXAM_REQUEST_DEADLINE",
        "REGISTRATION_DEADLINE",
        "PENALTY_REGISTRATION_DEADLINE",
    }:
        return True
    return _is_status_red_docket(docket_item)


def _terminal_done_value_from_case_state(docket_item: DocketItem) -> str | None:
    if not _should_close_docket_from_case_terminal_state(docket_item):
        return None

    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not case_id:
        return None

    signals = _load_case_completion_signals(case_id)
    family_key = _docket_task_family_key(docket_item)

    if family_key in {"REGISTRATION_DEADLINE", "PENALTY_REGISTRATION_DEADLINE"}:
        registration_done = signals.get("registration") or signals.get("reg_extension")
        if registration_done:
            return registration_done.isoformat()

    abandon_done = signals.get("abandon")
    if abandon_done:
        return f"AUTO_CANCELLED:{abandon_done.isoformat()}"

    complete_done = signals.get("complete")
    if complete_done:
        return complete_done.isoformat()

    matter = Matter.query.get(case_id)
    if matter is None:
        return None

    try:
        from app.services.case.terminal_status import is_terminal_case_status
    except Exception:
        return None

    status_candidates = (
        getattr(matter, "status_blue", None),
        getattr(matter, "inhouse_status", None),
        getattr(matter, "status_red", None),
    )
    if not any(is_terminal_case_status(value) for value in status_candidates):
        return None

    # Manual terminal case statuses are authoritative. Once a case is marked
    # closed/abandoned at the matter level, status-red/filing deadline dockets
    # should not remain open just because their due date is still in the future.
    return f"AUTO_CANCELLED:{date.today().isoformat()}"


def _maybe_apply_terminal_done_value_to_docket(docket_item: DocketItem) -> bool:
    if not docket_item or _is_done(docket_item):
        return False

    done_value = (
        _terminal_done_value_from_peer_dockets(docket_item)
        or _terminal_done_value_from_peer_workflows(docket_item)
        or _pct_advisory_done_value_if_superseded(docket_item)
        or _terminal_done_value_from_case_state(docket_item)
        or _stale_status_red_done_value(docket_item)
    )
    if not done_value:
        return False

    docket_item.done_date = done_value
    db.session.add(docket_item)
    return True


def _business_code_assignee_id_for_workflow(workflow: Workflow) -> int | None:
    business_code = (getattr(workflow, "business_code", None) or "").strip()
    if not business_code.upper().startswith("DOCKET:"):
        return None
    parts = business_code.split(":", 2)
    if len(parts) < 3:
        return None
    try:
        user_id = int((parts[2] or "").strip())
    except Exception:
        return None
    return user_id if user_id > 0 else None


def _sync_existing_workflows_for_terminal_docket(docket_item: DocketItem) -> list[Workflow]:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not docket_id or not case_id:
        return []

    rows = (
        Workflow.query.filter(Workflow.case_id == case_id)
        .filter(Workflow.business_code.like(f"{_BUSINESS_CODE_PREFIX}{docket_id}%"))
        .order_by(Workflow.id.asc())
        .all()
    )
    if not rows:
        return []

    task_name = _derive_task_name_with_role(docket_item, None)
    due_date = _effective_due(docket_item)
    legal_due = _legal_due(docket_item)
    synced: list[Workflow] = []
    for wf in rows:
        _apply_docket_updates(
            wf=wf,
            docket_item=docket_item,
            task_name=task_name,
            due_date=due_date,
            legal_due=legal_due,
            assignee_id=getattr(wf, "assignee_id", None),
            attorney_assignee_id=getattr(wf, "attorney_assignee_id", None),
            manager_assignee_id=getattr(wf, "inspector_id", None),
            business_code_assignee_id=_business_code_assignee_id_for_workflow(wf),
        )
        db.session.add(wf)
        synced.append(wf)
    return synced


def _resolve_assignee_id(owner_staff_party_id: str | None) -> int | None:
    return resolve_user_id_by_staff_party_id(owner_staff_party_id)


def _resolve_primary_staff_party_id_from_matter(
    case_id: str,
    *,
    prefer_mgmt: bool = False,
) -> str | None:
    """
    Best-effort owner resolution for legacy/auto dockets that were created without an owner.

    This is intentionally conservative and only used for well-known auto patterns
    (e.g. USPTO:* dockets) to prevent "fallback_to_all" workflow explosions.
    """
    mid = (case_id or "").strip()
    if not mid:
        return None

    role_priority = (
        ("manager", "mgmt", "attorney", "retainer", "handler", "staff", "draftsman")
        if prefer_mgmt
        else ("attorney", "retainer", "handler", "staff", "draftsman", "manager", "mgmt")
    )
    valid_roles = set(role_priority)

    try:
        rows = db.session.execute(
            text("""
                SELECT msa.staff_role_code, msa.staff_party_id, COALESCE(msa.seq, 1) AS seq_ord, msa.msa_id
                FROM matter_staff_assignment msa
                JOIN users u ON u.staff_party_id = msa.staff_party_id
                WHERE msa.matter_id = :mid
                  AND msa.staff_party_id IS NOT NULL
                  AND TRIM(msa.staff_party_id) <> ''
                  AND LOWER(TRIM(msa.staff_role_code)) IN (
                    'attorney','retainer','handler','staff','draftsman','manager','mgmt'
                  )
                  AND u.is_active = TRUE
                ORDER BY COALESCE(msa.seq, 1) ASC, msa.msa_id ASC, u.id ASC
                """).execution_options(policy_bypass=True),
            {"mid": mid},
        ).fetchall()
        by_role: dict[str, str] = {}
        for role, spid, _seq_ord, _msa_id in rows or []:
            r = (role or "").strip().lower()
            s = (spid or "").strip()
            if not r or not s or r not in valid_roles:
                continue
            if r not in by_role:
                by_role[r] = s
        for r in role_priority:
            if r in by_role:
                return by_role[r]
        if by_role:
            return next(iter(by_role.values()))
    except Exception:
        logger.exception("Failed to resolve owner staff_party_id from matter_staff_assignment")

    # Fallback: CaseFlatIndex stores User.id; map to User.staff_party_id.
    try:
        idx = CaseFlatIndex.query.get(mid)
        if idx:
            candidates = (
                (idx.manager_id, idx.attorney_id, idx.handler_id)
                if prefer_mgmt
                else (idx.attorney_id, idx.handler_id, idx.manager_id)
            )
            for raw in candidates:
                try:
                    uid = int(raw) if raw is not None else None
                except Exception:
                    uid = None
                if not uid:
                    continue
                u = User.query.get(uid)
                if not u or not bool(getattr(u, "is_active", False)):
                    continue
                spid = (getattr(u, "staff_party_id", None) or "").strip() if u else ""
                if spid:
                    return spid
    except Exception:
        logger.exception("Failed to resolve owner staff_party_id from CaseFlatIndex/User fallback")

    return None


def _prefer_mgmt_owner_resolution_for_docket(docket_item: DocketItem) -> bool:
    name_ref_upper = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    category_upper = (getattr(docket_item, "category", None) or "").strip().upper()
    return name_ref_upper.startswith("MGMT:") or category_upper in ("MGMT", "SLA", "ADMIN")


def _resolve_owner_assignee_id_for_docket(docket_item: DocketItem) -> int | None:
    owner_staff_party_id = (getattr(docket_item, "owner_staff_party_id", None) or "").strip()
    if not owner_staff_party_id:
        case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
        if case_id:
            owner_staff_party_id = (
                _resolve_primary_staff_party_id_from_matter(
                    case_id,
                    prefer_mgmt=_prefer_mgmt_owner_resolution_for_docket(docket_item),
                )
                or ""
            )
            if owner_staff_party_id:
                docket_item.owner_staff_party_id = owner_staff_party_id
                db.session.add(docket_item)
    return _resolve_assignee_id(owner_staff_party_id or None)


def _resolve_owner_staff_party_id_for_workflow(workflow: Workflow) -> str | None:
    normalized_category = normalize_workflow_category(getattr(workflow, "category", None))
    primary_owner_user_id = workflow_primary_owner_user_id(
        category=normalized_category,
        handler_id=getattr(workflow, "assignee_id", None),
        attorney_id=getattr(workflow, "attorney_assignee_id", None),
        manager_id=getattr(workflow, "inspector_id", None),
    )
    if primary_owner_user_id:
        user = User.query.get(primary_owner_user_id)
        if user:
            staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
            if staff_party_id:
                return staff_party_id

    case_id = str(getattr(workflow, "case_id", "") or "").strip()
    if not case_id:
        return None

    return _resolve_primary_staff_party_id_from_matter(
        case_id,
        prefer_mgmt=(normalized_category == "MGMT"),
    )


def _resolve_docket_category_for_workflow(
    workflow: Workflow,
    *,
    owner_staff_party_id: str | None = None,
) -> str:
    normalized_category = normalize_workflow_category(getattr(workflow, "category", None))
    if normalized_category:
        return normalized_category

    try:
        from app.utils.task_classification import determine_category_by_staff_role

        case_id = str(getattr(workflow, "case_id", "") or "").strip() or None
        if owner_staff_party_id:
            derived = determine_category_by_staff_role(case_id, staff_party_id=owner_staff_party_id)
            if derived:
                return str(derived).strip().upper()
    except Exception:
        logger.exception(
            "Failed to derive docket category from workflow owner (workflow=%s)",
            getattr(workflow, "id", None),
        )
    return "WORK"


def _configured_owner_fallback_assignee_id() -> int | None:
    if not has_app_context():
        return None
    raw = current_app.config.get("WORKFLOW_OWNER_FALLBACK_USER_ID")
    if raw in (None, "", 0):
        return None
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid WORKFLOW_OWNER_FALLBACK_USER_ID config: %r", raw)
        return None
    if user_id <= 0:
        return None
    user = User.query.get(user_id)
    if not user or not bool(getattr(user, "is_active", False)):
        logger.warning(
            "Configured WORKFLOW_OWNER_FALLBACK_USER_ID=%s is missing or inactive",
            user_id,
        )
        return None
    return int(user.id)


def _backfill_owner_for_known_auto_dockets(docket_item: DocketItem) -> None:
    """
    Some legacy auto-ingestion paths created dockets without owner_staff_party_id.
    With fallback_to_all distribution, that explodes into multiple workflows.

    Backfill owner for safe/known patterns only.
    """
    if not docket_item:
        return

    owner = (getattr(docket_item, "owner_staff_party_id", None) or "").strip()
    if owner:
        return

    # Only apply to well-known auto-generated patterns.
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    name_ref_upper = name_ref.upper()
    if not (
        name_ref_upper.startswith("USPTO:")
        or name_ref_upper.startswith("USPTO_OA:")
        or name_ref_upper.startswith("MGMT:STATUS_RED:")
        or name_ref == "Application"
    ):
        return

    case_id = str(getattr(docket_item, "matter_id", "") or "")
    prefer_mgmt = _prefer_mgmt_owner_resolution_for_docket(docket_item)
    resolved = _resolve_primary_staff_party_id_from_matter(case_id, prefer_mgmt=prefer_mgmt)
    if not resolved:
        return

    docket_item.owner_staff_party_id = resolved
    db.session.add(docket_item)


def _docket_business_code(docket_id: str | None, assignee_id: int | None) -> str:
    """Generate unified business_code for docket-based workflows.

    All code paths MUST use this function to ensure consistency.
    Format: DOCKET:{docket_id}:{assignee_id} or DOCKET:{docket_id} if no assignee
    """
    docket_id = (docket_id or "").strip()
    if not docket_id:
        return ""
    if assignee_id is not None:
        return f"{_BUSINESS_CODE_PREFIX}{docket_id}:{assignee_id}"
    return f"{_BUSINESS_CODE_PREFIX}{docket_id}"


def _workflow_id_from_docket_id(docket_id: str | None) -> int | None:
    raw = (docket_id or "").strip()
    if not raw:
        return None
    match = _WF_DOCKET_RE.match(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _mark_orphaned_workflow_generated_docket_deleted(docket_item: DocketItem) -> bool:
    """Retire WF-* docket rows whose source Workflow no longer exists."""
    wf_id = _workflow_id_from_docket_id(getattr(docket_item, "docket_id", None))
    if not wf_id:
        return False
    wf = db.session.get(Workflow, int(wf_id))
    if wf is not None and str(getattr(wf, "case_id", "") or "") == str(docket_item.matter_id):
        return False
    docket_item.is_deleted = True
    docket_item.deleted_at = datetime.utcnow()
    docket_item.delete_reason = "orphaned_workflow_generated_docket"
    db.session.add(docket_item)
    return True


def _find_existing_workflow(
    *,
    docket_item: DocketItem,
    assignee_id: int | None,
    prefer_canonical: bool = False,
) -> Workflow | None:
    docket_id = (docket_item.docket_id or "").strip()
    case_id = str(docket_item.matter_id)

    if _workflow_id_from_docket_id(docket_id):
        return None

    if docket_id:
        canonical_bc = _docket_business_code(docket_id, None)
        if prefer_canonical and canonical_bc:
            wf = Workflow.query.filter_by(case_id=case_id, business_code=canonical_bc).first()
            if wf:
                return wf

        # Build business_code with unified function
        bc = _docket_business_code(docket_id, assignee_id)
        if bc:
            wf = Workflow.query.filter_by(case_id=case_id, business_code=bc).first()
            if wf:
                return wf
        # Fallback: try canonical format without assignee_id.
        if canonical_bc:
            wf = Workflow.query.filter_by(
                case_id=case_id,
                business_code=canonical_bc,
            ).first()
            if wf and (
                assignee_id is None
                or wf.assignee_id is None
                or wf.assignee_id == assignee_id
                or getattr(wf, "attorney_assignee_id", None) == assignee_id
                or getattr(wf, "inspector_id", None) == assignee_id
            ):
                return wf

    wf_id = _workflow_id_from_docket_id(docket_id)
    if wf_id:
        wf = Workflow.query.get(wf_id)
        if wf and str(wf.case_id) == case_id:
            return wf
    return None


def _resolve_assignee_ids_from_matter(case_id: str) -> list[int]:
    """Resolve all assignee user IDs for a matter.

    Priority:
    1. CaseFlatIndex (fast indexed lookup)
    2. MatterStaffAssignment (authoritative source)
    """
    ids = set()

    def _add(val):
        try:
            if val:
                ids.add(int(val))
        except (ValueError, TypeError):
            pass

    # 1. Try CaseFlatIndex first (fast)
    idx = CaseFlatIndex.query.get(case_id)
    if idx:
        candidate_ids: list[int] = []
        for raw in (idx.manager_id, idx.attorney_id, idx.handler_id):
            try:
                uid = int(raw) if raw is not None else None
            except (ValueError, TypeError):
                uid = None
            if uid:
                candidate_ids.append(uid)
        if candidate_ids:
            active_rows = (
                db.session.query(User.id)
                .filter(User.id.in_(candidate_ids), User.is_active.is_(True))
                .all()
            )
            for row in active_rows:
                _add(row[0])

    # 2. Fallback to MatterStaffAssignment if CaseFlatIndex is empty
    if not ids:
        try:
            from app.utils.policy_sql import policy_text as text

            rows = db.session.execute(
                text("""
                SELECT u.id
                FROM users u
                JOIN party_staff ps ON u.staff_party_id = ps.party_id
                JOIN matter_staff_assignment msa ON msa.staff_party_id = ps.party_id
                WHERE msa.matter_id = :mid
                  AND LOWER(TRIM(msa.staff_role_code)) IN ('attorney', 'retainer', 'manager', 'mgmt', 'handler', 'staff', 'draftsman')
                  AND u.is_active = TRUE
            """).execution_options(policy_bypass=True),
                {"mid": case_id},
            ).fetchall()
            for row in rows:
                _add(row[0])
        except Exception:
            logger.exception(f"MatterStaffAssignment fallback failed for case {case_id}")

    return list(ids)


def _normalize_assignment_role(role_code: str | None) -> str | None:
    role = (role_code or "").strip().lower()
    if role in {"manager", "mgmt"}:
        return "manager"
    if role in {"attorney", "retainer"}:
        return "attorney"
    if role in {"handler", "staff", "draftsman"}:
        return "handler"
    if role in {"owner", "fallback"}:
        return role
    if not role:
        return None
    return role


def _resolve_multi_role_assignees(assignees: list) -> tuple[int | None, int | None, int | None]:
    manager_id: int | None = None
    attorney_id: int | None = None
    handler_id: int | None = None
    fallback_ids: list[int] = []

    for info in assignees or []:
        try:
            uid = int(getattr(info, "user_id", None) or 0)
        except Exception:
            uid = 0
        if uid <= 0:
            continue

        role = _normalize_assignment_role(getattr(info, "role_code", None))
        if role == "manager":
            if manager_id is None:
                manager_id = uid
            continue
        if role == "attorney":
            if attorney_id is None:
                attorney_id = uid
            continue
        if role == "handler":
            if handler_id is None:
                handler_id = uid
            continue
        if uid not in fallback_ids:
            fallback_ids.append(uid)

    # Manager-only tasks: do not force "handler" to equal manager.
    # This avoids incorrectly classifying/labeling the task as mixed (MGMT_WORK).
    if manager_id is not None and handler_id is None and attorney_id is None and not fallback_ids:
        return None, None, manager_id

    if handler_id is None and fallback_ids:
        handler_id = fallback_ids[0]
    if attorney_id is None and fallback_ids:
        attorney_id = fallback_ids[0]

    return handler_id, attorney_id, manager_id


def _derive_workflow_category_for_assignments(
    *,
    case_id: str,
    docket_item: DocketItem,
    handler_assignee_id: int | None,
    attorney_assignee_id: int | None,
    manager_assignee_id: int | None,
) -> str:
    return derive_workflow_category(
        case_id=case_id,
        handler_id=handler_assignee_id,
        attorney_id=attorney_assignee_id,
        manager_id=manager_assignee_id,
        hint_category=docket_item.category,
        hint_name_ref=docket_item.name_ref,
        hint_name_free=docket_item.name_free,
        source=(getattr(docket_item, "source", None) or "").strip().lower() or None,
    )


def _current_staff_snapshot(case_id: str) -> dict[str, str]:
    """Get current staff names for a case. Uses SAVEPOINT to prevent transaction pollution on errors."""
    cache = _get_task_sync_cache()
    cached = cache["staff_snapshot_by_case"].get(case_id)
    if cached is not None:
        return dict(cached)
    snapshot = {"attorney": "", "handler": "", "manager": ""}
    try:
        # Use SAVEPOINT so query failure doesn't abort outer transaction
        with db.session.begin_nested():
            sql = text("""
                SELECT msa.staff_role_code, p.name_display
                FROM matter_staff_assignment msa
                JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                JOIN party p ON p.party_id = ps.party_id
                WHERE msa.matter_id = :mid
                  AND LOWER(TRIM(msa.staff_role_code)) IN ('attorney', 'retainer', 'handler', 'staff', 'draftsman', 'manager', 'mgmt')
                """).execution_options(policy_bypass=True)
            rows = db.session.execute(sql, {"mid": case_id}).all()
            bucket = {"attorney": [], "handler": [], "manager": []}
            for role, name in rows:
                r = (role or "").strip().lower()
                n = (name or "").strip()
                if not n:
                    continue
                if r in ("attorney", "retainer"):
                    if n not in bucket["attorney"]:
                        bucket["attorney"].append(n)
                elif r in ("handler", "staff", "draftsman"):
                    if n not in bucket["handler"]:
                        bucket["handler"].append(n)
                elif r in ("manager", "mgmt"):
                    if n not in bucket["manager"]:
                        bucket["manager"].append(n)
            for key in snapshot:
                snapshot[key] = ", ".join(bucket[key]).strip()
    except Exception:
        # SAVEPOINT rolled back automatically, outer transaction still usable
        logger.exception(f"Failed to get staff snapshot for case {case_id}")
        snapshot = {"attorney": "", "handler": "", "manager": ""}

    if not any(snapshot.values()):
        try:
            with db.session.begin_nested():
                row = db.session.execute(
                    text("""
                        SELECT data
                        FROM matter_custom_field
                        WHERE matter_id = :mid AND namespace = 'basic'
                        LIMIT 1
                        """).execution_options(policy_bypass=True),
                    {"mid": case_id},
                ).scalar()
                if isinstance(row, dict):
                    snapshot["attorney"] = (row.get("attorney") or "").strip()
                    snapshot["handler"] = (row.get("handler") or "").strip()
                    snapshot["manager"] = (row.get("manager") or "").strip()
        except Exception:
            logger.exception(f"Custom field fallback failed for case {case_id}")

    cache["staff_snapshot_by_case"][case_id] = dict(snapshot)
    return snapshot


def _derive_task_name_with_role(docket_item: DocketItem, assignee_id: int | None) -> str:
    # Now handled by distinct DocketItems (Work vs Mgmt)
    title = (docket_item.name_free or docket_item.name_ref or "").strip()
    kind = _workflow_generated_docket_kind(docket_item)
    if not kind:
        return title
    return workflow_deadline_title(
        title,
        kind,
        legal_due_date=_legal_due(docket_item),
        effective_due_date=_effective_due(docket_item),
    )


def _strip_auto_cleanup_markers(note: str | None) -> str | None:
    text_note = (note or "").strip()
    if not text_note:
        return None
    cleaned = text_note
    for marker in _AUTO_CLEANUP_NOTE_MARKERS:
        cleaned = cleaned.replace(marker, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _append_note_marker(note: str | None, marker: str | None) -> str | None:
    marker = (marker or "").strip()
    if not marker:
        return (note or "").strip() or None
    text_note = (note or "").strip()
    if marker in text_note:
        return text_note or None
    return f"{text_note} {marker}".strip()


def _apply_docket_updates(
    *,
    wf: Workflow,
    docket_item: DocketItem,
    task_name: str,
    due_date: date | None,
    legal_due: date | None,
    assignee_id: int | None,
    attorney_assignee_id: int | None | object = _UNSET,
    manager_assignee_id: int | None | object = _UNSET,
    business_code_assignee_id: int | None | object = _UNSET,
) -> None:
    case_id = str(docket_item.matter_id)

    if not (wf.snapshot_attorney or wf.snapshot_handler or wf.snapshot_manager):
        snap = _current_staff_snapshot(case_id)
        wf.snapshot_attorney = snap.get("attorney") or None
        wf.snapshot_handler = snap.get("handler") or None
        wf.snapshot_manager = snap.get("manager") or None

    bc_assignee = assignee_id if business_code_assignee_id is _UNSET else business_code_assignee_id
    business_code = _docket_business_code(docket_item.docket_id, bc_assignee)
    if business_code and (wf.business_code or "").strip() != business_code:
        wf.business_code = business_code

    sync_workflow_due_dates_from_docket_source(
        wf,
        due_date=due_date,
        legal_due_date=legal_due,
    )
    if wf.assignee_id != assignee_id:
        wf.assignee_id = assignee_id
    if attorney_assignee_id is not _UNSET and wf.attorney_assignee_id != attorney_assignee_id:
        wf.attorney_assignee_id = attorney_assignee_id
    if manager_assignee_id is not _UNSET and wf.inspector_id != manager_assignee_id:
        wf.inspector_id = manager_assignee_id
    if wf.name != task_name:
        wf.name = task_name

    if _is_auto_cancelled(docket_item) or _is_auto_expired(docket_item):
        wf.status = "Abandoned"
        parsed_done = parse_done_date(docket_item.done_date)
        wf.completed_date = parsed_done or date.today()
        return

    if not _is_done(docket_item):
        inferred_done = _infer_done_date_from_matter_signals(
            docket_item=docket_item,
            task_name=task_name,
        )
        if inferred_done and not (docket_item.done_date or "").strip():
            docket_item.done_date = inferred_done.isoformat()
            db.session.add(docket_item)

    if _is_done(docket_item):
        wf.status = "Completed"
        parsed_done = parse_done_date(docket_item.done_date)
        if parsed_done:
            wf.completed_date = parsed_done
        elif not wf.completed_date:
            wf.completed_date = date.today()
        return

    if wf.status in ("Completed", "Abandoned"):
        wf.status = "Pending"
        wf.completed_date = None
        wf.note = _strip_auto_cleanup_markers(wf.note)


def _ensure_workflow_for_assignee(
    *,
    docket_item: DocketItem,
    assignee_id: int | None,
    attorney_assignee_id: int | None | object = _UNSET,
    manager_assignee_id: int | None | object = _UNSET,
    prefer_canonical: bool = False,
    canonical_business_code: bool = False,
    created_by_id: int | None = None,
) -> Workflow | None:
    task_name = _derive_task_name_with_role(docket_item, assignee_id)
    due_date = _effective_due(docket_item)
    legal_due = _legal_due(docket_item)
    case_id = str(docket_item.matter_id)
    effective_attorney = None if attorney_assignee_id is _UNSET else attorney_assignee_id
    effective_manager = None if manager_assignee_id is _UNSET else manager_assignee_id
    business_code_assignee = None if canonical_business_code else assignee_id

    lookup_assignee_id = assignee_id
    if lookup_assignee_id is None:
        lookup_assignee_id = effective_attorney
    if lookup_assignee_id is None:
        lookup_assignee_id = effective_manager

    wf = _find_existing_workflow(
        docket_item=docket_item,
        assignee_id=lookup_assignee_id,
        prefer_canonical=prefer_canonical,
    )
    if wf:
        _apply_docket_updates(
            wf=wf,
            docket_item=docket_item,
            task_name=task_name,
            due_date=due_date,
            legal_due=legal_due,
            assignee_id=assignee_id,
            attorney_assignee_id=attorney_assignee_id,
            manager_assignee_id=manager_assignee_id,
            business_code_assignee_id=business_code_assignee,
        )
        wf.category = _derive_workflow_category_for_assignments(
            case_id=case_id,
            docket_item=docket_item,
            handler_assignee_id=wf.assignee_id,
            attorney_assignee_id=getattr(wf, "attorney_assignee_id", None),
            manager_assignee_id=getattr(wf, "inspector_id", None),
        )
        db.session.add(wf)

        return wf

    wf_id = _workflow_id_from_docket_id(docket_item.docket_id)
    if wf_id:
        return None

    if not due_date and not legal_due:
        return None

    # Import role-based category function
    business_code = _docket_business_code(docket_item.docket_id, business_code_assignee)
    if not business_code:
        return None

    # ========== CREATE NEW WORKFLOW ==========
    wf = Workflow(
        case_id=case_id,
        name=task_name,
        status="Pending",
        due_date=due_date or legal_due,
        legal_due_date=legal_due,
        assignee_id=assignee_id,
        attorney_assignee_id=effective_attorney,
        inspector_id=effective_manager,
        created_by_id=created_by_id,
        category=_derive_workflow_category_for_assignments(
            case_id=case_id,
            docket_item=docket_item,
            handler_assignee_id=assignee_id,
            attorney_assignee_id=effective_attorney,
            manager_assignee_id=effective_manager,
        ),
        note=" Create: DocketItem ",
        business_code=business_code,
    )
    snap = _current_staff_snapshot(case_id)
    wf.snapshot_attorney = snap.get("attorney") or None
    wf.snapshot_handler = snap.get("handler") or None
    wf.snapshot_manager = snap.get("manager") or None
    _apply_docket_updates(
        wf=wf,
        docket_item=docket_item,
        task_name=task_name,
        due_date=due_date,
        legal_due=legal_due,
        assignee_id=assignee_id,
        attorney_assignee_id=attorney_assignee_id,
        manager_assignee_id=manager_assignee_id,
        business_code_assignee_id=business_code_assignee,
    )

    # Add and flush inside SAVEPOINT so rollback is scoped correctly
    try:
        with db.session.begin_nested():
            db.session.add(wf)
            db.session.flush()
    except IntegrityError as e:
        logger.warning("Workflow upsert conflict for %s: %s", business_code, e)
        try:
            db.session.expunge(wf)
        except Exception:
            logger.debug("Failed to expunge workflow object from session")
        existing = Workflow.query.filter_by(
            case_id=case_id,
            business_code=business_code,
        ).first()
        if existing:
            _apply_docket_updates(
                wf=existing,
                docket_item=docket_item,
                task_name=task_name,
                due_date=due_date,
                legal_due=legal_due,
                assignee_id=assignee_id,
                attorney_assignee_id=attorney_assignee_id,
                manager_assignee_id=manager_assignee_id,
                business_code_assignee_id=business_code_assignee,
            )
            existing.category = _derive_workflow_category_for_assignments(
                case_id=case_id,
                docket_item=docket_item,
                handler_assignee_id=existing.assignee_id,
                attorney_assignee_id=getattr(existing, "attorney_assignee_id", None),
                manager_assignee_id=getattr(existing, "inspector_id", None),
            )
            db.session.add(existing)
            return existing
        return None
    except Exception as e:
        logger.warning(f"Failed to create workflow {wf.name}: {e}")
        try:
            db.session.expunge(wf)
        except Exception:
            logger.debug("Failed to expunge workflow object from session")
        return None

    return wf


def _should_skip_workflow_for_docket(docket_item: DocketItem) -> bool:
    """Check if workflow creation should be skipped for this docket.

    Skip if this is a simple 'Application' marker and a full 'ApplicationDeadline' workflow already exists.
    """
    category = (docket_item.category or "").strip().upper()
    name_ref = (docket_item.name_ref or "").strip()
    name_free = (docket_item.name_free or "").strip()
    case_id = str(docket_item.matter_id)

    # Annuity state is managed by annuity_item -> ANNUITY workflow sync.
    # Skip duplicate status-red dockets like "MGMT:STATUS_RED:4RenewalDeadline".
    if is_annuity_status_red_deadline(name_ref=name_ref, title=(name_free or name_ref)):
        return True

    if _is_non_work_status_red_docket(docket_item):
        return True

    # Renewal-managed term-expiry rows are status markers, not actionable work.
    if _is_renewal_managed_term_expiry_docket(docket_item):
        return True

    # OA Status RED Matter  Status  ,  OA  
    # (NOTICE:OA:*)     Workflow  .
    if _matching_open_oa_due_docket_for_status_red(docket_item) is not None:
        return True

    # ============================================================
    # OA (Office Action) de-duplication rules
    # ============================================================
    # We keep multiple DocketItems for notifications/visibility (attorney/handler/manager),
    # but we only want ONE Workflow to represent the actual response work.
    #
    # - Skip manager helper docket: MGMT:NOTICE:OA:<oa_id>
    # - Skip handler helper docket: NOTICE:OA:<oa_id>:HDL
    # - Skip the raw USPTO notice docket: USPTO:<oa_id> when the OA response dockets exist
    #   (prevents the duplicate "Notice" workflow).
    name_ref_upper = name_ref.upper()
    if name_ref_upper.startswith("MGMT:NOTICE:OA:"):
        return True
    if re.match(r"^NOTICE:OA:[^:]+:HDL$", name_ref, re.IGNORECASE):
        return True
    if name_ref_upper.startswith("USPTO:"):
        try:
            oa_id = name_ref.split(":", 1)[1].strip()
        except Exception:
            oa_id = ""
        if oa_id:
            related_refs = (
                f"NOTICE:OA:{oa_id}",
                f"MGMT:NOTICE:OA:{oa_id}",
            )
            related = (
                DocketItem.query.filter(DocketItem.matter_id == case_id)
                .filter(DocketItem.name_ref.in_(list(related_refs)))
                .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
                .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
                .first()
            )
            if related is not None:
                return True

    # Legacy migrated V2_LIMIT rows (blank name_ref) may coexist with the newer
    # MGMT:STATUS_RED task for the same case/due date. In that situation, treat
    # the V2_LIMIT row as reference-only so we don't create duplicate workflows.
    if not name_ref and category == "V2_LIMIT":
        if _matching_status_red_peer_for_legacy_v2_limit(
            docket_item=docket_item,
            open_only=True,
        ):
            return True

        # Legacy generic payment notices ("Department") can carry very long statutory dates
        # and should not generate operational workflow noise.
        due = _effective_due(docket_item) or _legal_due(docket_item)
        name_compact = re.sub(r"\s+", "", name_free or "")
        if due and name_compact in {"Payment", "Department"} and (due - date.today()).days > 365:
            return True

    if _distribution_decision_is_none_for_docket(docket_item):
        return True

    # Only treat true "Application" marker tasks as filing duplicates.
    # Broad substring checks (e.g. "Application" in "ForeignApplicationDeadline") incorrectly skipped
    # MGMT status-red workflows and auto-abandoned valid tasks.
    name_free_compact = re.sub(r"\s+", "", name_free or "")
    is_filing = (
        category == "FILING"
        or name_ref in ("Application", "Application ()")
        or name_free_compact in ("ApplicationDeadline", "ApplicationDeadline()")
    )
    if not is_filing:
        return False

    due_date = _effective_due(docket_item) or _legal_due(docket_item)
    # Check for existing filing deadline workflow by name only (simplified logic)
    q = Workflow.query.filter_by(case_id=case_id).filter(
        or_(
            Workflow.name.ilike("%ApplicationDeadline%"),
            Workflow.name.ilike("%Application Deadline%"),
        )
    )
    q = q.filter(or_(Workflow.status.is_(None), Workflow.status.notin_(("Completed", "Abandoned"))))
    docket_id = (docket_item.docket_id or "").strip()
    if docket_id:
        # Do not treat workflows from the same docket as duplicates.
        # Otherwise, normal re-sync can self-skip and trigger incorrect cleanup.
        prefix = f"{_BUSINESS_CODE_PREFIX}{docket_id}"
        q = q.filter(
            or_(Workflow.business_code.is_(None), ~Workflow.business_code.like(f"{prefix}%"))
        )
    if due_date:
        q = q.filter(or_(Workflow.due_date == due_date, Workflow.legal_due_date == due_date))
    return q.first() is not None


def _reconcile_distributed_workflows(
    *,
    docket_item: DocketItem,
    target_assignee_ids: set[int | None],
    keep_workflow_id: int | list[int] | set[int] | None = None,
    delete_non_target: bool = False,
) -> None:
    docket_id = (docket_item.docket_id or "").strip()
    if not docket_id:
        return
    case_id = str(docket_item.matter_id)
    prefix = f"{_BUSINESS_CODE_PREFIX}{docket_id}"
    canonical_bc = _docket_business_code(docket_id, None)
    existing = (
        Workflow.query.filter_by(case_id=case_id)
        .filter(Workflow.business_code.like(f"{prefix}%"))
        .all()
    )

    keep_ids = set()
    if keep_workflow_id is not None:
        if isinstance(keep_workflow_id, (list, set, tuple)):
            keep_ids = {int(x) for x in keep_workflow_id if x}
        else:
            keep_ids = {int(keep_workflow_id)}

    for wf in existing:
        wf_id_val = int(getattr(wf, "id", 0) or 0)
        if keep_ids and wf_id_val in keep_ids:
            continue
        if delete_non_target and keep_ids:
            if wf.status == "Completed":
                continue
            wf_id = int(wf.id) if getattr(wf, "id", None) else None
            if wf_id:
                _delete_workflow_for_distribution_cleanup(workflow_id=wf_id)
            else:
                db.session.delete(wf)
            continue
        if wf.assignee_id in target_assignee_ids:
            expected_bc = _docket_business_code(docket_id, wf.assignee_id)
            current_bc = (wf.business_code or "").strip()
            if expected_bc and current_bc not in {expected_bc, canonical_bc}:
                wf.business_code = expected_bc
                db.session.add(wf)
            continue
        if delete_non_target and wf.status != "Completed":
            wf_id = int(wf.id) if getattr(wf, "id", None) else None
            if wf_id:
                _delete_workflow_for_distribution_cleanup(workflow_id=wf_id)
            else:
                db.session.delete(wf)
            continue
        # Keep explicit completion history only.
        # Non-target helper workflows are deleted so they do not appear as "".
        if wf.status == "Completed":
            continue
        wf_id = int(wf.id) if getattr(wf, "id", None) else None
        if wf_id:
            _delete_workflow_for_distribution_cleanup(workflow_id=wf_id)
        else:
            db.session.delete(wf)


def _delete_workflow_for_distribution_cleanup(
    *,
    workflow_id: int,
) -> None:
    wf_id = int(workflow_id)
    wf = db.session.get(Workflow, wf_id)

    try:
        from app.services.case.cascade_delete_service import delete_workflow_fk_children

        with db.session.begin_nested():
            delete_workflow_fk_children(wf_id)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="task_sync.reconcile_distributed_workflows.delete_fk_children",
            log_key="task_sync.reconcile_distributed_workflows.delete_fk_children",
            log_window_seconds=300,
        )

    try:
        with db.session.begin_nested():
            if wf is not None:
                db.session.delete(wf)
            else:
                Workflow.query.filter(Workflow.id == wf_id).delete(synchronize_session=False)
    except (ObjectDeletedError, StaleDataError):
        logger.info("Workflow already deleted during distribution cleanup (workflow_id=%s)", wf_id)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="task_sync.reconcile_distributed_workflows.delete_workflow",
            log_key="task_sync.reconcile_distributed_workflows.delete_workflow",
            log_window_seconds=300,
        )


def _cleanup_skipped_docket_workflows(
    docket_item: DocketItem,
    *,
    delete_auto_generated: bool = False,
) -> set[int]:
    docket_id = (getattr(docket_item, "docket_id", None) or "").strip()
    case_id = str(getattr(docket_item, "matter_id", "") or "").strip()
    if not docket_id or not case_id:
        return set()

    prefix = f"{_BUSINESS_CODE_PREFIX}{docket_id}"
    existing = (
        Workflow.query.filter_by(case_id=case_id)
        .filter(Workflow.business_code.like(f"{prefix}%"))
        .all()
    )
    if not existing:
        return set()

    today = date.today()
    changed_workflow_ids: set[int] = set()
    delete_completed_auto_generated = _is_non_work_status_red_docket(
        docket_item
    ) or _is_renewal_managed_term_expiry_docket(docket_item)

    with db.session.begin_nested():
        for wf in existing:
            note = (wf.note or "").strip()
            if (
                (" Create" not in note)
                and (not note.startswith("Create"))
                and ("USPTO Notice  Create" not in note)
            ):
                continue

            wf_id = int(wf.id) if getattr(wf, "id", None) else None
            if delete_auto_generated:
                if wf.status == "Completed" and not delete_completed_auto_generated:
                    continue
                if wf_id:
                    _delete_workflow_for_distribution_cleanup(workflow_id=wf_id)
                    changed_workflow_ids.add(wf_id)
                else:
                    db.session.delete(wf)
                continue

            if wf.status in ("Completed", "Abandoned"):
                continue

            wf.status = "Abandoned"
            wf.completed_date = wf.completed_date or today
            wf.note = f"{note} [:  Create ]".strip()
            db.session.add(wf)
            if wf_id:
                changed_workflow_ids.add(wf_id)

    if delete_auto_generated:
        return changed_workflow_ids

    if changed_workflow_ids:
        try:
            from app.services.workflow.sync_requests import enqueue_workflow_sync

            for wf_id in changed_workflow_ids:
                enqueue_workflow_sync(workflow_id=wf_id)
        except Exception as e:
            logger.warning(
                "Failed to enqueue workflow sync for skipped docket cleanup (%s): %s",
                sorted(changed_workflow_ids),
                e,
            )

    return changed_workflow_ids


def ensure_workflow_for_docket(
    *,
    docket_item: DocketItem,
    created_by_id: int | None = None,
) -> list[Workflow]:
    if _workflow_id_from_docket_id(getattr(docket_item, "docket_id", None)):
        return []

    try:
        _maybe_apply_terminal_done_value_to_docket(docket_item)
    except Exception:
        logger.exception(
            "Failed to apply terminal docket guardrail for docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    if _is_done(docket_item):
        if _should_skip_workflow_for_docket(docket_item):
            _cleanup_worklogs_for_skipped_docket(docket_item)
            _cleanup_skipped_docket_workflows(
                docket_item,
                delete_auto_generated=_should_delete_skipped_docket_workflows(docket_item),
            )
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=True,
            )
            return []
        try:
            ensure_worklog_for_docket(docket_item=docket_item, actor_id=created_by_id)
        except Exception:
            logger.exception(
                "Failed to reconcile worklog for terminal docket=%s",
                getattr(docket_item, "docket_id", None),
            )
        return _sync_existing_workflows_for_terminal_docket(docket_item)

    if _enforce_latest_open_uspto_oa_opinion_deadline(docket_item=docket_item):
        return []

    if _is_non_work_status_red_docket(docket_item):
        _cleanup_worklogs_for_skipped_docket(docket_item)
        _cleanup_skipped_docket_workflows(
            docket_item,
            delete_auto_generated=True,
        )
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=set(),
            delete_non_target=True,
        )
        return []

    if not is_visible_by_date(docket_item):
        if not _is_supported_open_core_status_red_deadline(docket_item):
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=False,
            )
        return []

    try:
        _cleanup_duplicate_status_red_workflows_for_oa_due(docket_item=docket_item)
    except Exception:
        logger.exception(
            "Failed to cleanup duplicate status-red workflows for OA docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    if _should_skip_workflow_for_docket(docket_item):
        _cleanup_worklogs_for_skipped_docket(docket_item)
        _cleanup_skipped_docket_workflows(
            docket_item,
            delete_auto_generated=_should_delete_skipped_docket_workflows(docket_item),
        )
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=set(),
        )
        return []

    assignees, decision = resolve_assignees_for_docket(
        docket_item,
        return_decision=True,
    )
    manual_assignment = _manual_assignment_override_for_docket(docket_item)
    if manual_assignment is not None:
        handler_assignee_id, attorney_assignee_id, manager_assignee_id = manual_assignment
        target_assignee_ids = {
            uid
            for uid in (handler_assignee_id, attorney_assignee_id, manager_assignee_id)
            if uid is not None
        }
        wf = _ensure_workflow_for_assignee(
            docket_item=docket_item,
            assignee_id=handler_assignee_id,
            attorney_assignee_id=attorney_assignee_id,
            manager_assignee_id=manager_assignee_id,
            prefer_canonical=True,
            canonical_business_code=True,
            created_by_id=created_by_id,
        )
        if wf:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=target_assignee_ids,
                keep_workflow_id=wf.id,
                delete_non_target=True,
            )
            return [wf]
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=target_assignee_ids,
        )
        return []
    if decision.distribute_to not in {"owner", "role_set", "all_staff", "none"}:
        logger.error(
            "Unsupported distribute_to '%s' for docket=%s (rule=%s); skipping workflow creation",
            decision.distribute_to,
            getattr(docket_item, "docket_id", None),
            decision.rule_id,
        )
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=set(),
        )
        return []
    if decision.distribute_to == "none":
        _reconcile_distributed_workflows(
            docket_item=docket_item,
            target_assignee_ids=set(),
        )
        return []
    if not assignees:
        if decision.distribute_to in {"role_set", "all_staff"}:
            recovered_assignee_id: int | None = None
            owner_staff_party_id = (
                getattr(docket_item, "owner_staff_party_id", None) or ""
            ).strip()
            if owner_staff_party_id:
                try:
                    recovered_assignee_id = _resolve_owner_assignee_id_for_docket(docket_item)
                except Exception:
                    recovered_assignee_id = None

            if recovered_assignee_id is None:
                fallback_assignee_id = _configured_owner_fallback_assignee_id()
                if fallback_assignee_id is not None:
                    recovered_assignee_id = int(fallback_assignee_id)

            if recovered_assignee_id is not None:
                wf = _ensure_workflow_for_assignee(
                    docket_item=docket_item,
                    assignee_id=recovered_assignee_id,
                    prefer_canonical=True,
                    canonical_business_code=True,
                    created_by_id=created_by_id,
                )
                if wf:
                    wf.note = _append_note_marker(wf.note, _OWNER_RECOVERED_NOTE_MARKER)
                    db.session.add(wf)
                    _reconcile_distributed_workflows(
                        docket_item=docket_item,
                        target_assignee_ids={recovered_assignee_id},
                        keep_workflow_id=wf.id,
                        delete_non_target=True,
                    )
                    return [wf]

            logger.warning(
                "No assignees resolved for distribute_to=%s (rule=%s, docket=%s); skipping unassigned workflow",
                decision.distribute_to,
                decision.rule_id,
                getattr(docket_item, "docket_id", None),
            )
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
            )
            return []
        target_assignee_id: int | None = None
        note_marker: str | None = None
        if decision.distribute_to == "owner":
            try:
                target_assignee_id = _resolve_owner_assignee_id_for_docket(docket_item)
            except Exception:
                logger.exception(
                    "Failed owner-assignee recovery for docket=%s",
                    getattr(docket_item, "docket_id", None),
                )
                target_assignee_id = None
            if target_assignee_id is not None:
                note_marker = _OWNER_RECOVERED_NOTE_MARKER
            else:
                fallback_assignee_id = _configured_owner_fallback_assignee_id()
                if fallback_assignee_id is not None:
                    target_assignee_id = int(fallback_assignee_id)
                    note_marker = _OWNER_FALLBACK_NOTE_MARKER
                else:
                    note_marker = _OWNER_UNASSIGNED_NOTE_MARKER
                    logger.error(
                        "Owner distribution unresolved (docket=%s, matter=%s, rule=%s); creating unassigned workflow",
                        getattr(docket_item, "docket_id", None),
                        getattr(docket_item, "matter_id", None),
                        decision.rule_id,
                    )

        wf = _ensure_workflow_for_assignee(
            docket_item=docket_item,
            assignee_id=target_assignee_id,
            prefer_canonical=True,
            canonical_business_code=True,
            created_by_id=created_by_id,
        )
        if wf and note_marker:
            wf.note = _append_note_marker(wf.note, note_marker)
            db.session.add(wf)
        if wf:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids={target_assignee_id},
                keep_workflow_id=wf.id,
                delete_non_target=True,
            )
        return [wf] if wf else []

    handler_assignee_id, attorney_assignee_id, manager_assignee_id = _resolve_multi_role_assignees(
        assignees
    )
    target_assignee_ids = {
        int(getattr(info, "user_id", 0))
        for info in assignees
        if getattr(info, "user_id", None) is not None
    }
    target_assignee_ids = {uid for uid in target_assignee_ids if uid > 0}
    if handler_assignee_id is not None:
        target_assignee_ids.add(int(handler_assignee_id))
    if attorney_assignee_id is not None:
        target_assignee_ids.add(int(attorney_assignee_id))
    if manager_assignee_id is not None:
        target_assignee_ids.add(int(manager_assignee_id))

    workflows: list[Workflow] = []
    wf: Workflow | None = None
    try:
        wf = _ensure_workflow_for_assignee(
            docket_item=docket_item,
            assignee_id=handler_assignee_id,
            attorney_assignee_id=attorney_assignee_id,
            manager_assignee_id=manager_assignee_id,
            prefer_canonical=True,
            canonical_business_code=True,
            created_by_id=created_by_id,
        )
        if wf:
            workflows.append(wf)
    except Exception as e:
        logger.error(
            "Failed to ensure consolidated workflow for docket=%s: %s",
            getattr(docket_item, "docket_id", None),
            e,
        )

    # For the remaining staff members not covered by the consolidated workflow trio,
    # create individual workflows.
    consolidated_ids = {handler_assignee_id, attorney_assignee_id, manager_assignee_id}
    remaining_ids = {uid for uid in target_assignee_ids if uid not in consolidated_ids}

    for uid in remaining_ids:
        try:
            extra_wf = _ensure_workflow_for_assignee(
                docket_item=docket_item,
                assignee_id=uid,
                prefer_canonical=False,
                canonical_business_code=False,
                created_by_id=created_by_id,
            )
            if extra_wf:
                workflows.append(extra_wf)
        except Exception as e:
            logger.error(
                "Failed to ensure individual workflow for assignee=%s docket=%s: %s",
                uid,
                getattr(docket_item, "docket_id", None),
                e,
            )

    _reconcile_distributed_workflows(
        docket_item=docket_item,
        target_assignee_ids=target_assignee_ids,
        keep_workflow_id={w.id for w in workflows if getattr(w, "id", None)},
        delete_non_target=True,
    )
    return workflows


def _backfill_worklog_snapshots_from_workflow(wl: WorkLog, docket_item: DocketItem) -> None:
    """Fill WorkLog snapshots from the linked Workflow when DocketItem has no snapshot data."""
    docket_id = (docket_item.docket_id or "").strip()
    if not docket_id:
        return
    case_id = str(docket_item.matter_id)
    prefix = f"{_BUSINESS_CODE_PREFIX}{docket_id}"
    canonical_bc = _docket_business_code(docket_id, None)
    try:
        with db.session.no_autoflush:
            candidates = (
                Workflow.query.filter_by(case_id=case_id)
                .filter(Workflow.business_code.like(f"{prefix}%"))
                .order_by(
                    case((Workflow.business_code == canonical_bc, 0), else_=1),
                    case((Workflow.status.in_(("Pending", "In Progress")), 0), else_=1),
                    Workflow.id.asc(),
                )
                .all()
            )
        if not candidates:
            return
        linked_wf = next(
            (
                row
                for row in candidates
                if row.snapshot_attorney or row.snapshot_handler or row.snapshot_manager
            ),
            candidates[0],
        )
        if linked_wf.snapshot_attorney and not wl.snapshot_attorney:
            wl.snapshot_attorney = linked_wf.snapshot_attorney
        if linked_wf.snapshot_handler and not wl.snapshot_handler:
            wl.snapshot_handler = linked_wf.snapshot_handler
        if linked_wf.snapshot_manager and not wl.snapshot_manager:
            wl.snapshot_manager = linked_wf.snapshot_manager
    except SQLAlchemyError as e:
        logger.warning(
            "Failed to backfill WorkLog snapshots from Workflow for docket=%s: %s",
            docket_id,
            e,
        )
        raise
    except Exception as e:
        logger.warning(
            "Failed to backfill WorkLog snapshots from Workflow for docket=%s: %s",
            docket_id,
            e,
        )


def ensure_worklog_for_docket(
    *,
    docket_item: DocketItem,
    actor_id: int | None = None,
) -> WorkLog | None:
    if not docket_item.docket_id:
        return None
    if _is_non_work_status_red_docket(docket_item):
        _delete_all_worklogs_for_docket(docket_item)
        return None

    wl = WorkLog.query.filter_by(docket_id=docket_item.docket_id).first()
    task_name = (docket_item.name_free or docket_item.name_ref or "").strip() or None
    task_category = (docket_item.category or "").strip() or None
    due_date = _effective_due(docket_item)
    owner_staff_party_id = (
        getattr(docket_item, "owner_staff_party_id", None) or ""
    ).strip() or None
    di_snap_atty = (getattr(docket_item, "snapshot_attorney", None) or "").strip() or None
    di_snap_hdl = (getattr(docket_item, "snapshot_handler", None) or "").strip() or None
    di_snap_mgr = (getattr(docket_item, "snapshot_manager", None) or "").strip() or None
    state, _ = done_state(docket_item.done_date)
    is_done = state in ("done", "cancelled", "expired")
    done_action = "completed"
    done_status = "completed"
    if state == "cancelled":
        done_action = "abandoned"
        done_status = "abandoned"
    elif state == "expired":
        done_action = "expired"
        done_status = "abandoned"

    if not wl:
        # Use SAVEPOINT to isolate WorkLog creation failures
        try:
            with db.session.begin_nested():
                matter = Matter.query.filter_by(matter_id=str(docket_item.matter_id)).first()
                wl = WorkLog(
                    docket_id=docket_item.docket_id,
                    matter_id=str(docket_item.matter_id),
                    our_ref=matter.our_ref if matter else None,
                    task_name=task_name,
                    task_category=task_category,
                    due_date=due_date,
                    owner_staff_party_id=owner_staff_party_id,
                    snapshot_attorney=di_snap_atty,
                    snapshot_handler=di_snap_hdl,
                    snapshot_manager=di_snap_mgr,
                    status=done_status if is_done else "pending",
                    action_type=done_action if is_done else "note",
                )
                # Fallback: if DocketItem has no snapshots, try to copy from linked Workflow
                if not any((di_snap_atty, di_snap_hdl, di_snap_mgr)) and not (
                    wl.snapshot_attorney or wl.snapshot_handler or wl.snapshot_manager
                ):
                    _backfill_worklog_snapshots_from_workflow(wl, docket_item)
                if is_done and actor_id:
                    wl.completed_by_id = actor_id
                    wl.completed_at = datetime.utcnow()
                db.session.add(wl)
                db.session.flush()
        except IntegrityError:
            # Concurrent create may win in another transaction.
            wl = WorkLog.query.filter_by(docket_id=docket_item.docket_id).first()
            if not wl:
                logger.error(
                    "Failed to recover WorkLog after integrity conflict for %s",
                    docket_item.docket_id,
                )
                return None
        except SQLAlchemyError as e:
            logger.error("Failed to create WorkLog for %s: %s", docket_item.docket_id, e)
            raise
        except Exception as e:
            logger.error(f"Failed to create WorkLog for {docket_item.docket_id}: {e}")
            return None

    if wl.task_name != task_name:
        wl.task_name = task_name
    if wl.task_category != task_category:
        wl.task_category = task_category
    if wl.due_date != due_date:
        wl.due_date = due_date
    if wl.owner_staff_party_id != owner_staff_party_id:
        wl.owner_staff_party_id = owner_staff_party_id

    # Sync staff snapshots from DocketItem
    if wl.snapshot_attorney != di_snap_atty:
        wl.snapshot_attorney = di_snap_atty
    if wl.snapshot_handler != di_snap_hdl:
        wl.snapshot_handler = di_snap_hdl
    if wl.snapshot_manager != di_snap_mgr:
        wl.snapshot_manager = di_snap_mgr

    # Fallback: if DocketItem has no snapshots (common in legacy data), try to copy from linked Workflow
    if not any((di_snap_atty, di_snap_hdl, di_snap_mgr)) and not (
        wl.snapshot_attorney or wl.snapshot_handler or wl.snapshot_manager
    ):
        _backfill_worklog_snapshots_from_workflow(wl, docket_item)

    if is_done:
        if wl.status != done_status:
            wl.status = done_status
        if wl.action_type != done_action:
            wl.action_type = done_action
        if not wl.completed_at:
            wl.completed_at = datetime.utcnow()
        if actor_id and not wl.completed_by_id:
            wl.completed_by_id = actor_id
    else:
        if wl.status in ("completed", "abandoned"):
            wl.status = "pending"
            wl.action_type = "note"
            wl.completed_at = None
            wl.completed_by_id = None

    db.session.add(wl)
    return wl


def sync_from_docket_item(*, docket_item: DocketItem, actor_id: int | None = None) -> None:
    """Sync DocketItem to Workflow and WorkLog.

    Each sub-operation handles its own SAVEPOINT, so we don't wrap everything
    in one begin_nested() to avoid double nesting issues.
    """
    docket_item = _bind_docket_item(docket_item)
    if docket_item is None:
        return

    if (docket_item.name_ref or "").strip() == "MGMT:FILING" or (
        docket_item.name_free or ""
    ).strip() == "ApplicationDeadline ":
        if not (docket_item.done_date or "").strip():
            docket_item.done_date = f"AUTO_CANCELLED:{date.today().isoformat()}"
            db.session.add(docket_item)

    is_deleted = bool(getattr(docket_item, "is_deleted", False))
    if not is_deleted and _mark_orphaned_workflow_generated_docket_deleted(docket_item):
        try:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=True,
            )
        except Exception:
            logger.exception("Failed to reconcile orphaned workflow-generated docket")
        try:
            _delete_open_worklogs_for_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for orphaned workflow-generated docket")
        if not (docket_item.done_date or "").strip():
            docket_item.done_date = f"AUTO_CANCELLED:{date.today().isoformat()}"
        db.session.add(docket_item)
        return
    elif not is_deleted and _workflow_id_from_docket_id(getattr(docket_item, "docket_id", None)):
        return

    try:
        _backfill_owner_for_known_auto_dockets(docket_item)
    except Exception:
        logger.exception("Failed to backfill docket owner (best-effort)")

    try:
        _maybe_apply_terminal_done_value_to_docket(docket_item)
    except Exception:
        logger.exception(
            "Failed to apply terminal docket guardrail during sync for docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    try:
        duplicate_status_red = _matching_status_red_peer_for_legacy_v2_limit(
            docket_item=docket_item,
            open_only=False,
        )
        if duplicate_status_red is not None:
            source_done = (getattr(duplicate_status_red, "done_date", None) or "").strip()
            done_value = (
                source_done
                if source_done.startswith("AUTO_CANCELLED:")
                else f"AUTO_CANCELLED:{date.today().isoformat()}"
            )
            _auto_cancel_legacy_v2_limit_duplicate(
                docket_item=docket_item,
                done_value=done_value,
                replacement_docket_item=duplicate_status_red,
            )
            return
    except Exception:
        logger.exception(
            "Failed to cleanup legacy V2_LIMIT duplicate for docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    try:
        _cleanup_duplicate_legacy_v2_limit_for_status_red(docket_item=docket_item)
    except Exception:
        logger.exception(
            "Failed to cleanup legacy V2_LIMIT peers for status-red docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    if is_deleted:
        soft_delete_done_value = (getattr(docket_item, "done_date", None) or "").strip() or (
            f"AUTO_CANCELLED:{date.today().isoformat()}"
        )
        try:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=True,
            )
        except Exception:
            logger.exception("Failed to reconcile workflows for soft-deleted docket item")
        try:
            _delete_open_worklogs_for_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for soft-deleted docket item")
        if not (docket_item.done_date or "").strip():
            docket_item.done_date = soft_delete_done_value
            db.session.add(docket_item)
        return

    if _is_non_work_status_red_docket(docket_item):
        try:
            _cleanup_worklogs_for_skipped_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for passive status-red docket item")
        try:
            _cleanup_skipped_docket_workflows(docket_item, delete_auto_generated=True)
        except Exception:
            logger.exception("Failed to delete passive status-red workflows")
        try:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=True,
            )
        except Exception:
            logger.exception("Failed to reconcile passive status-red workflows")
        return

    if _is_renewal_managed_term_expiry_docket(docket_item):
        try:
            _cleanup_worklogs_for_skipped_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for renewal-managed term-expiry docket")
        try:
            _cleanup_skipped_docket_workflows(docket_item, delete_auto_generated=True)
        except Exception:
            logger.exception("Failed to delete renewal-managed term-expiry workflows")
        return

    if not is_visible_by_date(docket_item):
        if _is_supported_open_core_status_red_deadline(docket_item):
            return
        try:
            _reconcile_distributed_workflows(
                docket_item=docket_item,
                target_assignee_ids=set(),
                delete_non_target=False,
            )
        except Exception:
            logger.exception("Failed to reconcile workflows for hidden docket item")
        try:
            _delete_open_worklogs_for_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for hidden docket item")
        return
    try:
        if _enforce_latest_open_uspto_oa_opinion_deadline(docket_item=docket_item):
            return
    except Exception:
        logger.exception(
            "Failed to enforce latest-open USPTO_OA policy for docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    skip_workflow = False
    try:
        skip_workflow = _should_skip_workflow_for_docket(docket_item)
    except Exception:
        logger.exception(
            "Failed to evaluate workflow-skip policy for docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    if skip_workflow:
        try:
            _cleanup_worklogs_for_skipped_docket(docket_item)
        except Exception:
            logger.exception("Failed to cleanup worklogs for skipped docket item")
        try:
            _cleanup_skipped_docket_workflows(
                docket_item,
                delete_auto_generated=_should_delete_skipped_docket_workflows(docket_item),
            )
        except Exception:
            logger.exception("Failed to auto-abandon skipped docket workflows")
        return

    try:
        _cleanup_duplicate_status_red_workflows_for_oa_due(docket_item=docket_item)
    except Exception:
        logger.exception(
            "Failed to cleanup duplicate status-red workflows for OA docket=%s",
            getattr(docket_item, "docket_id", None),
        )

    try:
        ensure_worklog_for_docket(docket_item=docket_item, actor_id=actor_id)
    except SQLAlchemyError as e:
        logger.error("Failed to ensure worklog for %s: %s", docket_item.docket_id, e)
        raise
    except Exception as e:
        logger.error(f"Failed to ensure worklog for {docket_item.docket_id}: {e}")
        # Continue with workflow sync even if worklog fails

    workflows = ensure_workflow_for_docket(docket_item=docket_item, created_by_id=actor_id)

    if workflows:
        from app.services.workflow.sync_requests import enqueue_workflow_sync

        for wf in workflows:
            try:
                if wf.id:
                    enqueue_workflow_sync(workflow_id=wf.id)
            except Exception as e:
                logger.error(f"Failed to enqueue workflow {wf.id} sync: {e}")


def sync_from_workflow(*, workflow: Workflow, actor_id: int | None = None) -> None:
    if not workflow:
        return

    try:
        if workflow.id:
            from app.services.workflow.sync_requests import enqueue_workflow_sync

            enqueue_workflow_sync(workflow_id=workflow.id)
    except Exception as e:
        logger.error(f"Failed to enqueue sync for workflow {workflow.id}: {e}")

    linked_source_docket = _linked_source_docket_item_for_workflow(workflow)
    skip_leg_docket = linked_source_docket is not None

    owner_staff_party_id = _resolve_owner_staff_party_id_for_workflow(workflow)
    docket_category = _resolve_docket_category_for_workflow(
        workflow,
        owner_staff_party_id=owner_staff_party_id,
    )

    date_map = {
        "LEG": workflow.legal_due_date or workflow.due_date,
        "DRA": workflow.draft_due_date,
        "SUB": workflow.submit_due_date,
    }
    effective_due = getattr(workflow, "due_date", None)
    legal_due = getattr(workflow, "legal_due_date", None) or effective_due
    normalized_workflow_name = (
        strip_workflow_deadline_title_suffix(getattr(workflow, "name", None)) or ""
    )
    if (getattr(workflow, "name", None) or "").strip() != normalized_workflow_name:
        workflow.name = normalized_workflow_name
        db.session.add(workflow)
    docket_base_name = normalized_workflow_name

    docket_items = _workflow_generated_docket_items(getattr(workflow, "id", None))
    try:
        case_id = str(workflow.case_id)
        existing_kinds = {
            kind
            for kind in (_workflow_generated_docket_kind(item) for item in docket_items)
            if kind
        }
        for key, due in date_map.items():
            if skip_leg_docket and key == "LEG":
                continue
            if not due or key in existing_kinds:
                continue
            canonical_id = f"WF-{workflow.id}-{key}"
            di = DocketItem(docket_id=canonical_id, matter_id=case_id)
            if hasattr(di, "raw_id"):
                di.raw_id = canonical_id
            docket_items.append(di)
            existing_kinds.add(key)
    except Exception as e:
        logger.error(
            "Failed to prepare workflow-generated docket items for workflow %s: %s",
            getattr(workflow, "id", None),
            e,
        )

    if not docket_items:
        return

    for di in docket_items:
        matched_key = _workflow_generated_docket_kind(di)
        due = date_map.get(matched_key) if matched_key else None
        if not matched_key:
            continue
        if hasattr(di, "raw_id"):
            di.raw_id = f"WF-{workflow.id}-{matched_key}"
        if skip_leg_docket and matched_key == "LEG":
            _soft_delete_workflow_generated_docket(
                di,
                reason="workflow_deadline_superseded_by_linked_docket",
            )
            db.session.add(di)
            continue
        if matched_key != "LEG" and not due:
            _soft_delete_workflow_generated_docket(
                di,
                reason="workflow_legacy_deadline_removed",
            )
            db.session.add(di)
            continue
        if matched_key == "LEG" and not (legal_due or effective_due):
            _soft_delete_workflow_generated_docket(
                di,
                reason="workflow_deadline_removed",
            )
            db.session.add(di)
            continue

        _reactivate_workflow_generated_docket(di)
        current_due = _parse_date(di.due_date)
        due_value = due.isoformat() if due else None
        if current_due != due or (due is None and (di.due_date or "").strip()):
            di.due_date = due_value
        if matched_key == "LEG":
            internal_due_value = (
                effective_due.isoformat()
                if effective_due and legal_due and effective_due != legal_due
                else None
            )
            if (di.extended_due_date or "").strip() != (internal_due_value or ""):
                di.extended_due_date = internal_due_value
        elif (di.extended_due_date or "").strip():
            di.extended_due_date = None
        if matched_key:
            docket_name = workflow_deadline_title(
                docket_base_name,
                matched_key,
                legal_due_date=legal_due,
                effective_due_date=effective_due,
            )
            deadline_label = (
                workflow_deadline_label(
                    matched_key,
                    legal_due_date=legal_due,
                    effective_due_date=effective_due,
                )
                or matched_key
            )
            if (di.name_ref or "").strip() != docket_name:
                di.name_ref = docket_name
            if (di.name_free or "").strip() != docket_name:
                di.name_free = docket_name
            memo = f"{deadline_label} - {workflow.note or ''}".strip(" -")
            if (di.memo or "").strip() != memo:
                di.memo = memo
        if (di.category or "").strip().upper() != docket_category:
            di.category = docket_category
        if di.owner_staff_party_id != owner_staff_party_id:
            di.owner_staff_party_id = owner_staff_party_id

        completed_on = getattr(workflow, "completed_date", None) or date.today()
        if workflow.status == "Completed":
            done_state_now, _ = done_state(di.done_date)
            desired_done_value = completed_on.isoformat()
            if (
                done_state_now in ("pending", "done")
                and (di.done_date or "").strip() != desired_done_value
            ):
                di.done_date = desired_done_value
        elif workflow.status == "Abandoned":
            done_state_now, _ = done_state(di.done_date)
            desired_done_value = f"AUTO_CANCELLED:{completed_on.isoformat()}"
            if done_state_now != "expired" and (di.done_date or "").strip() != desired_done_value:
                di.done_date = desired_done_value
        else:
            if (di.done_date or "").strip():
                di.done_date = None

        if di.snapshot_attorney != workflow.snapshot_attorney:
            di.snapshot_attorney = workflow.snapshot_attorney
        if di.snapshot_handler != workflow.snapshot_handler:
            di.snapshot_handler = workflow.snapshot_handler
        if di.snapshot_manager != workflow.snapshot_manager:
            di.snapshot_manager = workflow.snapshot_manager

        db.session.add(di)
        ensure_worklog_for_docket(docket_item=di, actor_id=actor_id)

def sync_from_annuity_item(annuity_id: str | int) -> None:
    from app.services.workflow.annuity_task_sync import sync_from_annuity_item as _impl

    _impl(annuity_id)


def sync_annuity_workflows_for_matter(matter_id: str) -> None:
    from app.services.workflow.annuity_task_sync import sync_annuity_workflows_for_matter as _impl

    _impl(matter_id)
