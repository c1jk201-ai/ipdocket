from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter, MatterCustomField
from app.services.case.case_audit_service import record_case_audit
from app.utils.permissions import require_matter_access


@bp.route("/<case_id>/custom/<namespace>", methods=["POST"])
@login_required
def save_custom_text(case_id: str, namespace: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    allowed = {
        "priority",
        "license",
        "transfer",
        "progress_misc",
        "progress",
        "old_workflow",
    }
    if namespace not in allowed:
        abort(404)

    text_value = (request.form.get("text") or "").strip()
    row = MatterCustomField.query.filter_by(matter_id=str(case_id), namespace=namespace).first()
    old_text = None
    if not row:
        row = MatterCustomField(matter_id=str(case_id), namespace=namespace, data={})
        db.session.add(row)
    else:
        try:
            old_text = (row.data or {}).get("text")
        except Exception:
            old_text = None
    row.data = {"text": text_value}

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name=f"custom_text.{namespace}",
        actor_user_id=getattr(current_user, "id", None),
        old_value={"text": (old_text or "")[:200]},
        new_value={"text": (text_value or "")[:200]},
    )
    db.session.commit()
    flash("Save.", "success")
    sec = f"sec-{namespace}".replace("_", "-")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor=sec))
