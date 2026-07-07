from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from flask import g, has_request_context
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.audit_log import AuditLog
from app.utils.error_logging import report_swallowed_exception

ACTION_LABELS = {
    "admin.config.delete": "System Settings Delete",
    "admin.config.update": "System Settings Change",
    "admin.deadline_settings.update": "Deadline Settings Change",
    "admin.uspto_notice_due_policy.update": "USPTO Deadline Change",
    "admin.staff.create": "Staff Create",
    "admin.staff.delete": "Staff Delete",
    "admin.staff.reassign": "Staff task reassignment",
    "admin.staff.update": "Staff Edit",
    "admin.user.create": "User Create",
    "admin.user.provision": "User ",
    "admin.user.update": "User Edit",
    "annuity.delete": "Renewal Delete",
    "annuity.status_change": "Renewal Status Change",
    "annuity.update": "Renewal Edit",
    "client.attachment.delete": "Client  Delete",
    "client.attachment.upload": "Client  Upload",
    "client.create": "Client Create",
    "client.delete": "Client Delete",
    "client.suggestion.apply": "Client value Apply",
    "client.update": "Client Edit",
    "document.letter.create": " Create",
    "document.letter.delete": " Delete",
    "expense.create": " Create",
    "expense.delete": " Delete",
    "expense.invoice_link.create": " Invoice link",
    "expense.invoice_link.delete": " Invoice link removed",
    "expense.invoice_link.update": " Invoice link Edit",
    "expense.payment.create": "  Create",
    "expense.payment.delete": "  Delete",
    "expense.update": " Edit",
    "settings.config.update": "Settings Change",
    "workflow.create": "Task Create",
    "workflow.update": "Task Edit",
    "workflow.status_change": "Task Status Change",
    "workflow.delete": "Task Delete",
    "worklog.bulk_transfer": "Task Bulk Previous",
    "docket.create": "Deadline Create",
    "docket.update": "Deadline Edit",
    "docket.status_change": "Deadline Status Change",
    "docket.delete": "Deadline Delete",
}

FIELD_LABELS = {
    "active": "active ",
    "address": "Address",
    "annuity_status": "Renewal Status",
    "assignee_id": "Handler",
    "billing_invoice_id": "Billing ID",
    "billing_line_item_id": "Billing Item ID",
    "case_id": "Matter ID",
    "category_code": " Type",
    "client_code": "Client ",
    "created_by_id": "Create",
    "currency": "Currency",
    "cycle_no": "Renewal",
    "delete_reason": "Delete Reason",
    "deleted_at": "Delete",
    "department": "Department",
    "description": "Description",
    "display_name": "Display",
    "dn_date": "DN ",
    "dn_no": "DN ",
    "due_date": "Internal Due date",
    "email": "Email",
    "expense_date": "",
    "expense_ref": " ",
    "extended_due_date": " Due date",
    "internal_due_date": "Internal/ Due date",
    "is_active": "Account active",
    "is_deleted": "Delete ",
    "key": "Settings ",
    "manager": "Contact",
    "matter_id": "Matter ID",
    "memo": "Notes",
    "name_display": "Display",
    "official_fee": "Official fee",
    "outstanding_amount": " Balance",
    "owner_staff_party_id": "Responsible Staff",
    "paid_date": "Payment",
    "party_id": "Staff ID",
    "phone": "",
    "position": "",
    "raw_id": "Original ID",
    "remit_no": " ",
    "remit_total": " ",
    "requested_total": "Invoice amount",
    "roles": "Permissions",
    "sent_amount": "",
    "sent_date": "",
    "service_fee": "Service Fee",
    "staff_code": "Staff ",
    "staff_party_id": "Link staff",
    "tax_no": "Tax documentation number",
    "total": "Amount",
    "type": "Type",
    "username": "User",
    "value": "value",
    "vat_amount": "Sales tax",
    "vendor_name": "",
    "name": "Task",
    "title": "Title",
    "status": "Status",
    "category": "Type",
    "priority": "Priority",
    "business_code": " ",
    "request_start_date": "Task",
    "legal_due_date": "Final Due date",
    "draft_due_date": "Draft Deadline",
    "draft_due_date2": "2 Draft Deadline",
    "submit_due_date": " Deadline",
    "draft_sent_date": "Draft Send",
    "submit_date": "",
    "completed_date": "",
    "difficulty": "",
    "page_count": "page",
    "work_hours": "Task(TC)",
    "attorney_assignee_id": "Responsible attorney",
    "inspector_id": "Manager",
    "note": "/Details Content",
    "send_memo": " Notes",
    "visible_from_date": "Task ",
    "notes": "Notes",
}


def audit_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + f"...(+{len(value) - 2000})"
        return value
    if isinstance(value, dict):
        return {str(k): audit_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [audit_json_value(v) for v in value]
    return str(value)


def diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(before) | set(after)):
        old = audit_json_value(before.get(key))
        new = audit_json_value(after.get(key))
        if old != new:
            changes[key] = {"from": old, "to": new}
    return changes


def snapshot_attrs(obj: Any, fields: list[str] | tuple[str, ...]) -> dict[str, Any]:
    return {field: audit_json_value(getattr(obj, field, None)) for field in fields}


def _request_id() -> str | None:
    if not has_request_context():
        return None
    try:
        return getattr(g, "request_id", None)
    except Exception:
        return None


def record_entity_audit(
    *,
    action: str,
    target_type: str,
    target_id: int | None = None,
    actor_id: int | None = None,
    meta: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog | None:
    try:
        normalized_actor_id = int(actor_id) if actor_id is not None else None
    except (TypeError, ValueError):
        normalized_actor_id = None

    try:
        row = AuditLog(
            request_id=request_id or _request_id(),
            actor_id=normalized_actor_id,
            user_id=normalized_actor_id,
            action=str(action or "").strip(),
            target_type=str(target_type or "").strip(),
            target_id=target_id,
            meta_json=json.dumps(audit_json_value(meta or {}), ensure_ascii=False),
        )
        db.session.add(row)
        return row
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="entity_audit.record_entity_audit",
            log_key="entity_audit.record_entity_audit",
            log_window_seconds=300,
        )
        return None


def record_entity_change_audit(
    *,
    action: str,
    target_type: str,
    target_id: int | None = None,
    actor_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    changes: dict[str, dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    title: str | None = None,
    request_id: str | None = None,
    include_snapshots: bool = False,
) -> AuditLog | None:
    payload = dict(meta or {})
    if title:
        payload.setdefault("title", title)
    if changes is None and before is not None and after is not None:
        changes = diff_snapshots(before, after)
    if changes:
        payload["changes"] = changes
    if include_snapshots:
        if before is not None:
            payload["before"] = before
        if after is not None:
            payload["after"] = after
    return record_entity_audit(
        action=action,
        target_type=target_type,
        target_id=target_id,
        actor_id=actor_id,
        meta=payload,
        request_id=request_id,
    )


def _target_type_variants(target_type: str) -> list[str]:
    raw = str(target_type or "").strip()
    variants = {raw, raw.lower(), raw.upper()}
    if raw:
        variants.add(raw[:1].upper() + raw[1:].lower())
    return [v for v in variants if v]


def _parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {"raw_meta": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _actor_name(row: AuditLog, meta: dict[str, Any]) -> str:
    actor_display = str(meta.get("actor_display_name") or "").strip()
    if actor_display:
        return actor_display
    actor = getattr(row, "actor", None)
    if actor is not None:
        return (
            str(getattr(actor, "display_name", None) or "").strip()
            or str(getattr(actor, "username", None) or "").strip()
            or str(getattr(actor, "email", None) or "").strip()
            or f"User #{getattr(actor, 'id', '')}"
        )
    actor_id = getattr(row, "actor_id", None) or getattr(row, "user_id", None)
    return f"User #{actor_id}" if actor_id else "SYSTEM"


def _display_value(value: Any, *, limit: int = 120) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + f"...(+{len(text) - limit})"
    return text


def _change_lines(meta: dict[str, Any]) -> list[dict[str, str]]:
    raw_changes = meta.get("changes")
    changes: list[dict[str, str]] = []
    if isinstance(raw_changes, dict):
        for field, payload in raw_changes.items():
            if not isinstance(payload, dict):
                continue
            changes.append(
                {
                    "field": str(field),
                    "label": FIELD_LABELS.get(str(field), str(field)),
                    "from": _display_value(payload.get("from")),
                    "to": _display_value(payload.get("to")),
                }
            )

    if not changes and {"old_status", "new_status"}.issubset(meta):
        changes.append(
            {
                "field": "status",
                "label": FIELD_LABELS["status"],
                "from": _display_value(meta.get("old_status")),
                "to": _display_value(meta.get("new_status")),
            }
        )
    return changes


def format_audit_row(row: AuditLog) -> dict[str, Any]:
    meta = _parse_meta(getattr(row, "meta_json", None))
    action = str(getattr(row, "action", "") or "").strip()
    changes = _change_lines(meta)
    summary = str(meta.get("title") or meta.get("name") or "").strip()
    if not summary:
        summary = ACTION_LABELS.get(action, action or " ")
    return {
        "id": getattr(row, "id", None),
        "action": action,
        "action_label": ACTION_LABELS.get(action, action or "-"),
        "actor": _actor_name(row, meta),
        "created_at": getattr(row, "created_at", None),
        "summary": summary,
        "changes": changes,
        "meta": meta,
    }


def load_audit_rows_for_target(
    *,
    target_type: str,
    target_id: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    try:
        rows = (
            AuditLog.query.options(joinedload(AuditLog.actor))
            .filter(AuditLog.target_type.in_(_target_type_variants(target_type)))
            .filter(AuditLog.target_id == int(target_id))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, min(int(limit), 50)))
            .all()
        )
        return [format_audit_row(row) for row in rows]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="entity_audit.load_audit_rows_for_target",
            log_key="entity_audit.load_audit_rows_for_target",
            log_window_seconds=300,
        )
        return []


def load_audit_rows_by_meta_value(
    *,
    target_type: str,
    meta_key: str,
    meta_value: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    token = str(meta_value or "").strip()
    if not token:
        return []
    try:
        candidates = (
            AuditLog.query.options(joinedload(AuditLog.actor))
            .filter(AuditLog.target_type.in_(_target_type_variants(target_type)))
            .filter(AuditLog.meta_json.contains(token))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(10, min(int(limit) * 4, 100)))
            .all()
        )
        out: list[dict[str, Any]] = []
        for row in candidates:
            meta = _parse_meta(getattr(row, "meta_json", None))
            if str(meta.get(meta_key) or "").strip() != token:
                continue
            out.append(format_audit_row(row))
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="entity_audit.load_audit_rows_by_meta_value",
            log_key="entity_audit.load_audit_rows_by_meta_value",
            log_window_seconds=300,
        )
        return []
