from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from flask import current_app, has_request_context, request
from flask_login import current_user
from sqlalchemy import and_, func, inspect, or_

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import EventKeyMap, Matter, MatterEvent, MatterStaffAssignment
from app.models.notification_queue import NotificationQueue
from app.services.productivity.search_service import _case_index_available  # Not used hereNew
from app.services.productivity.search_service import _apply_docket_visibility_filter, _ilike
from app.services.productivity.utils import (
    check_can_access_matter_id,
    get_docket_pk,
    get_docket_title,
    get_today,
    get_user_id,
    has_attr_safe,
)
from app.utils.docket_dates import effective_due_for_work, effective_due_text_expr, parse_date
from app.utils.error_logging import report_swallowed_exception

# Helpers for event availability
# We need to replicate these or import them?
# _matter_event_available is used in get_today_todos
# We can just use inspect again or import from search_service if we made them publicNew
# They are private in search_service.
# I'll implement local availability checkers for reminder service to be safe/self-contained for now,
# or use try-except around queries.


_MATTER_EVENT_AVAILABLE: Optional[bool] = None
_EVENT_KEY_MAP_AVAILABLE: Optional[bool] = None


def _matter_event_available() -> bool:
    global _MATTER_EVENT_AVAILABLE
    if _MATTER_EVENT_AVAILABLE is not None:
        return _MATTER_EVENT_AVAILABLE
    try:
        try:
            eng = db.get_engine(current_app)
        except Exception:
            eng = db.engine
        _MATTER_EVENT_AVAILABLE = inspect(eng).has_table("matter_event")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="reminder_service._matter_event_available",
            log_key="reminder_service._matter_event_available",
            log_window_seconds=300,
        )
        _MATTER_EVENT_AVAILABLE = False
    return _MATTER_EVENT_AVAILABLE


def _event_key_map_available() -> bool:
    global _EVENT_KEY_MAP_AVAILABLE
    if _EVENT_KEY_MAP_AVAILABLE is not None:
        return _EVENT_KEY_MAP_AVAILABLE
    try:
        try:
            eng = db.get_engine(current_app)
        except Exception:
            eng = db.engine
        _EVENT_KEY_MAP_AVAILABLE = inspect(eng).has_table("event_key_map")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="reminder_service._event_key_map_available",
            log_key="reminder_service._event_key_map_available",
            log_window_seconds=300,
        )
        _EVENT_KEY_MAP_AVAILABLE = False
    return _EVENT_KEY_MAP_AVAILABLE


def _effective_due_expr():
    if has_attr_safe(DocketItem, "extended_due_date"):
        return effective_due_text_expr(
            DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
        )
    return DocketItem.due_date


def _effective_due_value(d: Any) -> date | None:
    return effective_due_for_work(
        getattr(d, "due_date", None),
        getattr(d, "extended_due_date", None),
    )


def _is_statutory(d: Any) -> bool:
    """
    StatutoryDeadline 
    """
    for key in (
        "is_statutory",
        "is_statutory_deadline",
        "is_legal_deadline",
        "is_legal",
    ):
        v = getattr(d, key, None)
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return bool(v)

    for key in ("deadline_type", "due_type", "kind", "category"):
        v = (getattr(d, key, None) or "").strip().upper()
        if v in ("LEGAL", "STATUTORY", "LAW", "OFFICIAL"):
            return True

    name = (getattr(d, "name", None) or getattr(d, "title", None) or "").strip()
    if "Statutory" in name or "LEGAL" in name.upper():
        return True
    return False


_OA_ID_FROM_NAME_REF_RE = re.compile(r"^(?:MGMT:)?NOTICE:OA:([^:]+)", re.IGNORECASE)


def _parse_memo_json(value: str | None) -> dict:
    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _extract_office_action_id(d: Any) -> str | None:
    memo = _parse_memo_json(getattr(d, "memo", None))
    if (memo.get("trigger") or "").strip() == "office_action_due":
        oa_id = (memo.get("oa_id") or "").strip()
        if oa_id:
            return oa_id

    name_ref = (getattr(d, "name_ref", None) or "").strip()
    if not name_ref:
        # Legacy migration pattern: some OA-derived docket items are stored with
        # `category=V2_LIMIT` and reuse `docket_id == office_action.oa_id`.
        cat = (getattr(d, "category", None) or "").strip().upper()
        if cat == "V2_LIMIT":
            did = (getattr(d, "docket_id", None) or getattr(d, "id", None) or "").strip()
            return did or None
        return None
    m = _OA_ID_FROM_NAME_REF_RE.match(name_ref)
    if not m:
        return None
    oa_id = (m.group(1) or "").strip()
    return oa_id or None


def _not_done_filter(model):
    """
    Done/Cancel 
    """
    clauses = []
    if has_attr_safe(model, "is_done"):
        clauses.append(or_(model.is_done.is_(False), model.is_done.is_(None)))
    if has_attr_safe(model, "is_cancelled"):
        clauses.append(or_(model.is_cancelled.is_(False), model.is_cancelled.is_(None)))
    if has_attr_safe(model, "status"):
        clauses.append(
            or_(model.status.is_(None), model.status.notin_(["DONE", "CANCELLED", "CLOSED"]))
        )
    if has_attr_safe(model, "done_date"):
        clauses.append(or_(model.done_date.is_(None), model.done_date == ""))

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return and_(*clauses)


def ensure_docket_reminders(*, matter_id: Optional[str] = None, horizon_days: int = 60) -> None:
    """
    D-7/D-3/D-1 Notice  .
    """
    uid = get_user_id()
    today = get_today()
    to_date = today + timedelta(days=max(14, horizon_days))

    q = db.session.query(DocketItem)
    if matter_id and has_attr_safe(DocketItem, "matter_id"):
        q = q.filter(DocketItem.matter_id == matter_id)
    if has_attr_safe(DocketItem, "due_date"):
        effective_due = _effective_due_expr()
        q = q.filter(effective_due.isnot(None), effective_due != "")
        q = q.filter(effective_due <= to_date.isoformat())
    nf = _not_done_filter(DocketItem)
    if nf is not None:
        q = q.filter(nf)

    if has_attr_safe(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))

    # Contact ()
    if has_attr_safe(DocketItem, "assignee_id"):
        q = q.filter(DocketItem.assignee_id == uid)
    elif has_attr_safe(DocketItem, "user_id"):
        q = q.filter(DocketItem.user_id == uid)

    items = q.order_by(_effective_due_expr().asc()).limit(300).all()

    offsets = [7, 3, 1]
    changed = False
    for d in items:
        due = _effective_due_value(d)
        if not due:
            continue
        for off in offsets:
            remind_on = due - timedelta(days=off)
            if remind_on < (today - timedelta(days=1)):
                continue

            statutory = _is_statutory(d)
            prio = 10 if statutory else 0
            did = get_docket_pk(d)
            docket_id = str(did).strip() if did is not None else None

            title = f"Deadline D-{off}: {get_docket_title(d)}"
            msg = f"Due date: {due.isoformat()}"
            dedupe_key = f"docket:{docket_id or 'noid'}:{uid}:D{off}"

            existing = (
                db.session.query(NotificationQueue)
                .filter(NotificationQueue.dedupe_key == dedupe_key)
                .first()
            )
            if existing:
                if (
                    existing.remind_on != remind_on
                    or existing.due_date != due
                    or existing.priority != prio
                ):
                    existing.remind_on = remind_on
                    existing.due_date = due
                    existing.priority = prio
                    existing.title = title
                    existing.message = msg
                    existing.matter_id = getattr(d, "matter_id", None)
                    existing.docket_id = docket_id
                    changed = True
            else:
                row = NotificationQueue(
                    user_id=uid,
                    kind="DOCKET_REMINDER",
                    priority=prio,
                    title=title,
                    message=msg,
                    matter_id=getattr(d, "matter_id", None),
                    docket_id=docket_id,
                    remind_on=remind_on,
                    due_date=due,
                    dedupe_key=dedupe_key,
                )
                db.session.add(row)
                changed = True

    if changed:
        try:
            db.session.commit()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="reminder_service.ensure_docket_reminders.commit",
                log_key="reminder_service.ensure_docket_reminders.commit",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="reminder_service.ensure_docket_reminders.rollback",
                    log_key="reminder_service.ensure_docket_reminders.rollback",
                    log_window_seconds=300,
                )


def get_today_todos(*, matter_id: Optional[str] = None) -> list[dict]:
    """
    /Matter view  'Task'
    """
    uid = get_user_id()
    today = get_today()
    horizon = today + timedelta(days=14)
    overdue_days = current_app.config.get("TASK_TODO_OVERDUE_DAYS", 30)
    try:
        overdue_days = int(overdue_days)
    except (TypeError, ValueError):
        overdue_days = 30
    if overdue_days < 0:
        overdue_days = 0
    from_date = today - timedelta(days=overdue_days)

    side_effects = current_app.config.get("TASK_TODO_SIDE_EFFECTS_ENABLED", False)
    try:
        if isinstance(side_effects, str):
            side_effects = side_effects.strip().lower() in ("1", "true", "yes", "y", "on")
        side_effects = bool(side_effects)
    except Exception:
        side_effects = False
    if side_effects and has_request_context():
        method = (request.method or "").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            allow_get = current_app.config.get("TASK_TODO_SIDE_EFFECTS_ALLOW_GET", False)
            try:
                if isinstance(allow_get, str):
                    allow_get = allow_get.strip().lower() in ("1", "true", "yes", "y", "on")
                allow_get = bool(allow_get)
            except Exception:
                allow_get = False
            if not allow_get:
                side_effects = False

    auto_close_enabled = current_app.config.get("DEADLINE_AUTO_CLOSE_ENABLED", True)
    try:
        if isinstance(auto_close_enabled, str):
            auto_close_enabled = auto_close_enabled.strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
                "on",
            )
        auto_close_enabled = bool(auto_close_enabled)
    except Exception:
        auto_close_enabled = True

    auto_close_ok = bool(side_effects and auto_close_enabled)
    if auto_close_ok and has_request_context():
        # Never allow global mutations from a web request, even if configs are mis-set.
        # Global auto-close should run from background/ops context only.
        if not (matter_id or "").strip():
            auto_close_ok = False
        # Require edit-case permission on the target matter to avoid status bypass.
        elif not check_can_access_matter_id(str(matter_id), action="edit_case"):
            auto_close_ok = False

    if auto_close_ok:
        try:
            from app.services.deadlines.mgmt_deadlines import auto_close_post_due_deadlines

            auto_close_post_due_deadlines(matter_id=matter_id, commit=True)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="reminder_service.get_today_todos.auto_close_post_due_deadlines",
                log_key="reminder_service.get_today_todos.auto_close_post_due_deadlines",
                log_window_seconds=300,
            )

    if side_effects:
        ensure_docket_reminders(matter_id=matter_id)

    oa_unhandled_cache: dict[str, set[str]] = {}

    q = db.session.query(DocketItem)
    joined_matter = False
    if matter_id and has_attr_safe(DocketItem, "matter_id"):
        q = q.filter(DocketItem.matter_id == matter_id)
    if has_attr_safe(DocketItem, "due_date"):
        effective_due = _effective_due_expr()
        q = q.filter(effective_due.isnot(None), effective_due != "")
        q = q.filter(effective_due <= horizon.isoformat())
        q = q.filter(effective_due >= from_date.isoformat())
    nf = _not_done_filter(DocketItem)
    if nf is not None:
        q = q.filter(nf)

    if has_attr_safe(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))

    if has_attr_safe(DocketItem, "assignee_id"):
        q = q.filter(DocketItem.assignee_id == uid)
    elif has_attr_safe(DocketItem, "user_id"):
        q = q.filter(DocketItem.user_id == uid)
    else:
        q = _apply_docket_visibility_filter(q)

    if has_attr_safe(DocketItem, "matter_id") and has_attr_safe(Matter, "matter_id"):
        q = q.join(Matter, DocketItem.matter_id == Matter.matter_id).add_entity(Matter)
        joined_matter = True
        if has_attr_safe(Matter, "is_deleted"):
            q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
        if has_attr_safe(Matter, "status_blue"):
            status_blue = Matter.status_blue
            closed_blue = or_(
                _ilike(status_blue, ""),
                _ilike(status_blue, "Abandoned"),
                _ilike(status_blue, ""),
            )
            q = q.filter(or_(status_blue.is_(None), status_blue == "", ~closed_blue))
        if has_attr_safe(Matter, "inhouse_status"):
            inhouse_status = Matter.inhouse_status
            closed_inhouse = or_(
                _ilike(inhouse_status, ""),
                _ilike(inhouse_status, "Abandoned"),
                _ilike(inhouse_status, ""),
            )
            q = q.filter(or_(inhouse_status.is_(None), inhouse_status == "", ~closed_inhouse))
        if has_attr_safe(Matter, "status_red"):
            status_red = Matter.status_red
            closed_red = or_(
                _ilike(status_red, "Matter closed"),
                _ilike(status_red, "Term expired"),
                _ilike(status_red, "Abandoned"),
            )
            q = q.filter(or_(status_red.is_(None), status_red == "", ~closed_red))
        if _matter_event_available():
            close_std_keys = ("CLOSE_DATE", "ABANDON_WITHDRAW_DATE", "TERM_EXPIRY_DATE")
            close_raw_keys = ("Done/Closed", "Abandoned/Withdrawn", " Period ")
            event_filters = [
                MatterEvent.event_key.in_(close_std_keys),
                MatterEvent.event_key.in_(close_raw_keys),
            ]
            event_q = db.session.query(MatterEvent.matter_id)
            if _event_key_map_available():
                event_q = event_q.outerjoin(
                    EventKeyMap, EventKeyMap.raw_event_key == MatterEvent.event_key
                )
                event_filters.append(EventKeyMap.std_event_key.in_(close_std_keys))
            event_match = or_(*event_filters)
            closed_exists = event_q.filter(
                MatterEvent.matter_id == Matter.matter_id,
                MatterEvent.event_at.isnot(None),
                func.trim(MatterEvent.event_at) != "",
                event_match,
            ).exists()
            q = q.filter(~closed_exists)

        staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
        if staff_pid:
            msa_exists = (
                db.session.query(MatterStaffAssignment.matter_id)
                .filter(
                    MatterStaffAssignment.matter_id == Matter.matter_id,
                    MatterStaffAssignment.staff_party_id == staff_pid,
                )
                .exists()
            )
            if has_attr_safe(DocketItem, "owner_staff_party_id"):
                owner_clause = DocketItem.owner_staff_party_id == staff_pid
                q = q.filter(or_(owner_clause, msa_exists))
            else:
                q = q.filter(msa_exists)
        else:
            from sqlalchemy import false

            q = q.filter(false())

    items = q.order_by(_effective_due_expr().asc()).limit(80).all()

    out: list[dict] = []
    for row in items:
        if joined_matter:
            d, m = row
        else:
            d = row
            m = None
        due = _effective_due_value(d)
        if not due:
            continue
        if due < from_date or due > horizon:
            continue
        dday = (due - today).days
        statutory = _is_statutory(d)

        badge = "danger" if statutory else ("warning" if dday <= 3 else "secondary")
        if dday < 0:
            badge = "danger"

        mid = getattr(d, "matter_id", None)
        if mid:
            oa_id = _extract_office_action_id(d)
            if oa_id:
                try:
                    from app.services.matter.matter_auto_status import (
                        get_unhandled_open_office_action_ids,
                    )
                except Exception:
                    get_unhandled_open_office_action_ids = None  # type: ignore[assignment]

                if get_unhandled_open_office_action_ids is not None:
                    mid_str = str(mid)
                    unhandled = oa_unhandled_cache.get(mid_str)
                    if unhandled is None:
                        unhandled = get_unhandled_open_office_action_ids(mid_str)
                        oa_unhandled_cache[mid_str] = unhandled
                    if oa_id not in unhandled:
                        continue

        url = f"/case/{mid}" if mid else None
        did = get_docket_pk(d)
        name = get_docket_title(d)
        case_ref = (getattr(m, "our_ref", None) or "").strip() if m else ""

        out.append(
            {
                "type": "docket",
                "id": did or None,
                "matter_id": mid,
                "title": name,
                "case_ref": case_ref,
                "due_date": due.isoformat(),
                "dday": dday,
                "statutory": statutory,
                "badge": badge,
                "url": url,
            }
        )
    return out
