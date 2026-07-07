from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import login_required

from app.blueprints.admin import bp
from app.services.case_fields.parameter_admin import (
    build_parameter_admin_snapshot,
    delete_field_definition,
    repair_baseline_registry,
    upsert_field_definition,
    upsert_mapping,
)
from app.utils.permissions import role_required


@bp.route("/case-parameters")
@login_required
@role_required("admin")
def case_parameters_page():
    return render_template(
        "admin/case_parameters.html",
        active_page="case_parameters",
        snapshot=build_parameter_admin_snapshot(),
    )


@bp.route("/api/case-parameters", methods=["GET"])
@login_required
@role_required("admin")
def api_case_parameters_snapshot():
    return jsonify({"success": True, "snapshot": build_parameter_admin_snapshot()})


@bp.route("/api/case-parameters/field", methods=["POST"])
@login_required
@role_required("admin")
def api_case_parameter_field():
    payload = request.get_json(silent=True) or {}
    try:
        snapshot = upsert_field_definition(payload)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, "snapshot": snapshot})


@bp.route("/api/case-parameters/field/<field_key>", methods=["DELETE"])
@login_required
@role_required("admin")
def api_case_parameter_field_delete(field_key: str):
    force = str(request.args.get("force") or "").lower() in {"1", "true", "yes"}
    try:
        snapshot = delete_field_definition(field_key, force=force)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, "snapshot": snapshot})


@bp.route("/api/case-parameters/mapping", methods=["POST"])
@login_required
@role_required("admin")
def api_case_parameter_mapping():
    payload = request.get_json(silent=True) or {}
    try:
        snapshot = upsert_mapping(payload)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, "snapshot": snapshot})


@bp.route("/api/case-parameters/repair-baseline", methods=["POST"])
@login_required
@role_required("admin")
def api_case_parameter_repair_baseline():
    return jsonify({"success": True, "snapshot": repair_baseline_registry()})
