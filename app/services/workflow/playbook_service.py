from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_

from app.extensions import db
from app.models.ip_records import Matter
from app.models.workflow import Workflow
from app.models.workflow_checklist import WorkflowChecklistItem
from app.models.workflow_playbook import WorkflowPlaybookTemplate


@dataclass(frozen=True)
class PlaybookApplyResult:
    checklist_created: int = 0
    fields_updated: int = 0


def parse_checklist_text(value: str | None) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for raw in str(value or "").replace("\r", "\n").split("\n"):
        title = raw.strip().lstrip("-*").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(title)
    return items


def parse_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def schedule_from_form(form) -> dict[str, int]:
    fields = {
        "request_offset_days": "request_offset_days",
        "legal_due_offset_days": "legal_due_offset_days",
        "internal_due_offset_days": "internal_due_offset_days",
        "draft_due_offset_days": "draft_due_offset_days",
        "submit_due_offset_days": "submit_due_offset_days",
    }
    out: dict[str, int] = {}
    for target, form_key in fields.items():
        parsed = parse_int_or_none(form.get(form_key))
        if parsed is not None:
            out[target] = parsed
    return out


def template_to_checklist_text(template: WorkflowPlaybookTemplate | None) -> str:
    if not template:
        return ""
    items = template.checklist_json or []
    if not isinstance(items, list):
        return ""
    return "\n".join(str(item or "").strip() for item in items if str(item or "").strip())


def list_templates(
    *,
    active_only: bool = False,
    q: str = "",
    doc_type: str = "",
    limit: int = 200,
) -> list[WorkflowPlaybookTemplate]:
    query = WorkflowPlaybookTemplate.query
    if active_only:
        query = query.filter(WorkflowPlaybookTemplate.is_active.is_(True))
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                WorkflowPlaybookTemplate.name.ilike(like),
                WorkflowPlaybookTemplate.doc_type.ilike(like),
                WorkflowPlaybookTemplate.description.ilike(like),
                WorkflowPlaybookTemplate.event_key.ilike(like),
            )
        )
    doc_type = (doc_type or "").strip()
    if doc_type:
        query = query.filter(WorkflowPlaybookTemplate.doc_type == doc_type)
    return (
        query.order_by(
            WorkflowPlaybookTemplate.is_active.desc(),
            WorkflowPlaybookTemplate.sort_order.asc(),
            WorkflowPlaybookTemplate.updated_at.desc(),
        )
        .limit(max(1, min(int(limit or 200), 1000)))
        .all()
    )


def recommend_templates_for_workflow(
    workflow: Workflow,
    *,
    matter: Matter | None = None,
    limit: int = 30,
) -> list[WorkflowPlaybookTemplate]:
    rows = list_templates(active_only=True, limit=500)
    matter_type = str(getattr(matter, "matter_type", "") or "").strip().lower()
    right_group = str(getattr(matter, "right_group", "") or "").strip().lower()
    category = str(getattr(workflow, "category", "") or "").strip().lower()
    name = str(getattr(workflow, "name", "") or "").strip().lower()
    code = str(getattr(workflow, "business_code", "") or "").strip().lower()

    def score(row: WorkflowPlaybookTemplate) -> tuple[int, int, str]:
        value = 0
        if row.category and row.category.strip().lower() == category:
            value += 20
        if row.matter_type and row.matter_type.strip().lower() == matter_type:
            value += 12
        if row.right_group and row.right_group.strip().lower() == right_group:
            value += 8
        event_key = str(row.event_key or "").strip().lower()
        doc_type = str(row.doc_type or "").strip().lower()
        if event_key and (event_key in code or event_key in name):
            value += 30
        if doc_type and (doc_type in code or doc_type in name):
            value += 25
        return (-value, int(row.sort_order or 0), str(row.name or "").lower())

    rows.sort(key=score)
    return rows[: max(1, min(int(limit or 30), 100))]


def render_template_text(
    value: str | None,
    *,
    workflow: Workflow,
    matter: Matter | None,
    template: WorkflowPlaybookTemplate,
) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    context = {
        "our_ref": str(getattr(matter, "our_ref", "") or ""),
        "right_name": str(getattr(matter, "right_name", "") or ""),
        "matter_type": str(getattr(matter, "matter_type", "") or ""),
        "right_group": str(getattr(matter, "right_group", "") or ""),
        "workflow_name": str(getattr(workflow, "name", "") or ""),
        "doc_type": str(getattr(template, "doc_type", "") or ""),
        "template_name": str(getattr(template, "name", "") or ""),
    }
    try:
        return raw.format(**context)
    except (AttributeError, IndexError, KeyError, ValueError):
        return raw


def _date_from_offset(base_date: date, offset_days: int | None) -> date | None:
    if offset_days is None:
        return None
    return base_date + timedelta(days=int(offset_days))


def apply_template_to_workflow(
    *,
    template: WorkflowPlaybookTemplate,
    workflow: Workflow,
    actor_id: int | None = None,
    base_date: date | None = None,
) -> PlaybookApplyResult:
    matter = Matter.query.get(str(workflow.case_id)) if workflow.case_id else None
    base = base_date or workflow.request_start_date or date.today()
    checklist_created = 0
    fields_updated = 0

    existing_titles = {
        str(item.title or "").strip().lower()
        for item in (workflow.checklist_items or [])
        if str(item.title or "").strip()
    }
    checklist = template.checklist_json or []
    if isinstance(checklist, list):
        next_sort = len(existing_titles) + 1
        for title in checklist:
            clean_title = str(title or "").strip()
            key = clean_title.lower()
            if not clean_title or key in existing_titles:
                continue
            item = WorkflowChecklistItem(
                workflow_id=int(workflow.id),
                title=clean_title,
                sort_order=next_sort,
                created_by_id=actor_id,
            )
            db.session.add(item)
            existing_titles.add(key)
            next_sort += 1
            checklist_created += 1

    schedule = template.schedule_json or {}
    if not isinstance(schedule, dict):
        schedule = {}

    date_updates = {
        "request_start_date": _date_from_offset(
            base, parse_int_or_none(schedule.get("request_offset_days"))
        ),
        "legal_due_date": _date_from_offset(
            base, parse_int_or_none(schedule.get("legal_due_offset_days"))
        ),
        "due_date": _date_from_offset(
            base, parse_int_or_none(schedule.get("internal_due_offset_days"))
        ),
        "draft_due_date": _date_from_offset(
            base, parse_int_or_none(schedule.get("draft_due_offset_days"))
        ),
        "submit_due_date": _date_from_offset(
            base, parse_int_or_none(schedule.get("submit_due_offset_days"))
        ),
    }
    for field, value in date_updates.items():
        if value is not None and getattr(workflow, field, None) is None:
            setattr(workflow, field, value)
            fields_updated += 1

    rendered_request = render_template_text(
        template.request_template,
        workflow=workflow,
        matter=matter,
        template=template,
    )
    if rendered_request and not (workflow.note or "").strip():
        workflow.note = rendered_request
        fields_updated += 1

    rendered_memo = render_template_text(
        template.memo_template,
        workflow=workflow,
        matter=matter,
        template=template,
    )
    if rendered_memo and not (workflow.send_memo or "").strip():
        workflow.send_memo = rendered_memo
        fields_updated += 1

    db.session.flush()
    return PlaybookApplyResult(checklist_created=checklist_created, fields_updated=fields_updated)


def create_or_update_template(
    *,
    template: WorkflowPlaybookTemplate | None,
    form,
    actor_id: int | None,
) -> WorkflowPlaybookTemplate:
    row = template or WorkflowPlaybookTemplate(created_by_id=actor_id, created_at=datetime.utcnow())
    row.name = (form.get("name") or "").strip()
    row.doc_type = (form.get("doc_type") or "").strip()
    row.matter_type = (form.get("matter_type") or "").strip()
    row.right_group = (form.get("right_group") or "").strip()
    row.event_key = (form.get("event_key") or "").strip()
    row.category = (form.get("category") or "").strip().upper()
    row.description = (form.get("description") or "").strip() or None
    row.checklist_json = parse_checklist_text(form.get("checklist_text"))
    row.schedule_json = schedule_from_form(form)
    row.request_template = (form.get("request_template") or "").strip() or None
    row.memo_template = (form.get("memo_template") or "").strip() or None
    row.is_active = (
        form.get("is_active") in {"1", "on", "true", "yes"}
        if form.get("is_active") is not None
        else False
    )
    row.sort_order = parse_int_or_none(form.get("sort_order")) or 0
    row.updated_by_id = actor_id
    row.updated_at = datetime.utcnow()
    if row not in db.session:
        db.session.add(row)
    db.session.flush()
    return row
