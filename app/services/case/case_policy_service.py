from __future__ import annotations

from typing import Any

from app.services.core.config_service import ConfigService
from app.services.rules.rule_registry import get_rule_section_payload
from app.utils.error_logging import report_swallowed_exception

CASE_POLICY_KEY = "CASE_POLICY_JSON"
CASE_PROFILE_RULES_KEY = "CASE_PROFILE_RULES_JSON"


def _load_json(key: str) -> dict[str, Any]:
    raw = ConfigService.get_json(key, None)
    if isinstance(raw, dict):
        return raw
    return {}


def get_policy_section(name: str) -> dict[str, Any]:
    data = _load_json(CASE_POLICY_KEY)
    section = data.get(name) if isinstance(data, dict) else None
    if isinstance(section, dict):
        return section
    return {}


def _normalize_case_key(division: str | None, case_type: str | None) -> str:
    div = (division or "").strip().upper()
    typ = (case_type or "").strip().upper()
    if not div:
        return typ
    return f"{div}:{typ}"


def _index_profile_rules(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize profile rules into a key->dict map."""
    if isinstance(raw, dict):
        # Support {"profiles": {...}} or direct mapping.
        profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else raw
        if isinstance(profiles, dict):
            return {
                str(k).strip().upper(): dict(v) for k, v in profiles.items() if isinstance(v, dict)
            }
        return {}
    if isinstance(raw, list):
        out: dict[str, dict[str, Any]] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            key = (row.get("key") or "").strip()
            if not key:
                key = _normalize_case_key(
                    row.get("division"), row.get("case_type") or row.get("type")
                )
            if not key:
                continue
            out[key.strip().upper()] = dict(row)
        return out
    return {}


def get_case_profile_rules() -> dict[str, Any]:
    """
    Return raw rules dict.

    Supports RULE_REGISTRY_JSON.rules.case_profiles first, then the legacy
    CASE_PROFILE_RULES_JSON and CASE_POLICY_JSON.case_profiles sources.
    """
    registry_rules = get_rule_section_payload("case_profiles")
    if registry_rules:
        return registry_rules
    raw = _load_json(CASE_PROFILE_RULES_KEY)
    if raw:
        return raw
    return get_policy_section("case_profiles")


def get_case_profile_override(
    division: str | None,
    case_type: str | None,
    *,
    group: str | None = None,
) -> dict[str, Any]:
    """
    Resolve a case profile override for the given division/type.

    Rule precedence:
    1) DIV:TYPE
    2) DIV:GROUP (if group provided)
    3) TYPE
    4) GROUP (if group provided)
    """
    try:
        rules = get_case_profile_rules()
        profiles = _index_profile_rules(rules)
        if not profiles:
            return {}
        key = _normalize_case_key(division, case_type)
        group_key = _normalize_case_key(division, group) if group else ""
        for cand in (
            key,
            group_key,
            (case_type or "").strip().upper(),
            (group or "").strip().upper(),
        ):
            if cand and cand in profiles:
                return dict(profiles[cand] or {})
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case_policy_service.get_case_profile_override",
            log_key="case_policy_service.get_case_profile_override",
            log_window_seconds=300,
        )
    return {}


def get_case_profile_group_map() -> dict[str, str]:
    rules = get_case_profile_rules()
    if not isinstance(rules, dict):
        return {}
    raw = rules.get("group_map") or rules.get("groups") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        k = str(key).strip().upper()
        v = str(val).strip().upper()
        if k and v:
            out[k] = v
    return out
