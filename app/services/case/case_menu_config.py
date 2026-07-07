from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from app.services.case_fields.grouping import apply_default_field_groups
from app.services.core.config_service import ConfigService

CASE_MENU_CONFIG_KEY = "CASE_MENU_CONFIG_JSON"

_DEFAULT_MENU_CONFIG: dict[str, Any] = {
    "version": 1,
    "sections": [
        {
            "id": "domestic",
            "label": "US Matters",
            "description": "USPTO and U.S. copyright docketing",
            "division": "DOM",
            "icon": "bi-house-door",
            "theme": "primary",
            "order": 10,
            "items": [
                {"id": "dom-patent", "label": "Patent", "division": "DOM", "type": "PATENT", "order": 10},
                {
                    "id": "dom-trademark",
                    "label": "Trademark",
                    "division": "DOM",
                    "type": "TRADEMARK",
                    "order": 20,
                },
                {"id": "dom-design", "label": "Design", "division": "DOM", "type": "DESIGN", "order": 30},
                {"id": "dom-utility", "label": "Utility", "division": "DOM", "type": "UTILITY", "order": 40},
            ],
        },
        {
            "id": "incoming",
            "label": "Inbound US Matters",
            "description": "Foreign-origin matters entering the U.S.",
            "division": "INC",
            "icon": "bi-box-arrow-in-down-right",
            "theme": "success",
            "order": 20,
            "items": [
                {"id": "inc-patent", "label": "Patent", "division": "INC", "type": "PATENT", "order": 10},
                {
                    "id": "inc-trademark",
                    "label": "Trademark",
                    "division": "INC",
                    "type": "TRADEMARK",
                    "order": 20,
                },
                {"id": "inc-design", "label": "Design", "division": "INC", "type": "DESIGN", "order": 30},
                {"id": "inc-utility", "label": "Utility", "division": "INC", "type": "UTILITY", "order": 40},
            ],
        },
        {
            "id": "overseas",
            "label": "Foreign Matters",
            "description": "Non-U.S. filings managed from the U.S. docket",
            "division": "OUT",
            "icon": "bi-airplane",
            "theme": "info",
            "order": 30,
            "items": [
                {"id": "out-patent", "label": "Patent", "division": "OUT", "type": "PATENT", "order": 10},
                {
                    "id": "out-trademark",
                    "label": "Trademark",
                    "division": "OUT",
                    "type": "TRADEMARK",
                    "order": 20,
                },
                {"id": "out-design", "label": "Design", "division": "OUT", "type": "DESIGN", "order": 30},
                {"id": "out-utility", "label": "Utility", "division": "OUT", "type": "UTILITY", "order": 40},
            ],
        },
        {
            "id": "other",
            "label": "Other Matters",
            "description": "International, proceedings, and miscellaneous matters",
            "division": "ETC",
            "icon": "bi-grid",
            "theme": "secondary",
            "order": 40,
            "items": [
                {
                    "id": "etc-pct",
                    "label": "PCT",
                    "description": "International application",
                    "division": "ETC",
                    "type": "PCT",
                    "order": 10,
                },
                {
                    "id": "etc-madrid",
                    "label": "Madrid",
                    "description": "Madrid application",
                    "division": "ETC",
                    "type": "MADRID",
                    "order": 20,
                },
                {
                    "id": "etc-hague",
                    "label": "Hague",
                    "description": "Hague application",
                    "division": "ETC",
                    "type": "HAGUE",
                    "order": 30,
                },
                {
                    "id": "etc-copyright",
                    "label": "Copyright",
                    "description": "Copyright",
                    "division": "ETC",
                    "type": "COPYRIGHT",
                    "order": 40,
                },
                {
                    "id": "etc-litigation",
                    "label": "Proceedings / Litigation",
                    "description": "Proceedings / Litigation",
                    "division": "ETC",
                    "type": "LITIGATION",
                    "order": 50,
                },
                {
                    "id": "etc-misc",
                    "label": "Other",
                    "description": "Other",
                    "division": "ETC",
                    "type": "MISC",
                    "order": 60,
                },
            ],
        },
    ],
}

_SECTION_COLLAPSE_IDS = {
    "domestic": "caseCategoryDomestic",
    "incoming": "caseCategoryIncoming",
    "overseas": "caseCategoryOverseas",
    "other": "caseCategoryOther",
}
_SECTION_HEADING_IDS = {
    key: f"{collapse_id}Heading" for key, collapse_id in _SECTION_COLLAPSE_IDS.items()
}
_SECTION_LEGACY_PATHS = {
    "domestic": ("/case/dom/",),
    "incoming": ("/case/inc/",),
    "overseas": ("/case/out/",),
    "other": (
        "/case/etc/",
        "/case/PCT",
        "/case/pct",
        "/case/madrid",
        "/case/hague",
        "/case/copyright",
        "/case/litigation",
        "/case/misc",
    ),
}
_STANDARD_LIST_ENDPOINTS = {
    ("DOM", "PATENT"): "case_work.list_dom_patent",
    ("DOM", "TRADEMARK"): "case_work.list_dom_trademark",
    ("DOM", "DESIGN"): "case_work.list_dom_design",
    ("DOM", "UTILITY"): "case_work.list_dom_utility",
    ("INC", "PATENT"): "case_work.list_inc_patent",
    ("INC", "TRADEMARK"): "case_work.list_inc_trademark",
    ("INC", "DESIGN"): "case_work.list_inc_design",
    ("INC", "UTILITY"): "case_work.list_inc_utility",
    ("OUT", "PATENT"): "case_work.list_out_patent",
    ("OUT", "TRADEMARK"): "case_work.list_out_trademark",
    ("OUT", "DESIGN"): "case_work.list_out_design",
    ("OUT", "UTILITY"): "case_work.list_out_utility",
    ("ETC", "PCT"): "case_work.list_pct",
    ("ETC", "MADRID"): "case_work.list_madrid",
    ("ETC", "HAGUE"): "case_work.list_hague",
    ("ETC", "COPYRIGHT"): "case_work.list_copyright",
    ("ETC", "LITIGATION"): "case_work.list_litigation",
    ("ETC", "MISC"): "case_work.list_misc",
}
_STANDARD_PROFILE = {
    ("DOM", "PATENT"): ("DOM", "PATENT", "domestic_patent"),
    ("DOM", "TRADEMARK"): ("DOM", "TRADEMARK", "domestic_trademark"),
    ("DOM", "DESIGN"): ("DOM", "DESIGN", "domestic_design"),
    ("DOM", "UTILITY"): ("DOM", "PATENT", "domestic_patent"),
    ("INC", "PATENT"): ("INC", "PATENT", "incoming_patent"),
    ("INC", "TRADEMARK"): ("INC", "TRADEMARK", "incoming_trademark"),
    ("INC", "DESIGN"): ("INC", "DESIGN", "incoming_design"),
    ("INC", "UTILITY"): ("INC", "PATENT", "incoming_patent"),
    ("OUT", "PATENT"): ("OUT", "PATENT", "outgoing_patent"),
    ("OUT", "TRADEMARK"): ("OUT", "TRADEMARK", "outgoing_trademark"),
    ("OUT", "DESIGN"): ("OUT", "DESIGN", "outgoing_design"),
    ("OUT", "UTILITY"): ("OUT", "PATENT", "outgoing_patent"),
    ("ETC", "PCT"): ("OUT", "PCT", "pct"),
    ("ETC", "MADRID"): ("OUT", "TRADEMARK", "outgoing_trademark"),
    ("ETC", "HAGUE"): ("OUT", "DESIGN", "outgoing_design"),
    ("ETC", "COPYRIGHT"): ("", "MISC", "misc"),
    ("ETC", "LITIGATION"): ("", "LITIGATION", "litigation"),
    ("ETC", "MISC"): ("", "MISC", "misc"),
}
_VALID_PROFILE_TYPES = {
    "PATENT",
    "UTILITY",
    "DESIGN",
    "TRADEMARK",
    "PCT",
    "LITIGATION",
    "MISC",
}
_TYPE_TOKEN_RE = re.compile(r"[^A-Z0-9_-]+")


def default_case_menu_config() -> dict[str, Any]:
    return deepcopy(_DEFAULT_MENU_CONFIG)


def default_case_menu_config_json() -> str:
    return json.dumps(default_case_menu_config(), ensure_ascii=False, indent=2)


def normalize_case_menu_division(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"DOM", "INC", "OUT", "ETC"}:
        return raw
    aliases = {
        "DOMESTIC": "DOM",
        "US": "DOM",
        "INCOMING": "INC",
        "INBOUND": "INC",
        "INBOUND US": "INC",
        "FOREIGN": "OUT",
        "OVERSEAS": "OUT",
        "OUTGOING": "OUT",
        "OTHER": "ETC",
        "MISC": "ETC",
    }
    return aliases.get(raw, "")


def normalize_case_menu_type(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    token = _TYPE_TOKEN_RE.sub("_", raw.upper()).strip("_-")
    return token[:80]


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _clean_id(value: Any, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    return cleaned or fallback


def _clean_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _clean_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _normalize_field_rows(raw_fields: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(raw_fields, list):
        return rows
    for index, field in enumerate(raw_fields):
        if isinstance(field, str):
            key = field.strip()
            if not key:
                continue
            rows.append(
                {
                    "key": key,
                    "order": index + 1,
                    "col": 1,
                    "required": False,
                    "group": "",
                }
            )
            continue
        if not isinstance(field, dict):
            continue
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        order = field.get("order", index + 1)
        col = field.get("col", 1)
        try:
            order = float(order)
        except Exception:
            order = float(index + 1)
        try:
            col = int(col)
        except Exception:
            col = 1
        if col not in (1, 2):
            col = 1
        row = {
            "key": key,
            "order": order,
            "col": col,
            "required": bool(field.get("required", False)),
            "group": _clean_text(field.get("group") or field.get("section")),
        }
        group_order = _clean_float_or_none(field.get("group_order", field.get("section_order")))
        if group_order is not None:
            row["group_order"] = group_order
        rows.append(row)
    return rows


def _mapping_key_candidates(division: str, case_type: str) -> list[str]:
    candidates: list[str] = []
    if division and case_type:
        candidates.extend([f"IP:{division}:{case_type}", f"{division}:{case_type}"])
    if case_type:
        candidates.extend([f"IP:{case_type}", case_type])
    return candidates


def _load_mapping_payload(
    mappings: dict[str, Any], key: str, seen: set[str] | None = None
) -> dict[str, Any] | None:
    seen = seen or set()
    if key in seen:
        return None
    raw = mappings.get(key)
    if not isinstance(raw, dict):
        return None

    parent: dict[str, Any] = {}
    inherit_key = str(raw.get("inherit") or "").strip()
    if inherit_key:
        parent = _load_mapping_payload(mappings, inherit_key, seen | {key}) or {}

    raw_fields = raw.get("fields")
    parent_fields = parent.get("fields")
    raw_extra = raw.get("extra_allowed")
    parent_extra = parent.get("extra_allowed")

    extra_allowed: list[str] = []
    for value in (list(parent_extra) if isinstance(parent_extra, list) else []) + (
        list(raw_extra) if isinstance(raw_extra, list) else []
    ):
        text = str(value or "").strip()
        if text and text not in extra_allowed:
            extra_allowed.append(text)

    return {
        "namespace": str(raw.get("namespace") or parent.get("namespace") or "").strip(),
        "fields": (
            raw_fields if isinstance(raw_fields, list) and raw_fields else parent_fields or []
        ),
        "extra_allowed": extra_allowed,
    }


def _profile_mapping_payload(profile_division: str, profile_type: str) -> dict[str, Any] | None:
    try:
        from app.services.case_fields.unified_config import load_unified_registry_data

        data, _meta = load_unified_registry_data()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    mappings = data.get("mappings")
    if not isinstance(mappings, dict):
        mappings = data
    if not isinstance(mappings, dict):
        return None

    for key in _mapping_key_candidates(profile_division, profile_type):
        payload = _load_mapping_payload(mappings, key)
        if payload:
            return payload
    return None


def _generated_namespace(division: str, case_type: str) -> str:
    return f"custom_{division.lower()}_{case_type.lower()}".replace("-", "_")


def _normalize_item(raw_item: Any, *, section: dict[str, Any], index: int) -> dict[str, Any] | None:
    if not isinstance(raw_item, dict):
        return None
    section_division = normalize_case_menu_division(section.get("division"))
    division = normalize_case_menu_division(raw_item.get("division")) or section_division
    case_type = normalize_case_menu_type(raw_item.get("type") or raw_item.get("case_type"))
    if not division or not case_type:
        return None

    profile_division = normalize_case_menu_division(raw_item.get("profile_division"))
    profile_type = normalize_case_menu_type(raw_item.get("profile_type"))
    default_profile = _STANDARD_PROFILE.get((division, case_type))
    if default_profile:
        profile_division = profile_division or default_profile[0]
        profile_type = profile_type or default_profile[1]
    elif not profile_type:
        profile_type = "MISC"
    if profile_type in {"LITIGATION", "MISC"}:
        profile_division = ""
    namespace = _clean_text(raw_item.get("namespace"))
    if not namespace and default_profile:
        namespace = default_profile[2]
    if not namespace:
        namespace = _generated_namespace(division, case_type)

    order = _clean_int(raw_item.get("order"), index + 1)
    label = _clean_text(raw_item.get("label"), case_type)
    item_id = _clean_id(raw_item.get("id"), f"{division.lower()}-{case_type.lower()}")
    field_mapping = raw_item.get("field_mapping")
    if isinstance(field_mapping, dict):
        raw_fields = field_mapping.get("fields")
        extra_allowed = field_mapping.get("extra_allowed", raw_item.get("extra_allowed", []))
    else:
        raw_fields = raw_item.get("fields")
        extra_allowed = raw_item.get("extra_allowed", [])
    extra_allowed_list = [
        str(value or "").strip()
        for value in (extra_allowed if isinstance(extra_allowed, list) else [])
        if str(value or "").strip()
    ]
    fields = _normalize_field_rows(raw_fields)
    inherited_mapping = _profile_mapping_payload(profile_division, profile_type)
    fields_inherited = False
    if not fields and inherited_mapping:
        inherited_fields = _normalize_field_rows(inherited_mapping.get("fields"))
        if inherited_fields:
            fields = inherited_fields
            fields_inherited = True
            inherited_namespace = str(inherited_mapping.get("namespace") or "").strip()
            if inherited_namespace and (
                not namespace or namespace == _generated_namespace(division, case_type)
            ):
                namespace = inherited_namespace

    fields = apply_default_field_groups(
        fields,
        profile_division or division,
        profile_type or case_type,
    )

    item = {
        "id": item_id,
        "label": label,
        "description": _clean_text(raw_item.get("description")),
        "division": division,
        "type": case_type,
        "order": order,
        "enabled": raw_item.get("enabled", True) is not False,
        "profile_division": profile_division,
        "profile_type": profile_type,
        "profile_group": normalize_case_menu_type(raw_item.get("profile_group")),
        "namespace": namespace,
        "fields": fields,
        "fields_inherited": fields_inherited,
        "extra_allowed": extra_allowed_list,
        "forced_app_route": _clean_text(raw_item.get("forced_app_route")),
        "supports_image": raw_item.get("supports_image"),
        "auto_status": raw_item.get("auto_status"),
        "list_endpoint": _STANDARD_LIST_ENDPOINTS.get((division, case_type), ""),
        "is_custom": (division, case_type) not in _STANDARD_LIST_ENDPOINTS,
    }
    if item["profile_type"] not in _VALID_PROFILE_TYPES:
        item["profile_type"] = "MISC"
        item["profile_division"] = ""
    return item


def _normalize_section(raw_section: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw_section, dict):
        return None
    section_id = _clean_id(raw_section.get("id"), f"section-{index + 1}")
    division = normalize_case_menu_division(raw_section.get("division"))
    section = {
        "id": section_id,
        "label": _clean_text(raw_section.get("label"), "Case Menu"),
        "description": _clean_text(raw_section.get("description")),
        "division": division,
        "icon": _clean_text(raw_section.get("icon"), "bi-folder"),
        "theme": _clean_text(raw_section.get("theme"), "secondary"),
        "order": _clean_int(raw_section.get("order"), index + 1),
        "enabled": raw_section.get("enabled", True) is not False,
        "collapse_id": _SECTION_COLLAPSE_IDS.get(section_id, f"caseCategoryCustom{index + 1}"),
        "heading_id": _SECTION_HEADING_IDS.get(section_id, f"caseCategoryCustom{index + 1}Heading"),
        "legacy_paths": _SECTION_LEGACY_PATHS.get(section_id, ()),
        "items": [],
    }
    raw_items = raw_section.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    for item_index, raw_item in enumerate(raw_items):
        item = _normalize_item(raw_item, section=section, index=item_index)
        if item:
            section["items"].append(item)
    section["items"].sort(key=lambda item: (item.get("order", 0), item.get("label", "")))
    return section


def normalize_case_menu_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    source = raw if "sections" in raw else default_case_menu_config()
    sections = []
    for index, raw_section in enumerate(source.get("sections") or []):
        section = _normalize_section(raw_section, index)
        if section and section.get("enabled") and section.get("items"):
            sections.append(section)
    sections.sort(key=lambda section: (section.get("order", 0), section.get("label", "")))
    return {"version": raw.get("version") or 1, "sections": sections}


def get_case_menu_config() -> dict[str, Any]:
    raw = ConfigService.get_json(CASE_MENU_CONFIG_KEY, None)
    return normalize_case_menu_config(raw if raw is not None else default_case_menu_config())


def case_menu_config_json_for_editor(raw: Any | None = None) -> str:
    if raw is None:
        raw = ConfigService.get_json(CASE_MENU_CONFIG_KEY, None)
        if raw is None:
            raw = default_case_menu_config()
    return json.dumps(normalize_case_menu_config(raw), ensure_ascii=False, indent=2)


def iter_case_menu_items(*, enabled_only: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for section in get_case_menu_config().get("sections", []):
        if enabled_only and section.get("enabled") is False:
            continue
        for item in section.get("items", []):
            if enabled_only and item.get("enabled") is False:
                continue
            out.append(dict(item, section_id=section.get("id"), section_label=section.get("label")))
    return out


def find_case_menu_item(division: Any, case_type: Any) -> dict[str, Any] | None:
    div = normalize_case_menu_division(division)
    typ = normalize_case_menu_type(case_type)
    if not div or not typ:
        return None
    for item in iter_case_menu_items():
        if item.get("division") == div and item.get("type") == typ:
            return item
    return None


def is_configured_case_menu_kind(division: Any, case_type: Any) -> bool:
    return find_case_menu_item(division, case_type) is not None


def get_case_menu_mapping_overrides() -> list[dict[str, Any]]:
    overrides: list[dict[str, Any]] = []
    for item in iter_case_menu_items():
        division = item.get("division", "")
        case_type = item.get("type", "")
        if not division or not case_type:
            continue
        mapping_key = f"IP:{division}:{case_type}"
        profile_division = item.get("profile_division", "")
        profile_type = item.get("profile_type", "")
        inherit_key = ""
        if profile_type:
            inherit_key = (
                f"IP:{profile_division}:{profile_type}"
                if profile_division
                else f"IP:{profile_type}"
            )
        overrides.append(
            {
                "key": mapping_key,
                "namespace": item.get("namespace") or "",
                "fields": [] if item.get("fields_inherited") else item.get("fields") or [],
                "extra_allowed": item.get("extra_allowed") or [],
                "inherit": inherit_key,
            }
        )
    return overrides


def validate_case_menu_config_payload(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else default_case_menu_config()
        except Exception as exc:
            return {
                "valid": False,
                "errors": [f"invalid_json:{type(exc).__name__}"],
                "warnings": [],
                "preview": {},
            }
    if not isinstance(payload, dict):
        return {
            "valid": False,
            "errors": ["case menu config must be a JSON object"],
            "warnings": [],
            "preview": {},
        }
    raw_sections = payload.get("sections")
    if raw_sections is not None and not isinstance(raw_sections, list):
        errors.append("sections: must be list")
    normalized = normalize_case_menu_config(payload)
    try:
        from app.services.case_fields import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
    except Exception:
        registry = None
    seen: set[tuple[str, str]] = set()
    for section in normalized.get("sections", []):
        if not section.get("items"):
            warnings.append(f"{section.get('id')}: no enabled items")
        for item in section.get("items", []):
            key = (str(item.get("division") or ""), str(item.get("type") or ""))
            if key in seen:
                errors.append(f"duplicate item division/type: {key[0]}:{key[1]}")
            seen.add(key)
            profile_type = str(item.get("profile_type") or "")
            if profile_type and profile_type not in _VALID_PROFILE_TYPES:
                errors.append(f"{item.get('id')}.profile_type: unsupported profile type")
            if item.get("fields"):
                seen_fields: set[str] = set()
                duplicate_fields: set[str] = set()
                for idx, field in enumerate(item.get("fields") or []):
                    field_key = str(field.get("key") or "").strip()
                    if not field_key:
                        errors.append(f"{item.get('id')}.fields[{idx}].key: required")
                    elif registry is not None and not registry.exists(field_key):
                        errors.append(
                            f"{item.get('id')}.fields[{idx}].key: unknown field '{field_key}'"
                        )
                    if field_key:
                        if field_key in seen_fields:
                            duplicate_fields.add(field_key)
                        seen_fields.add(field_key)
                if duplicate_fields:
                    warnings.append(
                        f"{item.get('id')}.fields: duplicate field keys "
                        + ",".join(sorted(duplicate_fields))
                    )
    if not normalized.get("sections"):
        errors.append("sections: at least one enabled section with items is required")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "preview": preview_case_menu_config(normalized),
    }


def _preview_groups(fields: list[dict[str, Any]]) -> list[str]:
    groups: dict[str, dict[str, float | int | None]] = {}
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        label = str(field.get("group") or "").strip()
        if not label:
            continue
        try:
            group_order = (
                float(field.get("group_order"))
                if field.get("group_order") not in (None, "")
                else None
            )
        except Exception:
            group_order = None
        if label not in groups:
            groups[label] = {"order": group_order, "first": index}
        elif group_order is not None:
            current = groups[label].get("order")
            groups[label]["order"] = group_order if current is None else min(float(current), group_order)
    return [
        label
        for label, _info in sorted(
            groups.items(),
            key=lambda item: (
                item[1].get("order")
                if item[1].get("order") is not None
                else float(item[1].get("first") or 0) + 10000,
                item[1].get("first") or 0,
            ),
        )
    ]


def preview_case_menu_config(payload: Any | None = None) -> dict[str, Any]:
    normalized = normalize_case_menu_config(payload) if payload is not None else get_case_menu_config()
    sections = []
    for section in normalized.get("sections", []):
        sections.append(
            {
                "id": section.get("id"),
                "label": section.get("label"),
                "item_count": len(section.get("items") or []),
                "items": [
                    {
                        "label": item.get("label"),
                        "division": item.get("division"),
                        "type": item.get("type"),
                        "profile": ":".join(
                            part
                            for part in (
                                item.get("profile_division"),
                                item.get("profile_type"),
                            )
                            if part
                        ),
                        "namespace": item.get("namespace"),
                        "field_count": len(item.get("fields") or []),
                        "groups": _preview_groups(item.get("fields") or []),
                    }
                    for item in section.get("items", [])
                ],
            }
        )
    return {"version": normalized.get("version"), "sections": sections}


def case_menu_profile_values(division: Any, case_type: Any) -> tuple[str, str, str] | None:
    item = find_case_menu_item(division, case_type)
    if not item:
        return None
    return (
        str(item.get("profile_division") or ""),
        str(item.get("profile_type") or ""),
        str(item.get("namespace") or ""),
    )
