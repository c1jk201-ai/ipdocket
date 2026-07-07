from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from flask import current_app
from sqlalchemy import func, or_

from app.extensions import db
from app.models.party import Party
from app.models.ip_records import AnnuityItem, DocketItem, Matter
from app.models.user import User
from app.models.workflow import Workflow
from app.utils.docket_dates import parse_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import (
    can_access_matter,
    can_manage_case_globally,
    policy_accessible_matter_ids_select,
)
from app.utils.timezone import today_local
from app.utils.workflow_roles import workflow_user_filter


@dataclass
class CapacityItem:
    item_type: str
    id: str
    matter_id: str | None
    title: str
    due_date: date
    person_key: str
    person_id: str | int | None
    person_name: str
    estimated_hours: float
    priority: str | None = None
    url: str | None = None


def _date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _workflow_effort(wf: Workflow) -> float:
    if wf.work_hours is not None:
        return max(0.25, _safe_float(wf.work_hours, 2.0))
    priority = str(wf.priority or "").strip().lower()
    if priority in {"urgent", ""}:
        return 4.0
    if priority in {"important", "high", "In Progress"}:
        return 3.0
    return 2.0


def _generic_effort(priority: str | None = None) -> float:
    value = str(priority or "").strip().lower()
    if value in {"urgent", ""}:
        return 2.0
    return 1.0


def _user_label(user: User | None, fallback: str | int | None = None) -> str:
    if not user:
        return str(fallback or "")
    return (
        str(user.display_name or "").strip()
        or str(user.username or "").strip()
        or str(user.email or "").strip()
        or str(fallback or user.id)
    )


def _staff_maps() -> tuple[dict[str, str], dict[str, User]]:
    users = User.query.filter(User.is_active.is_(True)).all()
    staff_to_user = {
        str(user.staff_party_id).strip(): user
        for user in users
        if str(user.staff_party_id or "").strip()
    }
    names: dict[str, str] = {}
    for user in users:
        if user.id is not None:
            names[f"user:{user.id}"] = _user_label(user)
        staff_pid = str(user.staff_party_id or "").strip()
        if staff_pid:
            names[f"staff:{staff_pid}"] = _user_label(user, staff_pid)

    party_rows = Party.query.limit(2000).all()
    for party in party_rows:
        pid = str(party.party_id or "").strip()
        if pid:
            names.setdefault(f"staff:{pid}", str(party.name_display or "").strip() or pid)
    return names, staff_to_user


def _effective_docket_due(item: DocketItem) -> date | None:
    return parse_date(item.extended_due_date) or parse_date(item.due_date)


def _effective_annuity_due(item: AnnuityItem) -> date | None:
    return (
        parse_date(item.internal_due_date)
        or parse_date(item.due_date)
        or parse_date(item.extended_due_date)
    )


def _accessible_matter_filter(query, user: Any, model_matter_col):
    if can_manage_case_globally(user):
        return query
    return query.filter(model_matter_col.in_(policy_accessible_matter_ids_select(user)))


def _workflow_items(user: Any, today: date, end: date, names: dict[str, str]) -> list[CapacityItem]:
    q = Workflow.query.filter(Workflow.due_date.isnot(None), Workflow.due_date <= end)
    q = q.filter(Workflow.status.notin_(list(Workflow.TERMINAL_STATUSES)))
    if not can_manage_case_globally(user):
        q = q.filter(workflow_user_filter(getattr(user, "id", None)))
    rows = q.order_by(Workflow.due_date.asc(), Workflow.id.asc()).limit(3000).all()
    out: list[CapacityItem] = []
    for wf in rows:
        if not wf.due_date:
            continue
        mid = str(wf.case_id or "").strip() or None
        if mid and not can_access_matter(user, mid, action="view"):
            continue
        assignee_id = wf.assignee_id or wf.attorney_assignee_id or wf.inspector_id
        person_key = f"user:{assignee_id}" if assignee_id else "unassigned"
        name = names.get(person_key) or _user_label(
            wf.assignee or wf.attorney_assignee or wf.inspector,
            assignee_id,
        )
        out.append(
            CapacityItem(
                item_type="workflow",
                id=str(wf.id),
                matter_id=mid,
                title=wf.name or "Task",
                due_date=wf.due_date,
                person_key=person_key,
                person_id=assignee_id,
                person_name=name,
                estimated_hours=_workflow_effort(wf),
                priority=wf.priority,
                url=f"/case/{mid}#sec-workflow" if mid else f"/workflow/{wf.id}",
            )
        )
    return out


def _docket_items(user: Any, today: date, end: date, names: dict[str, str]) -> list[CapacityItem]:
    q = DocketItem.query.filter(
        or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
    )
    q = q.filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
    q = _accessible_matter_filter(q, user, DocketItem.matter_id)
    rows = q.limit(5000).all()
    out: list[CapacityItem] = []
    for item in rows:
        due = _effective_docket_due(item)
        if not due or due > end:
            continue
        owner = str(item.owner_staff_party_id or "").strip()
        person_key = f"staff:{owner}" if owner else "unassigned"
        out.append(
            CapacityItem(
                item_type="docket",
                id=str(item.docket_id),
                matter_id=str(item.matter_id or "").strip() or None,
                title=item.name_free or item.name_ref or "Deadline",
                due_date=due,
                person_key=person_key,
                person_id=owner or None,
                person_name=names.get(person_key) or owner or "",
                estimated_hours=_generic_effort(),
                url=f"/case/{item.matter_id}#sec-due" if item.matter_id else None,
            )
        )
    return out


def _annuity_items(user: Any, today: date, end: date, names: dict[str, str]) -> list[CapacityItem]:
    q = AnnuityItem.query.filter(
        or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))
    )
    q = q.filter(AnnuityItem.annuity_status == AnnuityItem.STATUS_PENDING)
    q = _accessible_matter_filter(q, user, AnnuityItem.matter_id)
    rows = q.limit(5000).all()
    out: list[CapacityItem] = []
    for item in rows:
        due = _effective_annuity_due(item)
        if not due or due > end:
            continue
        owner = str(item.owner_staff_party_id or "").strip()
        person_key = f"staff:{owner}" if owner else "unassigned"
        out.append(
            CapacityItem(
                item_type="annuity",
                id=str(item.annuity_id),
                matter_id=str(item.matter_id or "").strip() or None,
                title=f"{item.cycle_no} Annuity Fee",
                due_date=due,
                person_key=person_key,
                person_id=owner or None,
                person_name=names.get(person_key) or owner or "",
                estimated_hours=_generic_effort(),
                url=f"/renewal/Newmatter_id={item.matter_id}" if item.matter_id else "/renewal/",
            )
        )
    return out


def _absence_days_by_key() -> dict[str, float]:
    raw = current_app.config.get("CAPACITY_PLANNER_ABSENCE_DAYS") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        key_str = str(key or "").strip()
        if not key_str:
            continue
        out[key_str] = max(0.0, _safe_float(value, 0.0))
    return out


def _capacity_hours(person_key: str, days: int) -> tuple[float, float]:
    weekly_hours = _safe_float(current_app.config.get("CAPACITY_PLANNER_WEEKLY_HOURS"), 30.0)
    daily_hours = _safe_float(current_app.config.get("CAPACITY_PLANNER_DAILY_HOURS"), 6.0)
    absence_days = _absence_days_by_key().get(person_key, 0.0)
    hours = max(0.0, (weekly_hours * (float(days) / 7.0)) - (daily_hours * absence_days))
    return round(hours, 2), absence_days


def _load_level(utilization: float, overdue: int) -> str:
    if overdue > 0 or utilization >= 1.0:
        return "overloaded"
    if utilization >= 0.85:
        return "tight"
    if utilization <= 0.45:
        return "available"
    return "normal"


def _serialize_item(item: CapacityItem, today: date) -> dict[str, Any]:
    return {
        "type": item.item_type,
        "id": item.id,
        "matter_id": item.matter_id,
        "title": item.title,
        "due_date": item.due_date.isoformat(),
        "days_until": (item.due_date - today).days,
        "estimated_hours": item.estimated_hours,
        "priority": item.priority,
        "url": item.url,
    }


def _window_summary(items: list[CapacityItem], *, today: date, days: int) -> dict[str, Any]:
    end = today + timedelta(days=days)
    grouped: dict[str, list[CapacityItem]] = defaultdict(list)
    for item in items:
        if item.due_date <= end:
            grouped[item.person_key].append(item)

    people = []
    for person_key, person_items in grouped.items():
        hours = round(sum(item.estimated_hours for item in person_items), 2)
        capacity_hours, absence_days = _capacity_hours(person_key, days)
        utilization = round(hours / capacity_hours, 3) if capacity_hours > 0 else 1.0
        overdue = sum(1 for item in person_items if item.due_date < today)
        urgent = sum(
            1 for item in person_items if today <= item.due_date <= today + timedelta(days=3)
        )
        by_type: dict[str, int] = defaultdict(int)
        for item in person_items:
            by_type[item.item_type] += 1
        people.append(
            {
                "person_key": person_key,
                "person_id": person_items[0].person_id,
                "name": person_items[0].person_name,
                "item_count": len(person_items),
                "estimated_hours": hours,
                "capacity_hours": capacity_hours,
                "absence_days": absence_days,
                "utilization": utilization,
                "load_level": _load_level(utilization, overdue),
                "overdue_count": overdue,
                "urgent_count": urgent,
                "by_type": dict(sorted(by_type.items())),
                "items": [
                    _serialize_item(item, today)
                    for item in sorted(
                        person_items, key=lambda obj: (obj.due_date, obj.item_type, obj.id)
                    )[:20]
                ],
            }
        )
    people.sort(
        key=lambda row: (
            row["load_level"] != "overloaded",
            -row["utilization"],
            -row["item_count"],
            row["name"],
        )
    )
    return {
        "days": days,
        "start_date": today.isoformat(),
        "end_date": end.isoformat(),
        "total_items": sum(row["item_count"] for row in people),
        "total_estimated_hours": round(sum(row["estimated_hours"] for row in people), 2),
        "people": people,
    }


def _suggestions(window: dict[str, Any]) -> list[dict[str, Any]]:
    people = list(window.get("people") or [])
    overloaded = [p for p in people if p.get("load_level") == "overloaded" and p.get("item_count")]
    available = [
        p
        for p in people
        if p.get("load_level") in {"available", "normal"} and p.get("capacity_hours", 0) > 0
    ]
    available.sort(key=lambda p: (p.get("utilization", 1), p.get("item_count", 0)))
    out: list[dict[str, Any]] = []
    for src in overloaded[:5]:
        if not available:
            break
        dst = available[0]
        movable = [
            item
            for item in src.get("items") or []
            if int(item.get("days_until", 0)) >= 0 and item.get("type") in {"workflow", "docket"}
        ]
        if not movable:
            continue
        item = movable[-1]
        out.append(
            {
                "window_days": window.get("days"),
                "from": src.get("name"),
                "to": dst.get("name"),
                "item": item,
                "reason": "Estimated  Overdue Item    ",
            }
        )
    return out


def build_capacity_plan(
    *,
    user: Any,
    windows: tuple[int, ...] = (14, 28, 56),
) -> dict[str, Any]:
    today = today_local()
    max_days = max(windows)
    end = today + timedelta(days=max_days)
    names, _staff_to_user = _staff_maps()

    items: list[CapacityItem] = []
    try:
        items.extend(_workflow_items(user, today, end, names))
        items.extend(_docket_items(user, today, end, names))
        items.extend(_annuity_items(user, today, end, names))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="capacity_planner.build_capacity_plan",
            log_key="capacity_planner.build",
            log_window_seconds=300,
        )
        raise

    matter_ids = {item.matter_id for item in items if item.matter_id}
    matter_refs = (
        {
            matter.matter_id: matter.our_ref
            for matter in Matter.query.filter(Matter.matter_id.in_(matter_ids)).limit(5000).all()
        }
        if matter_ids
        else {}
    )
    for item in items:
        if item.matter_id and item.matter_id in matter_refs:
            item.title = f"{matter_refs[item.matter_id]} / {item.title}"

    window_rows = [_window_summary(items, today=today, days=days) for days in windows]
    bottlenecks = []
    for window in window_rows:
        for person in window["people"]:
            if person["load_level"] in {"overloaded", "tight"}:
                bottlenecks.append(
                    {
                        "window_days": window["days"],
                        "name": person["name"],
                        "person_key": person["person_key"],
                        "load_level": person["load_level"],
                        "utilization": person["utilization"],
                        "item_count": person["item_count"],
                        "overdue_count": person["overdue_count"],
                    }
                )
    bottlenecks.sort(key=lambda row: (row["load_level"] != "overloaded", -row["utilization"]))

    suggestions: list[dict[str, Any]] = []
    for window in window_rows:
        suggestions.extend(_suggestions(window))

    return {
        "today": today.isoformat(),
        "generated_at": datetime.utcnow().isoformat(),
        "windows": window_rows,
        "bottlenecks": bottlenecks[:20],
        "reassignment_suggestions": suggestions[:10],
    }
