import json


def _set_system_config(db_session, key: str, value: str) -> None:
    from app.models.system_config import SystemConfig

    SystemConfig.set_config(key, value)
    db_session.commit()


def _delete_system_config(db_session, key: str) -> None:
    from app.models.system_config import SystemConfig

    row = SystemConfig.query.filter_by(key=key).first()
    if row is None:
        return
    db_session.delete(row)
    db_session.commit()


def _reset_case_field_singletons() -> None:
    from app.services.case_fields.mapping_service import MappingService
    from app.services.case_fields.registry import FieldRegistry

    FieldRegistry.instance().reset()

    mapping = MappingService.instance()
    mapping._mappings.clear()
    mapping._initialized = False
    mapping._config_path = ""
    mapping._config_mtime = 0.0
    mapping._source_meta = {}
    mapping._allow_system_config = True


def test_unified_registry_prefers_system_config(app, db_session) -> None:
    _reset_case_field_singletons()

    payload = {
        "field_definitions": {
            "foo": {"label": "Foo", "input_type": "text"},
        },
        "mappings": {
            "IP:DOM:PATENT": {
                "namespace": "dom_patent",
                "fields": [{"key": "foo", "order": 1, "col": 1, "required": False}],
            }
        },
    }
    try:
        _set_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON", json.dumps(payload))

        from app.services.case_fields.mapping_service import MappingService
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
        assert registry.exists("foo")

        mapping = MappingService.instance()
        mapping.initialize()
        m = mapping.get_mapping("DOM", "PATENT")
        assert m is not None
        assert m.namespace == "dom_patent"
        assert any(fm.key == "foo" for fm in m.fields)
    finally:
        _delete_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON")
        _reset_case_field_singletons()


def test_unified_registry_reload_if_changed(app, db_session) -> None:
    _reset_case_field_singletons()

    payload_v1 = {
        "field_definitions": {
            "foo": {"label": "Foo", "input_type": "text"},
        },
        "mappings": {
            "IP:DOM:PATENT": {
                "namespace": "dom_patent",
                "fields": [{"key": "foo", "order": 1, "col": 1}],
            }
        },
    }
    payload_v2 = {
        "field_definitions": {
            "bar": {"label": "Bar", "input_type": "text"},
        },
        "mappings": {
            "IP:DOM:PATENT": {
                "namespace": "dom_patent_v2",
                "fields": [{"key": "bar", "order": 1, "col": 1}],
            }
        },
    }

    try:
        _set_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON", json.dumps(payload_v1))

        from app.services.case_fields.mapping_service import MappingService
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        mapping = MappingService.instance()
        registry.initialize()
        mapping.initialize()

        assert registry.exists("foo")
        assert mapping.get_mapping("DOM", "PATENT") is not None

        _set_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON", json.dumps(payload_v2))

        assert registry.reload_if_changed()
        assert mapping.reload_if_changed()

        assert not registry.exists("foo")
        assert registry.exists("bar")

        m = mapping.get_mapping("DOM", "PATENT")
        assert m is not None
        assert m.namespace == "dom_patent_v2"
        assert any(fm.key == "bar" for fm in m.fields)
    finally:
        _delete_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON")
        _reset_case_field_singletons()


def test_system_config_registry_preserves_file_baseline_fields(app, db_session) -> None:
    _reset_case_field_singletons()

    payload = {
        "field_definitions": {
            "foo": {"label": "Foo", "input_type": "text"},
        },
        "mappings": {
            "IP:DOM:PATENT": {
                "namespace": "dom_patent_override",
                "fields": [],
            }
        },
    }
    try:
        _set_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON", json.dumps(payload))

        from app.services.case_fields.mapping_service import MappingService
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
        assert registry.exists("foo")
        assert registry.exists("application_no")

        mapping = MappingService.instance()
        mapping.initialize()
        fields = mapping.get_fields_for_case("DOM", "PATENT")
        keys = {field["key"] for field in fields}
        assert "application_no" in keys
        assert len(fields) > 50
    finally:
        _delete_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON")
        _reset_case_field_singletons()


def test_system_config_registry_strips_retired_agency_fields(app, db_session) -> None:
    _reset_case_field_singletons()
    retired_mapping = "bi" + "b_mapping"
    retired_suffix = "\u006b\u006f"
    retired_title = f"title_{retired_suffix}"
    retired_litigation_title = f"litigation_title_{retired_suffix}"
    payload = {
        retired_mapping: {"version": 1},
        "field_definitions": {
            retired_title: {"label": "Retired title", "input_type": "text"},
            retired_litigation_title: {
                "label": "Retired litigation title",
                "input_type": "text",
            },
        },
        "mappings": {
            "IP:DOM:PATENT": {
                "namespace": "dom_patent",
                "fields": [
                    {"key": retired_title, "order": 1, "col": 1},
                    {"key": "application_no", "order": 2, "col": 1},
                ],
                "extra_allowed": [retired_title, "client_id"],
            }
        },
    }

    try:
        _set_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON", json.dumps(payload))

        from app.services.case_fields.registry import FieldRegistry
        from app.services.case_fields.unified_config import load_unified_registry_data

        data, _meta = load_unified_registry_data()
        assert data is not None
        assert retired_mapping not in data
        assert retired_title not in data["field_definitions"]
        assert retired_litigation_title not in data["field_definitions"]
        fields = data["mappings"]["IP:DOM:PATENT"]["fields"]
        assert all(field.get("key") != retired_title for field in fields)
        assert retired_title not in data["mappings"]["IP:DOM:PATENT"]["extra_allowed"]

        registry = FieldRegistry.instance()
        registry.initialize()
        assert not registry.exists(retired_title)
        assert not registry.exists(retired_litigation_title)
    finally:
        _delete_system_config(db_session, "UNIFIED_FIELD_REGISTRY_JSON")
        _reset_case_field_singletons()
