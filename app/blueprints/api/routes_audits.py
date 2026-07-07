from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app, g, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from app.blueprints.api import bp
from app.blueprints.api.routes import (
    _can_edit_case,
    _case_custom_namespaces,
    _safe_int,
    _update_case_custom_fields,
)
from app.blueprints.case.helpers import _update_basic_matter_info
from app.extensions import db
from app.models.case_audit_log import CaseAuditLog
from app.models.client import Client
from app.models.ip_records import Matter, MatterCustomField, MatterStaffAssignment
from app.models.user import User
from app.services.case.canonical_field_service import upsert_case_flat_index
from app.services.case.status_task_cleanup import apply_case_status_side_effects
from app.utils.permissions import check_permission


@bp.route("/case_audits/<int:audit_id>/undo", methods=["POST"])
@login_required
def case_audit_undo(audit_id: int):
    audit = CaseAuditLog.query.get_or_404(audit_id)
    case_id = str(audit.case_id)

    if not _can_edit_case(case_id):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    if audit.action == "UNDO":
        return jsonify({"ok": False, "error": "already_undone"}), 409

    undo_seconds = int(current_app.config.get("CASE_AUDIT_UNDO_SECONDS", 30))
    if audit.created_at and datetime.utcnow() - audit.created_at > timedelta(seconds=undo_seconds):
        return jsonify({"ok": False, "error": "expired"}), 409

    if getattr(current_user, "id", None) != audit.actor_user_id and not check_permission("admin"):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    matter = Matter.query.get_or_404(case_id)

    def _current_value(field: str):
        if field == "title":
            return (matter.right_name or "").strip()
        if field == "status":
            return (matter.inhouse_status or "").strip()
        if field == "our_ref":
            return (matter.our_ref or "").strip()
        if field == "your_ref":
            return (matter.your_ref or "").strip()
        if field == "assignee_id":
            msa = MatterStaffAssignment.query.filter(
                MatterStaffAssignment.matter_id == case_id,
                func.lower(func.trim(MatterStaffAssignment.staff_role_code)) == "attorney",
            ).first()
            if msa and msa.staff_party_id:
                user = User.query.filter_by(staff_party_id=msa.staff_party_id).first()
                return user.id if user else None
            return None
        if field == "client_id":
            row = (
                MatterCustomField.query.filter(MatterCustomField.matter_id == case_id)
                .filter(MatterCustomField.namespace.in_(_case_custom_namespaces()))
                .first()
            )
            data = (row.data or {}) if row else {}
            return _safe_int(data.get("client_id"))
        return None

    current_val = _current_value(audit.field_name)
    if current_val != audit.new_value:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "conflict",
                    "message": "  Change    none.",
                }
            ),
            409,
        )

    old_value = audit.old_value
    if audit.field_name == "title":
        matter.right_name = old_value or None
        _update_case_custom_fields(case_id, {"proposal_title": old_value or ""})
    elif audit.field_name == "status":
        matter.inhouse_status = old_value or None
    elif audit.field_name == "our_ref":
        matter.our_ref = old_value or None
    elif audit.field_name == "your_ref":
        matter.your_ref = old_value or None
    elif audit.field_name == "assignee_id":
        form_data = {"attorney_id": str(old_value or ""), "attorney": ""}
        _update_basic_matter_info(case_id, form_data)
    elif audit.field_name == "client_id":
        client_name = ""
        if old_value:
            client = Client.query.get(old_value)
            if client:
                client_name = client.name
        _update_case_custom_fields(
            case_id,
            {"client_id": old_value or "", "client_name": client_name or ""},
        )

    undo_audit = CaseAuditLog(
        case_id=case_id,
        actor_user_id=getattr(current_user, "id", None),
        action="UNDO",
        field_name=audit.field_name,
        old_value=audit.new_value,
        new_value=audit.old_value,
        request_id=getattr(g, "request_id", None),
    )
    db.session.add(undo_audit)
    db.session.commit()
    if audit.field_name == "status":
        apply_case_status_side_effects(
            matter_id=case_id,
            old_status=audit.new_value,
            new_status=audit.old_value,
            actor_id=getattr(current_user, "id", None),
            logger_override=current_app.logger,
        )
    try:
        upsert_case_flat_index(case_id)
        db.session.commit()
    except (AttributeError, RuntimeError, SQLAlchemyError, ValueError) as exc:
        db.session.rollback()
        current_app.logger.warning(
            "case_audit_undo: flat index refresh failed for %s: %s", case_id, exc
        )

    return jsonify({"ok": True, "audit_id": undo_audit.id})


@bp.route("/cases/<string:case_id>/audits")
@login_required
def case_audit_list(case_id: str):
    limit = _safe_int(request.args.get("limit"), 5) or 5
    limit = max(1, min(limit, 200))

    rows = (
        db.session.query(CaseAuditLog, User)
        .outerjoin(User, User.id == CaseAuditLog.actor_user_id)
        .filter(CaseAuditLog.case_id == str(case_id))
        .order_by(CaseAuditLog.created_at.desc(), CaseAuditLog.id.desc())
        .limit(limit)
        .all()
    )

    undo_seconds = int(current_app.config.get("CASE_AUDIT_UNDO_SECONDS", 30))
    now = datetime.utcnow()
    can_admin_undo = check_permission("admin")
    undo_fields = {
        "title",
        "status",
        "our_ref",
        "your_ref",
        "assignee_id",
        "client_id",
    }

    assignee_ids: set[int] = set()
    client_ids: set[int] = set()

    def _collect_id(value, target_set: set[int]) -> None:
        if value is None or value == "":
            return
        vid = _safe_int(value)
        if vid:
            target_set.add(vid)

    def _collect_id_from_payload(payload, key: str, target_set: set[int]) -> None:
        if not isinstance(payload, dict):
            return
        _collect_id(payload.get(key), target_set)

    for audit, _user in rows:
        field = (audit.field_name or "").strip()
        if field == "assignee_id":
            _collect_id(audit.old_value, assignee_ids)
            _collect_id(audit.new_value, assignee_ids)
        if field == "client_id":
            _collect_id(audit.old_value, client_ids)
            _collect_id(audit.new_value, client_ids)
        _collect_id_from_payload(audit.old_value, "assignee_id", assignee_ids)
        _collect_id_from_payload(audit.new_value, "assignee_id", assignee_ids)
        _collect_id_from_payload(audit.old_value, "client_id", client_ids)
        _collect_id_from_payload(audit.new_value, "client_id", client_ids)

    user_map = {}
    if assignee_ids:
        for u in User.query.filter(User.id.in_(assignee_ids)).all():
            user_map[u.id] = (
                (getattr(u, "display_name", None) or "").strip()
                or u.username
                or u.email
                or f"User {u.id}"
            )

    client_map = {}
    if client_ids:
        for c in Client.query.filter(Client.id.in_(client_ids)).all():
            client_map[c.id] = c.name or f"Client {c.id}"

    def _display_for_field(field: str, value):
        if field == "assignee_id":
            vid = _safe_int(value)
            if not vid:
                return None
            return user_map.get(vid) or str(vid)
        if field == "client_id":
            vid = _safe_int(value)
            if not vid:
                return None
            return client_map.get(vid) or str(vid)
        return None

    items = []
    for audit, user in rows:
        action = (audit.action or "").upper()
        field = (audit.field_name or "").strip()
        actor_name = user.username if user else None
        actor = actor_name
        # actor_type/UI Display :
        # - user  : action SYSTEM/AI  Display
        # - user  : PATCH/UNDO  action (UPLOAD/MEMO/FILE/DEADLINE) table Add
        actor_type = None
        if not actor:
            if action in ("SYSTEM", "AI"):
                actor = action
                actor_type = action
            else:
                actor = "SYSTEM" if not audit.actor_user_id else None
        else:
            if action not in ("PATCH", "UNDO"):
                actor = f"{actor} ({action})"

        undo_expires_at = None
        undo_expires_in = None
        if audit.created_at:
            undo_deadline = audit.created_at + timedelta(seconds=undo_seconds)
            undo_expires_at = undo_deadline.isoformat()
            undo_expires_in = max(0, int((undo_deadline - now).total_seconds()))

        undo_allowed = False
        if (
            action == "PATCH"
            and field in undo_fields
            and audit.created_at
            and undo_expires_in
            and undo_expires_in > 0
            and ((getattr(current_user, "id", None) == audit.actor_user_id) or can_admin_undo)
        ):
            undo_allowed = True

        old_display = _display_for_field(field, audit.old_value)
        new_display = _display_for_field(field, audit.new_value)
        items.append(
            {
                "id": audit.id,
                "field": field,
                "old_value": audit.old_value,
                "new_value": audit.new_value,
                "old_display": old_display,
                "new_display": new_display,
                "action": action or (audit.action or ""),
                "actor_id": audit.actor_user_id,
                "actor": actor,
                "actor_name": actor_name,
                "actor_type": actor_type,
                "created_at": audit.created_at.isoformat() if audit.created_at else None,
                "request_id": audit.request_id,
                "undo_allowed": undo_allowed,
                "undo_expires_at": undo_expires_at,
                "undo_expires_in": undo_expires_in,
            }
        )

    return jsonify({"ok": True, "items": items})
