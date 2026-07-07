from __future__ import annotations

from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter, MatterCustomField
from app.services.case.case_audit_service import record_case_audit
from app.utils.permissions import require_matter_access
from app.utils.policy_sql import policy_text as text

_HISTORY_ORDER_NAMESPACE = "history_order"
_MAX_ORDER_ITEMS = 4000


def _normalize_order_keys(raw_order: object) -> list[str]:
    if not isinstance(raw_order, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_order:
        raw = str(item or "").strip()
        if not raw or ":" not in raw:
            continue
        kind_raw, row_id_raw = raw.split(":", 1)
        kind = (kind_raw or "").strip().lower()
        row_id = (row_id_raw or "").strip()
        if kind not in {"notice", "letter"} or not row_id:
            continue
        key = f"{kind}:{row_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= _MAX_ORDER_ITEMS:
            break
    return out


def _valid_history_keys(case_id: str, ordered_keys: list[str]) -> list[str]:
    if not ordered_keys:
        return []

    notice_ids = {
        str(row[0] or "")
        for row in db.session.execute(
            text(
                """
                SELECT oa.oa_id
                FROM office_action oa
                WHERE oa.matter_id = :mid
                  AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
                  AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
                  AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
                """
            ).execution_options(policy_bypass=True),
            {"mid": str(case_id)},
        ).all()
    }
    letter_ids = {
        str(row[0] or "")
        for row in db.session.execute(
            text(
                """
                SELECT c.comm_id
                FROM communication c
                WHERE c.matter_id = :mid
                  AND (c.comm_type IS NULL OR TRIM(c.comm_type) = '' OR c.comm_type IN ('M', 'R', 'T'))
                """
            ).execution_options(policy_bypass=True),
            {"mid": str(case_id)},
        ).all()
    }

    valid: list[str] = []
    for key in ordered_keys:
        kind, row_id = key.split(":", 1)
        if kind == "notice" and row_id in notice_ids:
            valid.append(key)
        elif kind == "letter" and row_id in letter_ids:
            valid.append(key)
    return valid


def _load_saved_order(case_id: str) -> list[str]:
    row = MatterCustomField.query.filter_by(
        matter_id=str(case_id),
        namespace=_HISTORY_ORDER_NAMESPACE,
    ).first()
    if not row or not isinstance(row.data, dict):
        return []
    out: list[str] = []
    for item in row.data.get("order") or []:
        key = str(item or "").strip()
        if key:
            out.append(key)
    return out


@bp.post("/<case_id>/history/order")
@login_required
def save_history_order(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    payload = request.get_json(silent=True) or {}
    raw_order = payload.get("order")
    if not isinstance(raw_order, list):
        return jsonify({"ok": False, "message": "order(list) value required."}), 400

    normalized = _normalize_order_keys(raw_order)
    valid_order = _valid_history_keys(case_id, normalized)
    dropped_count = max(0, len(normalized) - len(valid_order))

    row = MatterCustomField.query.filter_by(
        matter_id=str(case_id), namespace=_HISTORY_ORDER_NAMESPACE
    ).first()
    old_order = _load_saved_order(case_id)
    if not row:
        row = MatterCustomField(
            matter_id=str(case_id),
            namespace=_HISTORY_ORDER_NAMESPACE,
            data={},
        )
        db.session.add(row)

    row.data = {
        "order": valid_order,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "updated_by": getattr(current_user, "id", None),
    }

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="history.order",
        actor_user_id=getattr(current_user, "id", None),
        old_value={"order_count": len(old_order)},
        new_value={"order_count": len(valid_order), "dropped_count": dropped_count},
    )
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "order_count": len(valid_order),
            "dropped_count": dropped_count,
        }
    )


@bp.delete("/<case_id>/history/order")
@login_required
def reset_history_order(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    row = MatterCustomField.query.filter_by(
        matter_id=str(case_id), namespace=_HISTORY_ORDER_NAMESPACE
    ).first()
    old_order = _load_saved_order(case_id)
    if row:
        db.session.delete(row)

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="history.order",
        actor_user_id=getattr(current_user, "id", None),
        old_value={"order_count": len(old_order)},
        new_value={"order_count": 0},
    )
    db.session.commit()
    return jsonify({"ok": True})
