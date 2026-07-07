from __future__ import annotations

import copy
import json
import re
from typing import Any

from app.extensions import db
from app.models.system_config import SystemConfig
from app.services.case_fields.field_types import INPUT_TYPES, SERIALIZERS
from app.services.case_fields.grouping import apply_default_field_groups_for_mapping_key
from app.services.case_fields.labels import coerce_field_label
from app.services.case_fields.unified_config import (
    UNIFIED_FIELD_REGISTRY_KEY,
    load_unified_registry_data,
)
from app.services.core.config_service import ConfigService

_FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CREATE_SELECT_WIDGETS = {
    "select_filing_type",
    "select_invention_grade",
    "select_department",
    "select_stand_reason",
    "select_design_filing_type",
    "select_design_filing_kind",
    "select_tm_filing_type",
    "select_tm_right_type",
    "select_tm_type",
    "select_tm_registration_payment_term",
    "select_litigation_right_type",
    "select_litigation_case",
    "select_litigation_court",
    "select_litigation_result",
    "select_deadline_type",
}

_INPUT_TYPE_PRESETS = [
    {
        "value": "text",
        "label": "Text",
        "input_type": "text",
        "serializer": "string",
        "validators": [],
    },
    {
        "value": "date",
        "label": "Date",
        "input_type": "date",
        "serializer": "date",
        "validators": [{"type": "date_format"}],
    },
    {
        "value": "textarea",
        "label": "Long text",
        "input_type": "textarea",
        "serializer": "string",
        "validators": [],
    },
    {
        "value": "select",
        "label": "Single select",
        "input_type": "select",
        "serializer": "string",
        "validators": [],
    },
    {
        "value": "select_yn",
        "label": "Yes / No",
        "input_type": "select_yn",
        "serializer": "bool",
        "validators": [],
    },
    {
        "value": "client_search",
        "label": "Contact search",
        "input_type": "client_search",
        "serializer": "string",
        "validators": [],
    },
    {
        "value": "number",
        "label": "Number",
        "input_type": "number",
        "serializer": "int",
        "validators": [],
    },
]


def _ensure_registry_shape(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    out = copy.deepcopy(data)
    if not isinstance(out.get("field_definitions"), dict):
        out["field_definitions"] = {}
    if not isinstance(out.get("mappings"), dict):
        out["mappings"] = {}
    return out


def load_parameter_registry_document() -> tuple[dict[str, Any], dict[str, Any]]:
    data, meta = load_unified_registry_data()
    return _ensure_registry_shape(data), dict(meta or {})


def _baseline_registry_document() -> dict[str, Any]:
    data, _meta = load_unified_registry_data(allow_system_config=False)
    return _ensure_registry_shape(data)


def _baseline_field_keys() -> set[str]:
    return set((_baseline_registry_document().get("field_definitions") or {}).keys())


def _baseline_mapping_keys() -> set[str]:
    return set((_baseline_registry_document().get("mappings") or {}).keys())


def _baseline_mapping(key: str) -> dict[str, Any]:
    mapping = (_baseline_registry_document().get("mappings") or {}).get(key)
    return dict(mapping) if isinstance(mapping, dict) else {}


def _parse_validators(raw: Any) -> list[Any]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        validators = raw
    elif isinstance(raw, dict):
        validators = [raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"validators must be valid JSON: {exc.msg}") from exc
        validators = parsed if isinstance(parsed, list) else [parsed]
    else:
        raise ValueError("validators must be a JSON list")

    cleaned: list[Any] = []
    for item in validators:
        if isinstance(item, str):
            token = item.strip()
            if token:
                cleaned.append(token)
            continue
        if isinstance(item, dict):
            validator_type = str(item.get("type") or "").strip()
            if not validator_type:
                raise ValueError("validator.type is required")
            cleaned.append({k: v for k, v in item.items() if v not in (None, "")})
            continue
        raise ValueError("each validator must be a string or object")
    return cleaned


def _normalize_options(raw: Any) -> list[dict[str, str]]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"options must be valid JSON or one option per line: {exc.msg}") from exc
            raw_items = parsed
        else:
            raw_items = [line.strip() for line in text.splitlines() if line.strip()]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise ValueError("options must be a JSON array or line-separated text")

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        value = ""
        label = ""
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            label = str(item.get("label") or value).strip()
        elif isinstance(item, (list, tuple)) and item:
            value = str(item[0] or "").strip()
            label = str(item[1] if len(item) > 1 else item[0]).strip()
        else:
            text_item = str(item or "").strip()
            if "|" in text_item:
                value, label = [part.strip() for part in text_item.split("|", 1)]
            elif "\t" in text_item:
                value, label = [part.strip() for part in text_item.split("\t", 1)]
            else:
                value = text_item
                label = text_item
        if not value:
            raise ValueError("option value cannot be blank")
        if value in seen:
            raise ValueError(f"duplicate option value: {value}")
        options.append({"value": value, "label": label or value})
        seen.add(value)
    return options


def _validator_key(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("type") or "").strip()
    return ""


def _merge_validator_defaults(validators: list[Any], defaults: list[Any]) -> list[Any]:
    out = list(validators)
    present = {_validator_key(item) for item in out if _validator_key(item)}
    for item in defaults:
        key = _validator_key(item)
        if key and key not in present:
            out.append(item)
            present.add(key)
    return out


def _is_bool_token(value: str) -> bool:
    return value.strip().lower() in {
        "y",
        "yes",
        "true",
        "1",
        "on",
        "t",
        "n",
        "no",
        "false",
        "0",
        "off",
        "f",
    }


def _validate_default_value(
    *,
    input_type: str,
    default_value: Any,
    options: list[dict[str, str]],
) -> None:
    if default_value in (None, ""):
        return
    value = str(default_value).strip()
    if not value:
        return
    if input_type == "date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError("date default_value must use YYYY-MM-DD")
    if input_type == "number" and not re.match(r"^[+-]?\d+(\.\d+)?$", value):
        raise ValueError("number default_value must be numeric")
    if input_type == "select_yn" and not _is_bool_token(value):
        raise ValueError("Yes/No default_value must be yes or no")
    if input_type == "select" and options:
        values = {str(option.get("value") or "").strip() for option in options}
        if value not in values:
            raise ValueError("select default_value must match one of the option values")


def _preset_for_input_type(input_type: str) -> dict[str, Any]:
    input_type = str(input_type or "").strip()
    for item in _INPUT_TYPE_PRESETS:
        if item["input_type"] == input_type:
            return dict(item)
    return {}


def _registry_field_usage_counts(data: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mapping in (data.get("mappings") or {}).values():
        if not isinstance(mapping, dict):
            continue
        for field in mapping.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _case_menu_field_usage_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        from app.services.case.case_menu_config import get_case_menu_mapping_overrides

        overrides = get_case_menu_mapping_overrides()
    except Exception:
        return counts

    for override in overrides:
        if not isinstance(override, dict):
            continue
        for field in override.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = str(field.get("key") or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
        for value in override.get("extra_allowed") or []:
            key = str(value or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _field_usage_counts(data: dict[str, Any]) -> dict[str, int]:
    counts = _registry_field_usage_counts(data)
    for key, value in _case_menu_field_usage_counts().items():
        counts[key] = counts.get(key, 0) + value
    return counts


def _normalize_field_definition(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = str(key or "").strip()
    if not key or not _FIELD_KEY_RE.match(key):
        raise ValueError("field key must use letters, numbers, and underscores")

    label = coerce_field_label(key, payload.get("label"))
    input_type = str(payload.get("input_type") or "").strip()
    if not label:
        raise ValueError("label is required")
    if not input_type:
        raise ValueError("input_type is required")

    preset = _preset_for_input_type(input_type)
    serializer = (
        str(payload.get("serializer") or "").strip()
        or str(preset.get("serializer") or "").strip()
        or "string"
    )
    options_source = str(payload.get("options_source") or "").strip() or None
    options = _normalize_options(payload.get("options")) if input_type == "select" else []
    help_text = str(payload.get("help_text") or "").strip()
    default_value = payload.get("default_value")
    if isinstance(default_value, str) and default_value.strip() == "":
        default_value = None
    validators = _parse_validators(payload.get("validators"))
    if input_type == "date":
        validators = _merge_validator_defaults(validators, [{"type": "date_format"}])
    _validate_default_value(
        input_type=input_type,
        default_value=default_value,
        options=options,
    )

    return {
        "key": key,
        "label": label,
        "input_type": input_type,
        "options_source": options_source,
        "options": options,
        "help_text": help_text,
        "deprecated": bool(payload.get("deprecated", False)),
        "serializer": serializer,
        "default_value": default_value,
        "validators": validators,
    }


def _normalize_mapping_fields(raw_fields: Any, definitions: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw_fields, list):
        raw_fields = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, field in enumerate(raw_fields):
        if not isinstance(field, dict):
            continue
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        if key != "__blank__" and key not in definitions:
            raise ValueError(f"unknown field key: {key}")
        if key in seen and key != "__blank__":
            raise ValueError(f"duplicate field key: {key}")
        seen.add(key)
        try:
            order = float(field.get("order", index + 1) or index + 1)
        except Exception:
            order = float(index + 1)
        try:
            col = int(field.get("col", 1) or 1)
        except Exception:
            col = 1
        if col not in (1, 2):
            col = 1
        rows.append(
            {
                "key": key,
                "order": order,
                "col": col,
                "required": bool(field.get("required", False)),
                "group": str(field.get("group") or field.get("section") or "").strip(),
                "group_order": field.get("group_order", field.get("section_order")),
            }
        )
    return sorted(rows, key=lambda item: (item["order"], item["col"], item["key"]))


def _merge_baseline_mapping_fields(key: str, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = _baseline_mapping(key)
    baseline_fields = baseline.get("fields") if isinstance(baseline.get("fields"), list) else []
    if not baseline_fields:
        return fields

    out = [dict(field) for field in fields]
    present = {str(field.get("key") or "") for field in out if isinstance(field, dict)}
    try:
        next_order = max(float(field.get("order") or 0) for field in out) + 1
    except ValueError:
        next_order = 1.0

    for field in baseline_fields:
        if not isinstance(field, dict):
            continue
        field_key = str(field.get("key") or "").strip()
        if not field_key or field_key in present:
            continue
        restored = dict(field)
        restored["order"] = next_order
        next_order += 1
        out.append(restored)
        present.add(field_key)
    return sorted(out, key=lambda item: (float(item.get("order") or 0), int(item.get("col") or 1)))


def _effective_mapping_fields(
    mappings: dict[str, Any],
    key: str,
    *,
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    seen = seen or set()
    if key in seen:
        return []
    mapping = mappings.get(key)
    if not isinstance(mapping, dict):
        return []

    own_fields = mapping.get("fields") if isinstance(mapping.get("fields"), list) else []
    inherit = str(mapping.get("inherit") or "").strip()
    if own_fields:
        return [dict(field) for field in own_fields if isinstance(field, dict)]
    if inherit:
        return _effective_mapping_fields(mappings, inherit, seen=seen | {key})
    return []


def _mapping_create_target(key: str) -> dict[str, str]:
    parts = [part for part in str(key or "").split(":") if part]
    if len(parts) >= 3 and parts[0] == "IP":
        return {"division": parts[1], "case_type": parts[2]}
    if len(parts) == 2 and parts[0] == "IP":
        return {"division": "ETC", "case_type": parts[1]}
    if len(parts) == 2:
        return {"division": parts[0], "case_type": parts[1]}
    if len(parts) == 1 and parts[0] in {"LITIGATION", "MISC"}:
        return {"division": "ETC", "case_type": parts[0]}
    return {"division": "", "case_type": ""}


def _create_widget_for(field_key: str, definition: dict[str, Any], mapping_key: str) -> str:
    input_type = str(definition.get("input_type") or "text").strip() or "text"
    options_source = str(definition.get("options_source") or "").strip()
    key = str(field_key or "").strip()
    upper_mapping = str(mapping_key or "").upper()

    if input_type == "select" and options_source:
        input_type = options_source
    if key == "filing_type":
        if "DESIGN" in upper_mapping:
            return "select_design_filing_type"
        if "TRADEMARK" in upper_mapping:
            return "select_tm_filing_type"
        return "select_filing_type"
    if key == "filing_kind" and "DESIGN" in upper_mapping:
        return "select_design_filing_kind"
    if key == "right_type":
        if "TRADEMARK" in upper_mapping:
            return "select_tm_right_type"
        if "LITIGATION" in upper_mapping or "MISC" in upper_mapping:
            return "select_litigation_right_type"
    return input_type


def _build_create_preview(
    *,
    mapping_key: str,
    mapping: dict[str, Any],
    effective_fields: list[dict[str, Any]],
    definitions: dict[str, Any],
) -> dict[str, Any]:
    target = _mapping_create_target(mapping_key)
    namespace = str(mapping.get("namespace") or "").strip()
    warnings: list[str] = []
    if not namespace:
        warnings.append("missing_namespace")
    if not effective_fields:
        warnings.append("no_effective_fields")

    rows = []
    unknown_fields: list[str] = []
    fallback_widgets: list[str] = []
    for field in effective_fields:
        field_key = str(field.get("key") or "").strip()
        definition = definitions.get(field_key)
        if not isinstance(definition, dict):
            unknown_fields.append(field_key)
            continue
        widget = _create_widget_for(field_key, definition, mapping_key)
        if (
            widget not in {"text", "number", "date", "textarea", "client_search", "blank", "select", "select_yn"}
            and widget not in _CREATE_SELECT_WIDGETS
        ):
            fallback_widgets.append(field_key)
        rows.append(
            {
                "key": field_key,
                "label": coerce_field_label(field_key, definition.get("label") or field_key),
                "widget": widget,
                "order": field.get("order"),
                "col": field.get("col"),
                "required": bool(field.get("required", False)),
                "group": str(field.get("group") or field.get("section") or "").strip(),
                "group_order": field.get("group_order", field.get("section_order")),
            }
        )
    if unknown_fields:
        warnings.append("unknown_fields:" + ",".join(sorted(set(unknown_fields))))
    if fallback_widgets:
        warnings.append("text_fallback_widgets:" + ",".join(sorted(set(fallback_widgets))))

    return {
        "target": target,
        "namespace": namespace,
        "field_count": len(rows),
        "required_count": sum(1 for row in rows if row["required"]),
        "warnings": warnings,
        "fields": rows,
    }


def _save_registry_document(data: dict[str, Any]) -> dict[str, Any]:
    normalized = _ensure_registry_shape(data)
    raw = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True)
    SystemConfig.set_config(UNIFIED_FIELD_REGISTRY_KEY, raw)
    db.session.commit()
    ConfigService.clear_cache()
    _reset_case_field_singletons()
    return normalized


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


def build_parameter_admin_snapshot() -> dict[str, Any]:
    data, meta = load_parameter_registry_document()
    baseline = _baseline_registry_document()
    baseline_fields = set((baseline.get("field_definitions") or {}).keys())
    baseline_mappings = set((baseline.get("mappings") or {}).keys())
    definitions = data.get("field_definitions") or {}
    mappings = data.get("mappings") or {}
    registry_usage = _registry_field_usage_counts(data)
    menu_usage = _case_menu_field_usage_counts()
    usage = dict(registry_usage)
    for key, value in menu_usage.items():
        usage[key] = usage.get(key, 0) + value

    input_types = set(INPUT_TYPES.keys())
    input_types.update(str(item.get("input_type") or "") for item in _INPUT_TYPE_PRESETS)
    serializer_options = set(SERIALIZERS.keys())
    option_sources: set[str] = set()
    validator_types: set[str] = {
        "required",
        "date_format",
        "application_number",
        "max_length",
        "regex",
    }

    fields = []
    for key, info in sorted(definitions.items(), key=lambda item: item[0].lower()):
        if not isinstance(info, dict):
            continue
        input_type = str(info.get("input_type") or "text")
        input_types.add(input_type)
        serializer = str(info.get("serializer") or "")
        if serializer:
            serializer_options.add(serializer)
        options_source = str(info.get("options_source") or "")
        if options_source:
            option_sources.add(options_source)
        options = _normalize_options(info.get("options"))
        for validator in info.get("validators") or []:
            if isinstance(validator, str):
                validator_types.add(validator)
            elif isinstance(validator, dict) and validator.get("type"):
                validator_types.add(str(validator.get("type")))
        fields.append(
            {
                "key": key,
                "label": coerce_field_label(key, info.get("label") or key),
                "input_type": input_type,
                "serializer": serializer,
                "options_source": options_source,
                "options": options,
                "options_json": json.dumps(options, ensure_ascii=False, indent=2),
                "options_text": "\n".join(
                    (
                        item["value"]
                        if item.get("label") == item.get("value")
                        else f"{item.get('value', '')}|{item.get('label', '')}"
                    )
                    for item in options
                ),
                "help_text": str(info.get("help_text") or ""),
                "deprecated": bool(info.get("deprecated", False)),
                "default_value": info.get("default_value"),
                "validators": info.get("validators") or [],
                "validators_json": json.dumps(
                    info.get("validators") or [], ensure_ascii=False, indent=2
                ),
                "usage_count": usage.get(key, 0),
                "registry_usage_count": registry_usage.get(key, 0),
                "menu_usage_count": menu_usage.get(key, 0),
                "baseline": key in baseline_fields,
            }
        )

    mapping_rows_by_key: dict[str, dict[str, Any]] = {}
    for key, info in sorted(mappings.items(), key=lambda item: item[0].lower()):
        if not isinstance(info, dict):
            continue
        raw_fields_for_mapping = info.get("fields") if isinstance(info.get("fields"), list) else []
        fields_for_mapping = apply_default_field_groups_for_mapping_key(
            [dict(field) for field in raw_fields_for_mapping if isinstance(field, dict)],
            key,
        )
        extra_allowed = (
            info.get("extra_allowed") if isinstance(info.get("extra_allowed"), list) else []
        )
        effective_fields = apply_default_field_groups_for_mapping_key(
            _effective_mapping_fields(mappings, key),
            key,
        )
        create_preview = _build_create_preview(
            mapping_key=key,
            mapping=info,
            effective_fields=effective_fields,
            definitions=definitions,
        )
        mapping_rows_by_key[key] = {
            "key": key,
            "namespace": str(info.get("namespace") or ""),
            "inherit": str(info.get("inherit") or ""),
            "field_count": len(effective_fields),
            "direct_field_count": len(fields_for_mapping),
            "effective_field_count": len(effective_fields),
            "fields": fields_for_mapping,
            "extra_allowed": extra_allowed,
            "baseline": key in baseline_mappings,
            "source": "registry",
            "menu_override": False,
            "read_only": False,
            "create_preview": create_preview,
        }

    menu_mapping_count = 0
    try:
        from app.services.case.case_menu_config import get_case_menu_mapping_overrides

        menu_overrides = get_case_menu_mapping_overrides()
    except Exception:
        menu_overrides = []

    for override in menu_overrides:
        if not isinstance(override, dict):
            continue
        key = str(override.get("key") or "").strip().upper()
        if not key:
            continue
        inherit = str(override.get("inherit") or "").strip().upper()
        raw_fields = override.get("fields") if isinstance(override.get("fields"), list) else []
        fields_for_mapping = apply_default_field_groups_for_mapping_key(
            [dict(field) for field in raw_fields if isinstance(field, dict)],
            key,
        )
        extra_allowed = [
            str(item or "").strip()
            for item in (override.get("extra_allowed") or [])
            if str(item or "").strip()
        ]
        namespace = str(override.get("namespace") or "").strip()
        is_noop_standard = key == inherit and not fields_for_mapping and not extra_allowed
        if is_noop_standard:
            continue

        menu_mapping_count += 1
        preview_mappings = dict(mappings)
        preview_mappings[key] = {
            "namespace": namespace,
            "inherit": inherit or None,
            "fields": fields_for_mapping,
            "extra_allowed": extra_allowed,
        }
        effective_fields = apply_default_field_groups_for_mapping_key(
            _effective_mapping_fields(preview_mappings, key),
            key,
        )
        create_preview = _build_create_preview(
            mapping_key=key,
            mapping=preview_mappings[key],
            effective_fields=effective_fields,
            definitions=definitions,
        )
        mapping_rows_by_key[key] = {
            "key": key,
            "namespace": namespace,
            "inherit": inherit,
            "field_count": len(effective_fields),
            "direct_field_count": len(fields_for_mapping),
            "effective_field_count": len(effective_fields),
            "fields": fields_for_mapping,
            "extra_allowed": extra_allowed,
            "baseline": key in baseline_mappings,
            "source": "create_menu",
            "menu_override": True,
            "read_only": True,
            "create_preview": create_preview,
        }

    mapping_rows = sorted(mapping_rows_by_key.values(), key=lambda item: item["key"].lower())

    return {
        "meta": meta,
        "fields": fields,
        "mappings": mapping_rows,
        "input_types": sorted(input_types),
        "input_type_presets": _INPUT_TYPE_PRESETS,
        "serializers": sorted(serializer_options),
        "option_sources": sorted(option_sources),
        "validator_types": sorted(validator_types),
        "baseline": {
            "field_count": len(baseline_fields),
            "mapping_count": len(baseline_mappings),
            "merged": bool(meta.get("baseline_merged")),
        },
        "case_menu": {
            "mapping_count": menu_mapping_count,
            "field_usage_count": sum(menu_usage.values()),
        },
    }


def upsert_field_definition(payload: dict[str, Any]) -> dict[str, Any]:
    data, _meta = load_parameter_registry_document()
    key = str(payload.get("key") or "").strip()
    current_key = str(payload.get("current_key") or key).strip()
    definition = _normalize_field_definition(key, payload)
    definitions = data["field_definitions"]
    baseline_fields = _baseline_field_keys()

    if current_key in baseline_fields and current_key != key:
        raise ValueError("baseline fields cannot be renamed")
    if current_key and current_key != key and current_key in definitions:
        usage = _field_usage_counts(data).get(current_key, 0)
        if usage:
            menu_usage = _case_menu_field_usage_counts().get(current_key, 0)
            if menu_usage:
                raise ValueError(
                    "cannot rename a field that is used by mappings or Matter Create Menu"
                )
            raise ValueError("cannot rename a field that is used by mappings")
        definitions.pop(current_key, None)
    definitions[key] = definition
    _save_registry_document(data)
    return build_parameter_admin_snapshot()


def delete_field_definition(key: str, *, force: bool = False) -> dict[str, Any]:
    key = str(key or "").strip()
    data, _meta = load_parameter_registry_document()
    definitions = data["field_definitions"]
    if key not in definitions:
        raise ValueError("field not found")
    if key in _baseline_field_keys():
        definitions[key] = {**definitions[key], "deprecated": True}
        _save_registry_document(data)
        return build_parameter_admin_snapshot()
    usage = _field_usage_counts(data).get(key, 0)
    if usage and not force:
        menu_usage = _case_menu_field_usage_counts().get(key, 0)
        if menu_usage:
            raise ValueError("field is used by mappings or Matter Create Menu")
        raise ValueError("field is used by mappings")
    definitions.pop(key, None)
    if force:
        for mapping in data["mappings"].values():
            if isinstance(mapping, dict) and isinstance(mapping.get("fields"), list):
                mapping["fields"] = [
                    field
                    for field in mapping["fields"]
                    if not (isinstance(field, dict) and field.get("key") == key)
                ]
    _save_registry_document(data)
    return build_parameter_admin_snapshot()


def upsert_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    data, _meta = load_parameter_registry_document()
    definitions = data["field_definitions"]
    key = str(payload.get("key") or "").strip().upper()
    current_key = str(payload.get("current_key") or key).strip().upper()
    if not key:
        raise ValueError("mapping key is required")
    baseline_mappings = _baseline_mapping_keys()
    if current_key in baseline_mappings and current_key != key:
        raise ValueError("baseline mappings cannot be renamed")

    fields = _normalize_mapping_fields(payload.get("fields"), definitions)
    if key in baseline_mappings:
        fields = _merge_baseline_mapping_fields(key, fields)
    fields = apply_default_field_groups_for_mapping_key(fields, key)
    mapping = {
        "namespace": str(payload.get("namespace") or "").strip(),
        "inherit": str(payload.get("inherit") or "").strip().upper() or None,
        "fields": fields,
        "extra_allowed": [
            str(item or "").strip()
            for item in (payload.get("extra_allowed") or [])
            if str(item or "").strip()
        ],
    }
    if not mapping["inherit"]:
        mapping.pop("inherit", None)

    mappings = data["mappings"]
    if current_key and current_key != key:
        mappings.pop(current_key, None)
    mappings[key] = mapping
    _save_registry_document(data)
    return build_parameter_admin_snapshot()


def repair_baseline_registry() -> dict[str, Any]:
    data, _meta = load_parameter_registry_document()
    _save_registry_document(data)
    return build_parameter_admin_snapshot()
