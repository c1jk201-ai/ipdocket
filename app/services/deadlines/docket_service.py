import json
import uuid
from datetime import date, datetime

from sqlalchemy import or_

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.ip_records import DocketItem
from app.models.user import User
from app.services.docket_manual_state import memo_has_manual_abandon_lock
from app.services.workflow.sync_requests import enqueue_docket_sync_for_item
from app.utils.error_logging import report_swallowed_exception


def _date_token(v: object) -> str:
    try:
        return str(v or "").strip().split("T")[0].strip()
    except Exception:
        return ""


def _effective_due_token(item: DocketItem) -> str:
    return _date_token(
        (getattr(item, "extended_due_date", None) or getattr(item, "due_date", None))
    )


def _normalize_deadline_type(value: str | None, *, default: str = "LEGAL") -> str:
    raw = str(value or "").strip().upper()
    if raw in {"INTERNAL", "INNER", "INHOUSE", "IN", "I", "Internal", "Internal deadline", "Internal"}:
        return "INTERNAL"
    if raw in {"LEGAL", "LAW", "STATUTORY", "L", "Statutory", "Statutory deadline"}:
        return "LEGAL"
    return "INTERNAL" if str(default or "").strip().upper() == "INTERNAL" else "LEGAL"


def _parse_memo_json(value: str | None) -> dict:
    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _active_docket_query(query):
    if hasattr(DocketItem, "is_deleted"):
        query = query.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    return query


def upsert_filing_docket(
    matter_id: str,
    deadline_date: str,
    *,
    deadline_type: str | None = None,
    commit: bool = False,
) -> None:
    """
    Upserts a single 'Filing' docket item for the given matter.

    Filing is represented as one consolidated HYBRID workflow through task sync.
    Do not create a separate "Filing (Process)" docket/workflow; the consolidated
    workflow already carries handler/attorney/manager assignments.
    MGMT filing dockets are explicitly auto-cancelled by policy.
    """
    mid = (matter_id or "").strip()
    due = (deadline_date or "").strip()
    if not mid or not due:
        return
    due_type = _normalize_deadline_type(deadline_type, default="LEGAL")

    idx = CaseFlatIndex.query.get(mid)
    att_id = idx.attorney_id if idx else None
    hdl_id = idx.handler_id if idx else None
    mgr_id = idx.manager_id if idx else None

    # 1. Attorney Task (Main)
    # Use standard ref 'Filing' for backward compatibility and main tracking
    main_docket = _upsert_single_docket(
        mid,
        "FILING",
        "Filing",
        "Filing Deadline",
        due,
        att_id,
        due_type=due_type,
    )

    db.session.flush()
    _sync_filing_docket_now(main_docket)
    # Policy: we do not keep helper filing tasks active for filing.
    _retire_filing_handler_docket(mid)
    _cancel_mgmt_filing_docket(mid)

    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _cancel_mgmt_filing_docket(matter_id: str) -> None:
    if not matter_id:
        return
    docket = _active_docket_query(
        DocketItem.query.filter_by(matter_id=matter_id, name_ref="MGMT:FILING")
    ).first()
    if not docket:
        return
    if (docket.done_date or "").strip():
        return
    docket.done_date = f"AUTO_CANCELLED:{date.today().isoformat()}"
    db.session.add(docket)
    try:
        enqueue_docket_sync_for_item(docket_item=docket)
    except Exception as exc:
        # Best-effort: sync enqueue should not block docket updates.
        report_swallowed_exception(
            exc,
            context="docket_service._cancel_mgmt_filing_docket.enqueue_sync",
            log_key="docket_service._cancel_mgmt_filing_docket.enqueue_sync",
            log_window_seconds=300,
        )


def _retire_filing_handler_docket(matter_id: str) -> int:
    """Hide legacy "Filing (Process)" helper rows so filing stays a single workflow."""
    if not matter_id:
        return 0
    rows = DocketItem.query.filter_by(matter_id=matter_id, name_ref="Filing (Process)").all()
    count = 0
    for docket in rows:
        was_deleted = bool(getattr(docket, "is_deleted", False))
        if not was_deleted:
            docket.is_deleted = True
            docket.deleted_at = datetime.utcnow()
            docket.delete_reason = "retire_filing_handler_helper"
            db.session.add(docket)
            count += 1
        _cleanup_retired_filing_handler_outputs(docket)
        if not was_deleted:
            try:
                enqueue_docket_sync_for_item(docket_item=docket)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="docket_service._retire_filing_handler_docket.enqueue_sync",
                    log_key="docket_service._retire_filing_handler_docket.enqueue_sync",
                    log_window_seconds=300,
                )
    return count


def _cleanup_retired_filing_handler_outputs(docket: DocketItem) -> None:
    """Remove workflow/worklog rows already created from the retired helper docket."""
    docket_id = (getattr(docket, "docket_id", None) or "").strip()
    if not docket_id:
        return
    try:
        from app.models.workflow import Workflow
        from app.models.worklog import WorkLog
        from app.services.workflow.task_sync import _delete_workflow_for_distribution_cleanup

        WorkLog.query.filter_by(docket_id=docket_id).delete(synchronize_session=False)
        prefix = f"DOCKET:{docket_id}"
        workflows = (
            Workflow.query.filter(Workflow.business_code.like(f"{prefix}%"))
            .order_by(Workflow.id.asc())
            .all()
        )
        for workflow in workflows:
            if getattr(workflow, "id", None):
                _delete_workflow_for_distribution_cleanup(
                    workflow_id=int(workflow.id),
                )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="docket_service._cleanup_retired_filing_handler_outputs",
            log_key="docket_service._cleanup_retired_filing_handler_outputs",
            log_window_seconds=300,
        )


def _sync_filing_docket_now(docket: DocketItem | None) -> None:
    """Filing deadline workflow must be visible immediately; durable queue is fallback only."""
    if docket is None:
        return
    try:
        from app.services.workflow.task_sync import sync_from_docket_item

        sync_from_docket_item(docket_item=docket, actor_id=None)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="docket_service._sync_filing_docket_now",
            log_key="docket_service._sync_filing_docket_now",
            log_window_seconds=300,
        )


def upsert_exam_request_docket(matter_id: str, deadline_date: str, *, commit: bool = False) -> None:
    """
    Upserts 'Exam Request' docket items (Att + Handler + Mgmt).
    """
    mid = (matter_id or "").strip()
    due = (deadline_date or "").strip()
    if not mid or not due:
        return

    idx = CaseFlatIndex.query.get(mid)
    att_id = idx.attorney_id if idx else None
    hdl_id = idx.handler_id if idx else None
    mgr_id = idx.manager_id if idx else None

    # 1. Attorney
    _upsert_single_docket(mid, "EXAM", "Examination request", "Examination requestDeadline", due, att_id)

    # 2. Handler
    if hdl_id:
        _upsert_single_docket(mid, "EXAM", "Examination request (Process)", "Examination requestDeadline (Process)", due, hdl_id)

    # 3. Mgmt
    _upsert_single_docket(mid, "MGMT", "MGMT:EXAM_REQUEST", "ExaminationExpense management", due, mgr_id)

    if commit:
        db.session.commit()
    else:
        db.session.flush()


def upsert_registration_docket(matter_id: str, deadline_date: str, *, commit: bool = False) -> None:
    """
    Upserts 'Registration' docket items (Att + Handler + Mgmt).
    """
    mid = (matter_id or "").strip()
    due = (deadline_date or "").strip()
    if not mid or not due:
        return

    idx = CaseFlatIndex.query.get(mid)
    att_id = idx.attorney_id if idx else None
    hdl_id = idx.handler_id if idx else None
    mgr_id = idx.manager_id if idx else None

    # 1. Attorney
    _upsert_single_docket(mid, "REG", "Registration", "RegistrationDue date", due, att_id)

    # 2. Handler
    if hdl_id:
        _upsert_single_docket(mid, "REG", "Registration (Process)", "RegistrationDue date (Process)", due, hdl_id)

    # 3. Mgmt
    _upsert_single_docket(mid, "MGMT", "MGMT:REGISTRATION", "RegistrationDeadline ", due, mgr_id)

    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _resolve_owner_ids(owner_user_id):
    raw = str(owner_user_id).strip() if owner_user_id is not None else ""
    if not raw:
        return None, None
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None, None
    user = User.query.get(user_id)
    if user and user.staff_party_id:
        staff_party_id = (str(user.staff_party_id) or "").strip()
        return user_id, staff_party_id or None
    return user_id, None


def _upsert_single_docket(mid, cat, ref, title, due, owner_user_id, due_type: str = "LEGAL"):
    # Determine category based on owner user id -> staff_party_id
    from app.utils.task_classification import determine_category_by_staff_role

    assignee_id, owner_staff_party_id = _resolve_owner_ids(owner_user_id)
    # If explicitly management-task by ref, never downgrade to WORK.
    if ref and str(ref).upper().startswith("MGMT:"):
        save_category = "MGMT"
    else:
        save_category = determine_category_by_staff_role(
            mid, assignee_id=assignee_id, staff_party_id=owner_staff_party_id
        )

    # Prefer an open row with the same due date, then latest open row, then latest done row.
    candidates = (
        _active_docket_query(DocketItem.query.filter_by(matter_id=mid, name_ref=ref))
        .order_by(DocketItem.docket_id.desc())
        .all()
    )
    open_candidates = [row for row in candidates if not (row.done_date or "").strip()]
    docket = None
    if open_candidates:
        target_due_token = _date_token(due)
        docket = next(
            (row for row in open_candidates if _effective_due_token(row) == target_due_token),
            open_candidates[0],
        )
    elif candidates:
        docket = candidates[0]

    normalized_due_type = _normalize_deadline_type(due_type, default="LEGAL")

    if docket:
        existing_memo = _parse_memo_json(getattr(docket, "memo", None))
        if (docket.done_date or "").strip() and memo_has_manual_abandon_lock(existing_memo):
            return docket
        if (docket.done_date or "").strip():
            docket.done_date = None
        if normalized_due_type == "INTERNAL":
            docket.due_date = None
            docket.extended_due_date = due
        else:
            docket.due_date = due
            docket.extended_due_date = None
        docket.owner_staff_party_id = owner_staff_party_id

        # Enforce role-based category
        docket.category = save_category

        # Force name update if changed (e.g. migration)
        if title and docket.name_free != title:
            docket.name_free = title
    else:
        # User implies robust "Assignment". If owner is missing, creating an unassigned task is usually better than nothing.
        docket = DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=mid,
            category=save_category,
            name_ref=ref,
            name_free=title,
            due_date=due if normalized_due_type == "LEGAL" else None,
            extended_due_date=due if normalized_due_type == "INTERNAL" else None,
            owner_staff_party_id=owner_staff_party_id,
        )
        db.session.add(docket)

    try:
        enqueue_docket_sync_for_item(docket_item=docket)
    except Exception as exc:
        # Best-effort: sync enqueue should not block docket updates.
        report_swallowed_exception(
            exc,
            context="docket_service._upsert_single_docket.enqueue_sync",
            log_key="docket_service._upsert_single_docket.enqueue_sync",
            log_window_seconds=300,
        )

    _cancel_duplicate_dockets(mid=mid, name_ref=ref, keep_docket_id=docket.docket_id)
    return docket


def _cancel_duplicate_dockets(*, mid: str, name_ref: str, keep_docket_id: str) -> int:
    if not mid or not name_ref or not keep_docket_id:
        return 0
    duplicates = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == mid,
            DocketItem.name_ref == name_ref,
            DocketItem.docket_id != keep_docket_id,
        )
    ).all()
    if not duplicates:
        return 0

    today = date.today().isoformat()
    count = 0
    for item in duplicates:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo_json(getattr(item, "memo", None))
        if memo_data.get("locked"):
            continue
        item.done_date = f"AUTO_CANCELLED:{today}"
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            # Best-effort: sync enqueue should not block duplicate cancellation.
            report_swallowed_exception(
                exc,
                context="docket_service._cancel_duplicate_dockets.enqueue_sync",
                log_key="docket_service._cancel_duplicate_dockets.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1
    return count


def complete_filing_docket(matter_id: str, done_date: str, *, commit: bool = False) -> None:
    """
    Marks 'Filing' docket items (All variants) as completed.
    """
    _complete_docket_variants(
        matter_id,
        done_date,
        ["Filing", "Filing (Process)", "MGMT:FILING", "MGMT:STATUS_RED:FilingDeadline"],
        commit=commit,
    )


def complete_exam_request_docket(matter_id: str, done_date: str, *, commit: bool = False) -> None:
    """
    Marks 'Exam Request' docket items (All variants) as completed.
    """
    _complete_docket_variants(
        matter_id,
        done_date,
        [
            "Examination request",
            "Examination request (Process)",
            "MGMT:EXAM_REQUEST",
            "MGMT:STATUS_RED:Examination requestDeadline",
            # Foreign email extraction may emit this stable English key directly.
            "exam_request",
        ],
        commit=commit,
    )


def complete_registration_docket(matter_id: str, done_date: str, *, commit: bool = False) -> None:
    """
    Marks 'Registration' docket items (All variants) as completed.
    """
    _complete_docket_variants(
        matter_id,
        done_date,
        ["Registration", "Registration (Process)", "MGMT:REGISTRATION", "MGMT:STATUS_RED:RegistrationDeadline"],
        commit=commit,
    )


def complete_office_action_docket(
    matter_id: str, oa_id: str, done_date: str, *, commit: bool = False
) -> None:
    """
    Marks Office Action docket items (Att + Handler + Mgmt) as completed.
    """
    if not oa_id:
        return
    # Variants:
    # 1. USPTO:{oa_id} (notice-backed legacy/system row)
    # 2. NOTICE:OA:{oa_id}
    # 3. NOTICE:OA:{oa_id}:HDL
    # 4. MGMT:NOTICE:OA:{oa_id}
    # 5. MGMT:OFFICE_ACTION_DUE:{oa_id} (Legacy/Fallback)
    # 6. OA:{oa_id} (upload-automation linked)

    variants = [
        f"USPTO:{oa_id}",
        f"OA:{oa_id}",
        f"NOTICE:OA:{oa_id}",
        f"NOTICE:OA:{oa_id}:HDL",
        f"MGMT:NOTICE:OA:{oa_id}",
        f"MGMT:NOTICE_SEND_3D:{oa_id}",
        f"MGMT:OFFICE_ACTION_DUE:{oa_id}",
    ]
    like_patterns = [f"%:{oa_id}", f"%:{oa_id}:%"]
    _complete_docket_variants(
        matter_id, done_date, variants, like_patterns=like_patterns, commit=False
    )
    _complete_uspto_oa_by_office_action(matter_id, oa_id, done_date)
    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _complete_docket_variants(
    matter_id, done_date, refs, *, like_patterns=None, commit: bool = False
):
    mid = (matter_id or "").strip()
    done = (done_date or "").strip()
    if not mid or not done:
        return

    conditions = []
    if refs:
        conditions.append(DocketItem.name_ref.in_(refs))
    if like_patterns:
        for pat in like_patterns:
            conditions.append(DocketItem.name_ref.like(pat))

    if not conditions:
        return

    # Batch update for efficiency? Or iterate to trigger sync?
    # Must iterate to trigger `sync_from_docket_item` logic
    dockets = _active_docket_query(
        DocketItem.query.filter(DocketItem.matter_id == mid, or_(*conditions))
    ).all()

    for d in dockets:
        if not (d.done_date or "").strip():
            d.done_date = done
            db.session.add(d)
            try:
                enqueue_docket_sync_for_item(docket_item=d)
            except Exception as exc:
                # Best-effort: sync enqueue should not block completion updates.
                report_swallowed_exception(
                    exc,
                    context="docket_service._complete_docket_variants.enqueue_sync",
                    log_key="docket_service._complete_docket_variants.enqueue_sync",
                    log_window_seconds=300,
                )

    if commit:
        db.session.commit()
    else:
        db.session.flush()


def _complete_uspto_oa_by_office_action(matter_id: str, oa_id: str, done_date: str) -> None:
    """Close legacy USPTO_OA rows linked by memo/dispatch when OA is completed."""
    mid = (matter_id or "").strip()
    oid = (oa_id or "").strip()
    done = (done_date or "").strip()
    if not mid or not oid or not done:
        return

    try:
        from app.models.communication import OfficeAction

        oa = OfficeAction.query.filter_by(oa_id=oid, matter_id=mid).first()
    except Exception:
        oa = None

    dispatch_token = ""
    if oa is not None:
        dispatch_token = _date_token(getattr(oa, "notified_date", None)) or _date_token(
            getattr(oa, "received_date", None)
        )
    dispatch_prefix = f"USPTO_OA:OFFICE_ACTION:{dispatch_token}:".upper() if dispatch_token else ""

    rows = _active_docket_query(DocketItem.query.filter(DocketItem.matter_id == mid)).all()
    for di in rows:
        if (di.done_date or "").strip():
            continue

        matched = False
        memo = _parse_memo_json(getattr(di, "memo", None))
        if (memo.get("trigger") or "").strip() == "office_action_due":
            if (memo.get("oa_id") or "").strip() == oid:
                matched = True

        if not matched and dispatch_prefix:
            category = (getattr(di, "category", None) or "").strip().upper()
            name_ref = (getattr(di, "name_ref", None) or "").strip()
            if category == "USPTO_OA" and name_ref.upper().startswith(dispatch_prefix):
                matched = True

        if not matched:
            continue

        di.done_date = done
        db.session.add(di)
        try:
            enqueue_docket_sync_for_item(docket_item=di)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="docket_service._complete_uspto_oa_by_office_action.enqueue_sync",
                log_key="docket_service._complete_uspto_oa_by_office_action.enqueue_sync",
                log_window_seconds=300,
            )
