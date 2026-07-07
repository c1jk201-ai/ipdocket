from __future__ import annotations

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.services.ops.security_health import accept_key_rotation, get_security_health

bp = Blueprint("admin_security_health", __name__, url_prefix="/admin/security")


def _is_admin_user(user) -> bool:
    #  User  fit  
    if getattr(user, "is_admin", False):
        return True
    role = (getattr(user, "role", "") or getattr(user, "user_role", "") or "").lower()
    return role in ("admin", "superadmin", "mgmt_director")


@bp.get("/health.json")
@login_required
def security_health_json():
    if not _is_admin_user(current_user):
        return jsonify({"error": "forbidden"}), 403
    return jsonify(get_security_health())


@bp.get("/health")
@login_required
def security_health_page():
    if not _is_admin_user(current_user):
        return "Forbidden", 403
    return render_template(
        "admin/security_health.html",
        health=get_security_health(),
        active_page="security_health",
    )


@bp.post("/accept-key-rotation")
@login_required
def accept_key_rotation_post():
    if not _is_admin_user(current_user):
        return "Forbidden", 403
    accept_key_rotation(current_app)
    flash(" Change fingerprint (Updated).", "success")
    return redirect(url_for("admin_security_health.security_health_page"))
