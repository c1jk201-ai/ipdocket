import json

from app.models.system_config import SystemConfig
from app.services.case.case_menu_config import (
    CASE_MENU_CONFIG_KEY,
    default_case_menu_config,
    validate_case_menu_config_payload,
)
from app.services.case.case_parameter_service import CaseParameterService
from app.services.core.config_service import ConfigService


def _clear_case_menu_config(db_session) -> None:
    row = SystemConfig.query.filter_by(key=CASE_MENU_CONFIG_KEY).first()
    if row:
        db_session.delete(row)
        db_session.commit()
    ConfigService.clear_cache()
    CaseParameterService.reload_if_changed()


def test_admin_matter_create_menu_page_renders_wizard_editor(admin_client):
    res = admin_client.get("/admin/matter-create-menu")
    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")

    assert 'id="matter-create-menu-wizard"' in body
    assert 'id="matter-create-menu-editor"' in body
    assert 'id="case-menu-json"' in body
    assert "/admin/case-parameters" in body
    assert "New Matter Wizard" in body
    assert "parameter-health" in body
    assert "matter-form-preview" in body
    assert "case-menu-group-options" in body
    assert "parameter-add-group-order" in body
    assert "Default/Responsible" in body
    assert "group_order" in body
    assert body.index("Matter Create Menu") < body.index("Matter Parameters")


def test_admin_matter_create_menu_api_saves_case_menu(admin_client, db_session):
    try:
        _clear_case_menu_config(db_session)
        payload = default_case_menu_config()
        payload["sections"][0]["items"][0]["fields"] = [
            {
                "key": "application_no",
                "order": 10,
                "col": 1,
                "required": True,
                "group": "Default/Responsible",
                "group_order": 10,
            }
        ]

        res = admin_client.post("/admin/api/matter-create-menu", json={"value": payload})

        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["preview"]["sections"][0]["items"][0]["field_count"] == 1

        saved = SystemConfig.get_config(CASE_MENU_CONFIG_KEY, "")
        assert "application_no" in saved
        assert "Default/Responsible" in saved
        assert "group_order" in saved
    finally:
        _clear_case_menu_config(db_session)


def test_admin_matter_create_menu_validate_returns_normalized_editor_value(
    admin_client, db_session
):
    try:
        _clear_case_menu_config(db_session)
        payload = default_case_menu_config()
        payload["sections"][0]["items"][0]["fields"] = []

        res = admin_client.post("/admin/api/matter-create-menu/validate", json={"value": payload})

        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["valid"] is True

        editor_value = json.loads(data["value"])
        dom_patent = editor_value["sections"][0]["items"][0]
        assert dom_patent["fields_inherited"] is True
        assert len(dom_patent["fields"]) > 50
        assert dom_patent["fields"][0]["key"] == "client_mgmt_no"
    finally:
        _clear_case_menu_config(db_session)


def test_matter_create_menu_validate_warns_duplicate_parameter_keys(db_session):
    try:
        _clear_case_menu_config(db_session)
        payload = default_case_menu_config()
        payload["sections"][0]["items"][0]["fields"] = [
            {"key": "application_no", "order": 10, "col": 1, "group": "Default/Responsible"},
            {"key": "application_no", "order": 20, "col": 2, "group": "Default/Responsible"},
        ]

        validation = validate_case_menu_config_payload(payload)

        assert validation["valid"] is True
        assert any("duplicate field keys application_no" in warning for warning in validation["warnings"])
        assert validation["preview"]["sections"][0]["items"][0]["groups"] == ["Default/Responsible"]
    finally:
        _clear_case_menu_config(db_session)
