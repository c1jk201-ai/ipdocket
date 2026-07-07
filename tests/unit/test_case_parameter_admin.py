from __future__ import annotations

import json
import re

from app.models.system_config import SystemConfig
from app.services.case.case_menu_config import CASE_MENU_CONFIG_KEY, default_case_menu_config
from app.services.case_fields.unified_config import UNIFIED_FIELD_REGISTRY_KEY
from app.services.core.config_service import ConfigService


def _clear_case_menu_config(db_session) -> None:
    row = SystemConfig.query.filter_by(key=CASE_MENU_CONFIG_KEY).first()
    if row:
        db_session.delete(row)
        db_session.commit()
    ConfigService.clear_cache()
    from app.services.case.case_parameter_service import CaseParameterService

    CaseParameterService.reload_if_changed()


def _clear_registry_override(db_session) -> None:
    row = SystemConfig.query.filter_by(key=UNIFIED_FIELD_REGISTRY_KEY).first()
    if row:
        db_session.delete(row)
        db_session.commit()
    ConfigService.clear_cache()
    from app.services.case_fields.mapping_service import MappingService
    from app.services.case_fields.registry import FieldRegistry

    FieldRegistry.instance().reset()
    mapping = MappingService.instance()
    mapping._mappings.clear()
    mapping._initialized = False
    mapping._source_meta = {}


def test_case_parameter_admin_page_renders(admin_client, db_session) -> None:
    try:
        _clear_registry_override(db_session)
        resp = admin_client.get("/admin/case-parameters")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Matter Parameters" in body
        assert "Field Definitions" in body
        assert "Matter Mappings" in body
        assert "UNIFIED_FIELD_REGISTRY_JSON" in body
        assert "Load effective fields" in body
        assert "mapping-group-options" in body
        assert "Group order" in body
        assert "field-input-preset" in body
        assert "field-options" in body
        assert "field-input-preview" in body
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_snapshot_includes_baseline_preview_targets(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)

        response = admin_client.get("/admin/api/case-parameters")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["snapshot"]["baseline"]["field_count"] > 50
        assert data["snapshot"]["baseline"]["mapping_count"] > 10

        mappings = {item["key"]: item for item in data["snapshot"]["mappings"]}
        assert mappings["IP:DOM:PATENT"]["baseline"] is True
        assert mappings["IP:DOM:PATENT"]["create_preview"]["target"] == {
            "division": "DOM",
            "case_type": "PATENT",
        }
        assert mappings["IP:MISC"]["create_preview"]["target"] == {
            "division": "ETC",
            "case_type": "MISC",
        }
        dom_patent_preview = {
            item["key"]: item for item in mappings["IP:DOM:PATENT"]["create_preview"]["fields"]
        }
        assert dom_patent_preview["client_mgmt_no"]["label"] == "Client management No."
        assert dom_patent_preview["client_mgmt_no"]["group"] == "Default/Responsible"
        assert dom_patent_preview["client_mgmt_no"]["group_order"] == 10
        assert dom_patent_preview["filing_type"]["label"] == "Filing type"
        assert dom_patent_preview["applicant_name"]["label"] == "Applicant"
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_create_layout_coerces_placeholder_labels(db_session) -> None:
    try:
        _clear_registry_override(db_session)

        from app.services.case.case_parameter_service import CaseParameterService

        layout, meta = CaseParameterService.get_field_layout_with_meta("DOM", "PATENT")
        labels = {
            cell[1]: cell[0].replace(" *", "")
            for row in layout
            for cell in row
            if cell[1] and cell[1] != "__blank__"
        }

        assert labels["client_mgmt_no"] == "Client management No."
        assert labels["filing_type"] == "Filing type"
        assert labels["manager"] == "Docketing owner"
        assert labels["attorney"] == "Responsible attorney"
        assert labels["applicant_name"] == "Applicant"
        assert labels["filing_deadline"] == "Filing deadline"
        assert meta["novelty_doc_deadline"]["input_type"] == "date"
        assert meta["application_no"]["label"] == "Application No."
    finally:
        _clear_registry_override(db_session)


def test_dom_patent_create_renders_novelty_doc_deadline_calendar_input(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        _clear_case_menu_config(db_session)

        response = admin_client.get("/case/matter/create?division=DOM&type=PATENT")
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        assert re.search(
            r'<input[^>]+type="date"[^>]+name="novelty_doc_deadline"',
            body,
            re.S,
        )
        assert "vendor/flatpickr/flatpickr.min.js" in body
    finally:
        _clear_case_menu_config(db_session)
        _clear_registry_override(db_session)


def test_case_parameter_admin_updates_field_and_mapping(admin_client, db_session) -> None:
    try:
        _clear_registry_override(db_session)

        field_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_test_param",
                "label": "Admin Test Param",
                "input_type": "text",
                "serializer": "string",
                "options_source": "",
                "help_text": "editable through admin",
                "deprecated": False,
                "validators": '[{"type":"max_length","value":30}]',
            },
        )
        assert field_resp.status_code == 200
        field_data = field_resp.get_json()
        assert field_data["success"] is True
        assert any(field["key"] == "admin_test_param" for field in field_data["snapshot"]["fields"])

        mapping_resp = admin_client.post(
            "/admin/api/case-parameters/mapping",
            json={
                "key": "IP:DOM:ADMIN_TEST",
                "namespace": "admin_test",
                "inherit": "",
                "extra_allowed": [],
                "fields": [
                    {
                        "key": "admin_test_param",
                        "order": 1,
                        "col": 1,
                        "required": True,
                        "group": "Default/Responsible",
                        "group_order": 10,
                    }
                ],
            },
        )
        assert mapping_resp.status_code == 200
        mapping_data = mapping_resp.get_json()
        assert mapping_data["success"] is True
        assert any(
            mapping["key"] == "IP:DOM:ADMIN_TEST"
            for mapping in mapping_data["snapshot"]["mappings"]
        )
        saved_mapping_snapshot = next(
            mapping
            for mapping in mapping_data["snapshot"]["mappings"]
            if mapping["key"] == "IP:DOM:ADMIN_TEST"
        )
        assert saved_mapping_snapshot["create_preview"]["fields"][0]["group"] == "Default/Responsible"
        assert saved_mapping_snapshot["create_preview"]["fields"][0]["group_order"] == 10

        row = SystemConfig.query.filter_by(key=UNIFIED_FIELD_REGISTRY_KEY).one()
        saved = json.loads(row.value)
        assert saved["field_definitions"]["admin_test_param"]["label"] == "Admin Test Param"
        assert saved["mappings"]["IP:DOM:ADMIN_TEST"]["fields"][0]["required"] is True
        assert saved["mappings"]["IP:DOM:ADMIN_TEST"]["fields"][0]["group"] == "Default/Responsible"
        assert saved["mappings"]["IP:DOM:ADMIN_TEST"]["fields"][0]["group_order"] == 10

        from app.services.case.case_parameter_service import CaseParameterService

        layout, meta = CaseParameterService.get_field_layout_with_meta("DOM", "ADMIN_TEST")
        assert layout[0][0][3] == "Default/Responsible"
        assert layout[0][0][4] == 10
        assert meta["admin_test_param"]["group"] == "Default/Responsible"
        assert meta["admin_test_param"]["group_order"] == 10
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_supports_date_and_inline_select_options(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)

        date_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_test_date",
                "label": "Admin Test Date",
                "input_type": "date",
                "serializer": "",
                "options_source": "",
                "options": "",
                "validators": "[]",
            },
        )
        assert date_resp.status_code == 200

        select_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_test_status",
                "label": "Admin Test Status",
                "input_type": "select",
                "serializer": "string",
                "options_source": "",
                "options": "PENDING|Pending\nDONE|Done",
                "validators": "[]",
            },
        )
        assert select_resp.status_code == 200

        mapping_resp = admin_client.post(
            "/admin/api/case-parameters/mapping",
            json={
                "key": "IP:DOM:ADMIN_SELECT_TEST",
                "namespace": "admin_select_test",
                "inherit": "",
                "extra_allowed": [],
                "fields": [
                    {
                        "key": "admin_test_date",
                        "order": 1,
                        "col": 1,
                        "required": False,
                        "group": "Test",
                    },
                    {
                        "key": "admin_test_status",
                        "order": 1,
                        "col": 2,
                        "required": True,
                        "group": "Test",
                    },
                ],
            },
        )
        assert mapping_resp.status_code == 200
        data = mapping_resp.get_json()
        fields = {field["key"]: field for field in data["snapshot"]["fields"]}
        assert fields["admin_test_date"]["serializer"] == "date"
        assert fields["admin_test_date"]["validators"] == [{"type": "date_format"}]
        assert fields["admin_test_date"]["options"] == []
        assert fields["admin_test_status"]["options"] == [
            {"value": "PENDING", "label": "Pending"},
            {"value": "DONE", "label": "Done"},
        ]

        from app.services.case.case_parameter_service import CaseParameterService

        layout, meta = CaseParameterService.get_field_layout_with_meta(
            "DOM", "ADMIN_SELECT_TEST"
        )
        assert layout[0][0][2] == "date"
        assert layout[0][1][2] == "select"
        assert meta["admin_test_status"]["options"] == [
            {"value": "PENDING", "label": "Pending"},
            {"value": "DONE", "label": "Done"},
        ]

        from app.services.case.form_support import validate_custom_field_updates

        accepted = validate_custom_field_updates(
            matter_id="",
            namespace="admin_select_test",
            form_data={"admin_test_status": "DONE"},
            allowed_keys=["admin_test_status"],
            strict_dates=True,
        )
        assert accepted["admin_test_status"] == "DONE"

        import pytest

        with pytest.raises(ValueError, match="Invalid option"):
            validate_custom_field_updates(
                matter_id="",
                namespace="admin_select_test",
                form_data={"admin_test_status": "BROKEN"},
                allowed_keys=["admin_test_status"],
                strict_dates=True,
            )
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_clears_stale_options_when_field_is_not_select(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)

        select_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_test_stale_options",
                "label": "Admin Test Stale Options",
                "input_type": "select",
                "serializer": "string",
                "options": "A|Alpha\nB|Beta",
                "validators": "[]",
            },
        )
        assert select_resp.status_code == 200

        text_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "current_key": "admin_test_stale_options",
                "key": "admin_test_stale_options",
                "label": "Admin Test Stale Options",
                "input_type": "text",
                "serializer": "string",
                "options": "A|Alpha\nB|Beta",
                "validators": "[]",
            },
        )
        assert text_resp.status_code == 200
        data = text_resp.get_json()
        field = next(
            item
            for item in data["snapshot"]["fields"]
            if item["key"] == "admin_test_stale_options"
        )
        assert field["input_type"] == "text"
        assert field["options"] == []

        from app.services.case.form_support import validate_custom_field_updates

        accepted = validate_custom_field_updates(
            matter_id="",
            namespace="admin_test",
            form_data={"admin_test_stale_options": "outside-select-list"},
            allowed_keys=["admin_test_stale_options"],
            strict_dates=True,
        )
        assert accepted["admin_test_stale_options"] == "outside-select-list"
    finally:
        _clear_registry_override(db_session)


def test_matter_create_menu_custom_inline_select_renders_in_create_form(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        _clear_case_menu_config(db_session)

        field_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_test_status",
                "label": "Admin Test Status",
                "input_type": "select",
                "serializer": "string",
                "options": "PENDING|Pending\nDONE|Done",
                "validators": "[]",
            },
        )
        assert field_resp.status_code == 200

        payload = default_case_menu_config()
        payload["sections"] = [
            {
                "id": "admin-test",
                "label": "Admin Test",
                "division": "DOM",
                "order": 10,
                "items": [
                    {
                        "id": "dom-admin-select-test",
                        "label": "Admin Select Test",
                        "division": "DOM",
                        "type": "ADMIN_SELECT_TEST",
                        "profile_division": "DOM",
                        "profile_type": "PATENT",
                        "namespace": "admin_select_test",
                        "fields": [
                            {
                                "key": "admin_test_status",
                                "order": 10,
                                "col": 1,
                                "required": True,
                                "group": "Test",
                            }
                        ],
                    }
                ],
            }
        ]

        menu_resp = admin_client.post("/admin/api/matter-create-menu", json={"value": payload})
        assert menu_resp.status_code == 200

        create_resp = admin_client.get(
            "/case/matter/create?division=DOM&type=ADMIN_SELECT_TEST"
        )
        assert create_resp.status_code == 200
        body = create_resp.get_data(as_text=True)
        assert 'name="admin_test_status"' in body
        assert '<select' in body
        assert 'value="PENDING"' in body
        assert ">Pending</option>" in body
        assert 'value="DONE"' in body
        assert ">Done</option>" in body
    finally:
        _clear_case_menu_config(db_session)
        _clear_registry_override(db_session)


def test_matter_create_menu_deadline_suffix_renders_date_control(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        _clear_case_menu_config(db_session)

        field_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_text_deadline",
                "label": "Admin Text Deadline",
                "input_type": "text",
                "serializer": "string",
                "validators": "[]",
            },
        )
        assert field_resp.status_code == 200

        payload = default_case_menu_config()
        payload["sections"] = [
            {
                "id": "admin-deadline",
                "label": "Admin Deadline",
                "division": "DOM",
                "order": 10,
                "items": [
                    {
                        "id": "dom-admin-deadline-test",
                        "label": "Admin Deadline Test",
                        "division": "DOM",
                        "type": "ADMIN_DEADLINE_TEST",
                        "profile_division": "DOM",
                        "profile_type": "PATENT",
                        "namespace": "admin_deadline_test",
                        "fields": [
                            {
                                "key": "admin_text_deadline",
                                "order": 10,
                                "col": 1,
                                "group": "Test",
                            }
                        ],
                    }
                ],
            }
        ]
        menu_resp = admin_client.post("/admin/api/matter-create-menu", json={"value": payload})
        assert menu_resp.status_code == 200

        from app.services.case.case_parameter_service import CaseParameterService

        layout, meta = CaseParameterService.get_field_layout_with_meta(
            "DOM", "ADMIN_DEADLINE_TEST"
        )
        assert layout[0][0][2] == "date"
        assert meta["admin_text_deadline"]["input_type"] == "date"

        create_resp = admin_client.get(
            "/case/matter/create?division=DOM&type=ADMIN_DEADLINE_TEST"
        )
        assert create_resp.status_code == 200
        body = create_resp.get_data(as_text=True)
        assert re.search(
            r'<input[^>]+type="date"[^>]+name="admin_text_deadline"',
            body,
            re.S,
        )
        assert "vendor/flatpickr/flatpickr.min.css" in body
        assert "vendor/flatpickr/flatpickr.min.js" in body
    finally:
        _clear_case_menu_config(db_session)
        _clear_registry_override(db_session)


def test_matter_create_menu_parameter_input_types_render_and_validate(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        _clear_case_menu_config(db_session)

        field_payloads = [
            {
                "key": "admin_test_due",
                "label": "Admin Test Due",
                "input_type": "date",
                "serializer": "",
                "default_value": "2026-07-06",
                "validators": "[]",
            },
            {
                "key": "admin_test_count",
                "label": "Admin Test Count",
                "input_type": "number",
                "serializer": "",
                "default_value": "3",
                "validators": "[]",
            },
            {
                "key": "admin_test_flag",
                "label": "Admin Test Flag",
                "input_type": "select_yn",
                "serializer": "",
                "default_value": "Yes",
                "validators": "[]",
            },
            {
                "key": "admin_test_notes",
                "label": "Admin Test Notes",
                "input_type": "textarea",
                "serializer": "",
                "default_value": "Default memo",
                "validators": "[]",
            },
            {
                "key": "admin_test_contact",
                "label": "Admin Test Contact",
                "input_type": "client_search",
                "serializer": "",
                "validators": "[]",
            },
            {
                "key": "admin_test_phase",
                "label": "Admin Test Phase",
                "input_type": "select",
                "serializer": "",
                "options": "TODO|To do\nDONE|Done",
                "default_value": "DONE",
                "validators": "[]",
            },
        ]
        for payload in field_payloads:
            response = admin_client.post("/admin/api/case-parameters/field", json=payload)
            assert response.status_code == 200

        payload = default_case_menu_config()
        payload["sections"] = [
            {
                "id": "admin-types",
                "label": "Admin Types",
                "division": "DOM",
                "order": 10,
                "items": [
                    {
                        "id": "dom-admin-types-test",
                        "label": "Admin Types Test",
                        "division": "DOM",
                        "type": "ADMIN_TYPES_TEST",
                        "profile_division": "DOM",
                        "profile_type": "PATENT",
                        "namespace": "admin_types_test",
                        "fields": [
                            {"key": "admin_test_due", "order": 10, "col": 1, "group": "Test"},
                            {"key": "admin_test_count", "order": 10, "col": 2, "group": "Test"},
                            {"key": "admin_test_flag", "order": 20, "col": 1, "group": "Test"},
                            {"key": "admin_test_notes", "order": 20, "col": 2, "group": "Test"},
                            {"key": "admin_test_contact", "order": 30, "col": 1, "group": "Test"},
                            {"key": "admin_test_phase", "order": 30, "col": 2, "group": "Test"},
                        ],
                    }
                ],
            }
        ]
        menu_resp = admin_client.post("/admin/api/matter-create-menu", json={"value": payload})
        assert menu_resp.status_code == 200

        create_resp = admin_client.get("/case/matter/create?division=DOM&type=ADMIN_TYPES_TEST")
        assert create_resp.status_code == 200
        body = create_resp.get_data(as_text=True)

        assert re.search(
            r'<input[^>]+type="date"[^>]+name="admin_test_due"[^>]+value="2026-07-06"',
            body,
            re.S,
        )
        assert re.search(
            r'<input[^>]+type="number"[^>]+name="admin_test_count"[^>]+value="3"',
            body,
            re.S,
        )
        assert 'type="radio" name="admin_test_flag"' in body
        assert re.search(
            r'<textarea[^>]+name="admin_test_notes"[^>]*>\s*Default memo\s*</textarea>',
            body,
            re.S,
        )
        assert 'name="admin_test_contact"' in body
        assert 'data-client-search="1"' in body
        assert 'name="admin_test_phase"' in body
        assert 'value="DONE" selected' in body

        from app.services.case.form_support import validate_custom_field_updates

        accepted = validate_custom_field_updates(
            matter_id="",
            namespace="admin_types_test",
            form_data={
                "admin_test_count": "007",
                "admin_test_flag": "No",
                "admin_test_phase": "TODO",
            },
            allowed_keys=["admin_test_count", "admin_test_flag", "admin_test_phase"],
        )
        assert accepted["admin_test_count"] == "7"
        assert accepted["admin_test_flag"] == "No"
        assert accepted["admin_test_phase"] == "TODO"

        import pytest

        with pytest.raises(ValueError, match="Invalid number"):
            validate_custom_field_updates(
                matter_id="",
                namespace="admin_types_test",
                form_data={"admin_test_count": "x"},
                allowed_keys=["admin_test_count"],
            )
        with pytest.raises(ValueError, match="Invalid yes/no"):
            validate_custom_field_updates(
                matter_id="",
                namespace="admin_types_test",
                form_data={"admin_test_flag": "maybe"},
                allowed_keys=["admin_test_flag"],
            )
    finally:
        _clear_case_menu_config(db_session)
        _clear_registry_override(db_session)


def test_case_parameter_admin_tracks_matter_create_menu_usage(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        _clear_case_menu_config(db_session)

        field_resp = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_menu_only",
                "label": "Admin Menu Only",
                "input_type": "text",
                "serializer": "string",
                "validators": "[]",
            },
        )
        assert field_resp.status_code == 200

        payload = default_case_menu_config()
        payload["sections"] = [
            {
                "id": "admin-menu-usage",
                "label": "Admin Menu Usage",
                "division": "DOM",
                "order": 10,
                "items": [
                    {
                        "id": "dom-admin-menu-only",
                        "label": "Admin Menu Only",
                        "division": "DOM",
                        "type": "ADMIN_MENU_ONLY",
                        "profile_division": "DOM",
                        "profile_type": "PATENT",
                        "namespace": "admin_menu_only",
                        "fields": [
                            {
                                "key": "admin_menu_only",
                                "order": 10,
                                "col": 1,
                                "group": "Test",
                            }
                        ],
                    }
                ],
            }
        ]
        menu_resp = admin_client.post("/admin/api/matter-create-menu", json={"value": payload})
        assert menu_resp.status_code == 200

        response = admin_client.get("/admin/api/case-parameters")
        assert response.status_code == 200
        data = response.get_json()
        fields = {field["key"]: field for field in data["snapshot"]["fields"]}
        assert fields["admin_menu_only"]["usage_count"] == 1
        assert fields["admin_menu_only"]["menu_usage_count"] == 1
        assert fields["admin_menu_only"]["registry_usage_count"] == 0
        assert data["snapshot"]["case_menu"]["mapping_count"] == 1

        mappings = {mapping["key"]: mapping for mapping in data["snapshot"]["mappings"]}
        menu_mapping = mappings["IP:DOM:ADMIN_MENU_ONLY"]
        assert menu_mapping["source"] == "create_menu"
        assert menu_mapping["menu_override"] is True
        assert menu_mapping["read_only"] is True
        assert menu_mapping["direct_field_count"] == 1
        assert menu_mapping["create_preview"]["fields"][0]["key"] == "admin_menu_only"

        page_resp = admin_client.get("/admin/case-parameters")
        assert page_resp.status_code == 200
        body = page_resp.get_data(as_text=True)
        assert "param-menu-mapping-count" in body
        assert "This layout is controlled by" in body

        delete_resp = admin_client.delete("/admin/api/case-parameters/field/admin_menu_only")
        assert delete_resp.status_code == 400
        assert "Matter Create Menu" in delete_resp.get_json()["error"]
    finally:
        _clear_case_menu_config(db_session)
        _clear_registry_override(db_session)


def test_case_parameter_admin_rejects_invalid_defaults(admin_client, db_session) -> None:
    try:
        _clear_registry_override(db_session)

        bad_date = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_bad_date_default",
                "label": "Bad Date Default",
                "input_type": "date",
                "default_value": "20260706",
                "validators": "[]",
            },
        )
        assert bad_date.status_code == 400

        bad_select = admin_client.post(
            "/admin/api/case-parameters/field",
            json={
                "key": "admin_bad_select_default",
                "label": "Bad Select Default",
                "input_type": "select",
                "options": "A|Alpha",
                "default_value": "B",
                "validators": "[]",
            },
        )
        assert bad_select.status_code == 400
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_preserves_baseline_mapping_fields(admin_client, db_session) -> None:
    try:
        _clear_registry_override(db_session)

        response = admin_client.post(
            "/admin/api/case-parameters/mapping",
            json={
                "key": "IP:DOM:PATENT",
                "namespace": "domestic_patent",
                "inherit": "",
                "extra_allowed": [],
                "fields": [],
            },
        )
        assert response.status_code == 200
        data = response.get_json()
        mapping = next(
            item for item in data["snapshot"]["mappings"] if item["key"] == "IP:DOM:PATENT"
        )
        assert mapping["baseline"] is True
        assert mapping["effective_field_count"] > 50

        row = SystemConfig.query.filter_by(key=UNIFIED_FIELD_REGISTRY_KEY).one()
        saved = json.loads(row.value)
        assert len(saved["mappings"]["IP:DOM:PATENT"]["fields"]) > 50
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_repair_baseline_endpoint_persists_missing_baseline(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)
        SystemConfig.set_config(
            UNIFIED_FIELD_REGISTRY_KEY,
            json.dumps({"field_definitions": {}, "mappings": {}}),
        )
        db_session.commit()
        ConfigService.clear_cache()

        response = admin_client.post("/admin/api/case-parameters/repair-baseline", json={})
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True

        row = SystemConfig.query.filter_by(key=UNIFIED_FIELD_REGISTRY_KEY).one()
        saved = json.loads(row.value)
        assert "application_no" in saved["field_definitions"]
        assert "IP:DOM:PATENT" in saved["mappings"]
        assert len(saved["mappings"]["IP:DOM:PATENT"]["fields"]) > 50
    finally:
        _clear_registry_override(db_session)


def test_case_parameter_admin_deprecates_baseline_field_instead_of_deleting(
    admin_client, db_session
) -> None:
    try:
        _clear_registry_override(db_session)

        response = admin_client.delete("/admin/api/case-parameters/field/application_no")
        assert response.status_code == 200
        data = response.get_json()
        field = next(item for item in data["snapshot"]["fields"] if item["key"] == "application_no")
        assert field["baseline"] is True
        assert field["deprecated"] is True

        row = SystemConfig.query.filter_by(key=UNIFIED_FIELD_REGISTRY_KEY).one()
        saved = json.loads(row.value)
        assert "application_no" in saved["field_definitions"]
        assert saved["field_definitions"]["application_no"]["deprecated"] is True
    finally:
        _clear_registry_override(db_session)
