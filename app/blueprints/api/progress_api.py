from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.api import bp
from app.extensions import db
from app.models.worklog import WorkLog
from app.services.case.case_audit_service import record_case_audit
from app.utils.permissions import can_access_matter


@bp.route("/cases/<matter_id>/progress", methods=["POST"])
@login_required
def api_progress_add(matter_id):
    if not can_access_matter(current_user, matter_id, action="edit_case"):
        return jsonify({"error": "forbidden"}), 403

    content = (request.json or {}).get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400

    wl = WorkLog(
        matter_id=str(matter_id),
        description=content,
        action_type="note",
        status="pending",
        completed_by_id=current_user.id,
    )
    db.session.add(wl)

    record_case_audit(
        case_id=str(matter_id),
        action="USER",
        field_name="api.progress.add",
        actor_user_id=getattr(current_user, "id", None),
        old_value=None,
        new_value={"worklog_id": wl.id, "preview": content[:200]},
    )

    db.session.commit()
    return jsonify({"ok": True, "id": wl.id, "matter_id": str(matter_id)})
