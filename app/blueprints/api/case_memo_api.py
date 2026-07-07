from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.api import bp
from app.extensions import db
from app.models.ip_records import MatterMemo
from app.services.case.case_audit_service import record_case_audit
from app.utils.permissions import can_access_matter


@bp.route("/cases/<matter_id>/memos", methods=["POST"])
@login_required
def api_case_memo_create(matter_id):
    if not can_access_matter(current_user, matter_id, action="edit_case"):
        return jsonify({"error": "forbidden"}), 403

    content = (request.json or {}).get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400

    memo = MatterMemo(
        matter_id=str(matter_id),
        body=content,
        created_by_name=getattr(current_user, "username", "API"),
        created_by_id=getattr(current_user, "id", None),
    )
    db.session.add(memo)

    record_case_audit(
        case_id=str(matter_id),
        action="USER",
        field_name="api.memo.add",
        actor_user_id=getattr(current_user, "id", None),
        old_value=None,
        new_value={"memo_id": memo.id, "preview": content[:200]},
    )
    db.session.commit()

    return jsonify({"ok": True, "id": memo.id, "matter_id": str(matter_id)})
