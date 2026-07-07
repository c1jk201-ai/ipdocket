from __future__ import annotations

import json

from bs4 import BeautifulSoup

from app.models.system_config import SystemConfig
from app.services.case.case_menu_config import (
    CASE_MENU_CONFIG_KEY,
    case_menu_config_json_for_editor,
    default_case_menu_config,
    get_case_menu_config,
    get_case_menu_mapping_overrides,
)
from app.services.case.case_parameter_service import CaseParameterService
from app.services.core.config_service import ConfigService


def _reload_case_config() -> None:
    ConfigService.clear_cache()
    CaseParameterService.reload_if_changed()


def _set_case_menu_config(db_session, payload: dict) -> None:
    SystemConfig.set_config(CASE_MENU_CONFIG_KEY, json.dumps(payload))
    db_session.commit()
    _reload_case_config()


def _clear_case_menu_config(db_session) -> None:
    row = SystemConfig.query.filter_by(key=CASE_MENU_CONFIG_KEY).first()
    if row:
        db_session.delete(row)
        db_session.commit()
    _reload_case_config()


def _find_item(config: dict, division: str, case_type: str) -> dict:
    return next(
        item
        for section in config["sections"]
        for item in section["items"]
        if item["division"] == division and item["type"] == case_type
    )


def test_standard_case_menu_editor_inherits_profile_fields(app, db_session) -> None:
    try:
        _clear_case_menu_config(db_session)

        editor_config = json.loads(case_menu_config_json_for_editor(default_case_menu_config()))
        dom_patent = _find_item(editor_config, "DOM", "PATENT")

        assert dom_patent["namespace"] == "domestic_patent"
        assert dom_patent["fields_inherited"] is True
        assert len(dom_patent["fields"]) > 50
        assert dom_patent["fields"][0]["key"] == "client_mgmt_no"

        override = next(
            item for item in get_case_menu_mapping_overrides() if item["key"] == "IP:DOM:PATENT"
        )
        assert override["fields"] == []
        assert override["inherit"] == "IP:DOM:PATENT"
    finally:
        _clear_case_menu_config(db_session)


def test_saved_empty_standard_fields_restore_inherited_profile(db_session) -> None:
    config = default_case_menu_config()
    dom_patent = _find_item(config, "DOM", "PATENT")
    dom_patent["namespace"] = "custom_dom_patent"
    dom_patent["fields"] = []

    try:
        _set_case_menu_config(db_session, config)

        normalized = get_case_menu_config()
        restored = _find_item(normalized, "DOM", "PATENT")
        assert restored["namespace"] == "domestic_patent"
        assert restored["fields_inherited"] is True
        assert len(restored["fields"]) > 50

        override = next(
            item for item in get_case_menu_mapping_overrides() if item["key"] == "IP:DOM:PATENT"
        )
        assert override["fields"] == []
    finally:
        _clear_case_menu_config(db_session)


def test_custom_case_menu_item_renders_select_and_create_fields(
    authenticated_client, db_session
) -> None:
    config = default_case_menu_config()
    incoming = next(section for section in config["sections"] if section["id"] == "incoming")
    incoming["items"] = [
        {
            "id": "inc-custom-dd",
            "label": "Custom-dd",
            "division": "INC",
            "type": "Custom-dd",
            "profile_division": "INC",
            "profile_type": "PATENT",
            "namespace": "incoming_custom_dd",
            "order": 10,
            "fields": [
                {"key": "application_no", "order": 1, "col": 1},
                {"key": "application_date", "order": 1, "col": 2},
            ],
        }
    ]

    try:
        _set_case_menu_config(db_session, config)

        select_resp = authenticated_client.get("/case/matter/create/select")
        assert select_resp.status_code == 200
        select_html = select_resp.get_data(as_text=True)
        assert "Custom-dd" in select_html
        assert "type=CUSTOM-DD" in select_html

        create_resp = authenticated_client.get("/case/matter/create?division=INC&type=Custom-dd")
        assert create_resp.status_code == 200
        soup = BeautifulSoup(create_resp.get_data(as_text=True), "html.parser")

        assert soup.select_one('[name="case_type"][value="CUSTOM-DD"]') is not None
        assert soup.select_one('[name="application_no"]') is not None
        assert soup.select_one('[name="application_date"]') is not None
        assert soup.select_one("#caseCategoryIncoming .nav-link.active") is not None
        assert soup.select_one("#caseCategoryIncoming").get("class")
        assert "show" in soup.select_one("#caseCategoryIncoming").get("class")
    finally:
        _clear_case_menu_config(db_session)


def test_case_menu_item_can_be_removed_from_create_select(authenticated_client, db_session) -> None:
    config = default_case_menu_config()
    incoming = next(section for section in config["sections"] if section["id"] == "incoming")
    incoming["items"] = [
        item for item in incoming["items"] if item.get("type") != "TRADEMARK"
    ]

    try:
        _set_case_menu_config(db_session, config)

        resp = authenticated_client.get("/case/matter/create/select")
        assert resp.status_code == 200
        soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
        incoming_card = next(
            card
            for card in soup.select(".card")
            if "Inbound US Matters" in card.get_text(" ", strip=True)
        )

        assert "Patent" in incoming_card.get_text(" ", strip=True)
        assert "Trademark" not in incoming_card.get_text(" ", strip=True)
    finally:
        _clear_case_menu_config(db_session)
