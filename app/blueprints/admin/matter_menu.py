from __future__ import annotations

import json
from typing import Any

from flask import current_app, jsonify, render_template, request
from flask_login import login_required

from app.blueprints.admin import bp
from app.blueprints.admin.routes import _case_menu_field_options, _record_config_audit
from app.extensions import db
from app.models.system_config import SystemConfig
from app.services.case.case_menu_config import (
    CASE_MENU_CONFIG_KEY,
    case_menu_config_json_for_editor,
    default_case_menu_config_json,
    preview_case_menu_config,
    validate_case_menu_config_payload,
)
from app.services.core.config_service import ConfigService
from app.services.core.staff_options import clear_staff_assignment_cache
from app.utils.permissions import role_required


def _extract_case_menu_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        if "value" in payload:
            return payload.get("value")
        if CASE_MENU_CONFIG_KEY in payload:
            return payload.get(CASE_MENU_CONFIG_KEY)
    return payload


def _json_string_for_storage(value: Any) -> str:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return json.dumps(value if value is not None else {}, ensure_ascii=False, indent=2)


def _reload_case_menu_runtime() -> None:
    try:
        ConfigService.clear_cache()
    except (RuntimeError, ValueError):
        current_app.logger.warning("ConfigService cache clear skipped", exc_info=True)
    try:
        from app.services.case.case_parameter_service import CaseParameterService

        CaseParameterService.reload_if_changed()
    except (ImportError, RuntimeError, ValueError):
        current_app.logger.warning("Case field registry reload skipped", exc_info=True)
    clear_staff_assignment_cache()


@bp.route("/matter-create-menu")
@login_required
@role_required("admin")
def matter_create_menu_page():
    return render_template(
        "admin/matter_create_menu.html",
        active_page="matter_create_menu",
        case_menu_value=case_menu_config_json_for_editor(),
        case_menu_config_key=CASE_MENU_CONFIG_KEY,
        default_case_menu_config=case_menu_config_json_for_editor(default_case_menu_config_json()),
        case_menu_preview=preview_case_menu_config(),
        case_menu_field_options=_case_menu_field_options(),
    )


@bp.route("/api/matter-create-menu/validate", methods=["POST"])
@login_required
@role_required("admin")
def api_matter_create_menu_validate():
    payload = request.get_json(silent=True)
    value = _extract_case_menu_payload(payload)
    validation = validate_case_menu_config_payload(value)
    response = {"success": bool(validation.get("valid")), **validation}
    if validation.get("valid"):
        response["value"] = case_menu_config_json_for_editor(value)
    return jsonify(response)


@bp.route("/api/matter-create-menu", methods=["POST"])
@login_required
@role_required("admin")
def api_matter_create_menu():
    payload = request.get_json(silent=True)
    value = _extract_case_menu_payload(payload)
    validation = validate_case_menu_config_payload(value)
    if not validation.get("valid"):
        return jsonify({"success": False, "error": "invalid_case_menu_config", **validation}), 400

    old_value = SystemConfig.get_config(CASE_MENU_CONFIG_KEY, "")
    new_value = _json_string_for_storage(value)
    SystemConfig.set_config(CASE_MENU_CONFIG_KEY, new_value)
    _record_config_audit(
        action="admin.matter_create_menu.update",
        key=CASE_MENU_CONFIG_KEY,
        old_value=old_value,
        new_value=new_value,
        source="admin.api_matter_create_menu",
    )
    db.session.commit()
    _reload_case_menu_runtime()
    return jsonify(
        {
            "success": True,
            "preview": validation.get("preview") or preview_case_menu_config(),
            "value": case_menu_config_json_for_editor(new_value),
        }
    )
