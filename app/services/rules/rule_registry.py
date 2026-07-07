from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception

RULE_REGISTRY_KEY = "RULE_REGISTRY_JSON"

_KNOWN_SECTIONS = {
    "case_profiles",
    "us_defaults",
    "matter_defaults",
    "automation_actions",
}
_RULE_META_KEYS = {
    "id",
    "key",
    "name",
    "description",
    "version",
    "effective_from",
    "effective_to",
    "enabled",
    "scope",
    "country",
    "division",
    "case_type",
    "type",
    "right_type",
    "action",
    "source",
    "order",
    "payload",
}


@dataclass(frozen=True)
class RuleResolution:
    section: str
    payload: dict[str, Any]
    rule_id: str | None = None
    version: str | None = None
    source: str = RULE_REGISTRY_KEY
    effective_from: str | None = None
    effective_to: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.payload)

    def audit_meta(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "rule_id": self.rule_id,
            "version": self.version,
            "source": self.source,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_token(value: Any) -> str:
    return str(value or "").strip().upper()


def _parse_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _today(effective_at: date | datetime | None) -> date:
    if isinstance(effective_at, datetime):
        return effective_at.date()
    if isinstance(effective_at, date):
        return effective_at
    return date.today()


def _load_registry() -> dict[str, Any]:
    raw = ConfigService.get_json(RULE_REGISTRY_KEY, {})
    if isinstance(raw, dict):
        return raw
    return {}


def _registry_section(registry: dict[str, Any], section: str) -> Any:
    rules = registry.get("rules")
    if isinstance(rules, dict) and section in rules:
        return rules.get(section)
    return registry.get(section)


def _context_key_candidates(context: dict[str, Any]) -> set[str]:
    country = _clean_token(context.get("country"))
    division = _clean_token(context.get("division"))
    case_type = _clean_token(context.get("case_type") or context.get("type"))
    right_type = _clean_token(context.get("right_type"))
    action = _clean_token(context.get("action"))
    source = _clean_token(context.get("source"))
    out = {token for token in (country, division, case_type, right_type, action, source) if token}
    if division and case_type:
        out.add(f"{division}:{case_type}")
    if country and division and case_type:
        out.add(f"{country}:{division}:{case_type}")
    if country and right_type:
        out.add(f"{country}:{right_type}")
    if source and action:
        out.add(f"{source}:{action}")
    return out


def _scope_for(rule: dict[str, Any]) -> dict[str, Any]:
    scope = _as_dict(rule.get("scope"))
    merged = dict(scope)
    for key in ("country", "division", "case_type", "type", "right_type", "action", "source"):
        if key in rule and key not in merged:
            merged[key] = rule.get(key)
    return merged


def _rule_payload(rule: dict[str, Any]) -> dict[str, Any]:
    payload = rule.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    return {k: v for k, v in rule.items() if k not in _RULE_META_KEYS}


def _specificity(rule: dict[str, Any], context: dict[str, Any]) -> int | None:
    if rule.get("enabled") is False:
        return None

    key = _clean_token(rule.get("key"))
    if key:
        candidates = _context_key_candidates(context)
        if candidates and key not in candidates:
            return None

    score = 1 if key else 0
    scope = _scope_for(rule)
    for raw_key, raw_value in scope.items():
        selector = _clean_token(raw_value)
        if not selector:
            continue
        key_name = "case_type" if raw_key == "type" else str(raw_key)
        context_value = _clean_token(context.get(key_name))
        if key_name == "case_type" and not context_value:
            context_value = _clean_token(context.get("type"))
        if context_value and context_value != selector:
            return None
        if context_value:
            score += 1
    return score


def _is_effective(rule: dict[str, Any], *, effective_at: date) -> bool:
    starts = _parse_date(rule.get("effective_from"))
    ends = _parse_date(rule.get("effective_to"))
    if starts and starts > effective_at:
        return False
    if ends and ends < effective_at:
        return False
    return True


def _sort_key(item: tuple[int, int, dict[str, Any]]) -> tuple[int, date, int, str]:
    index, specificity, rule = item
    effective_from = _parse_date(rule.get("effective_from")) or date.min
    version = str(rule.get("version") or "")
    return (specificity, effective_from, -index, version)


def _resolve_from_entries(
    section: str,
    entries: list[Any],
    *,
    context: dict[str, Any],
    registry_version: str | None,
    effective_at: date,
) -> RuleResolution:
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if not _is_effective(entry, effective_at=effective_at):
            continue
        specificity = _specificity(entry, context)
        if specificity is None:
            continue
        candidates.append((index, specificity, entry))

    if not candidates:
        return RuleResolution(section=section, payload={}, version=registry_version)

    _index, _specificity_score, selected = sorted(candidates, key=_sort_key)[-1]
    return RuleResolution(
        section=section,
        payload=_rule_payload(selected),
        rule_id=str(selected.get("id") or selected.get("key") or "").strip() or None,
        version=str(selected.get("version") or registry_version or "").strip() or None,
        effective_from=str(selected.get("effective_from") or "").strip() or None,
        effective_to=str(selected.get("effective_to") or "").strip() or None,
    )


def _resolve_section_value(
    section: str,
    value: Any,
    *,
    context: dict[str, Any],
    registry_version: str | None,
    effective_at: date,
) -> RuleResolution:
    if isinstance(value, list):
        return _resolve_from_entries(
            section,
            value,
            context=context,
            registry_version=registry_version,
            effective_at=effective_at,
        )

    if not isinstance(value, dict):
        return RuleResolution(section=section, payload={}, version=registry_version)

    entries = value.get("entries")
    if not isinstance(entries, list):
        entries = value.get("rules")
    if isinstance(entries, list):
        resolved = _resolve_from_entries(
            section,
            entries,
            context=context,
            registry_version=str(value.get("version") or registry_version or "") or None,
            effective_at=effective_at,
        )
        if resolved.found:
            return resolved

    if not _is_effective(value, effective_at=effective_at):
        return RuleResolution(section=section, payload={}, version=registry_version)

    return RuleResolution(
        section=section,
        payload=_rule_payload(value) if "payload" in value else dict(value),
        rule_id=str(value.get("id") or value.get("key") or section).strip() or section,
        version=str(value.get("version") or registry_version or "").strip() or None,
        effective_from=str(value.get("effective_from") or "").strip() or None,
        effective_to=str(value.get("effective_to") or "").strip() or None,
    )


def resolve_rule_from_registry(
    section: str,
    registry: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    effective_at: date | datetime | None = None,
) -> RuleResolution:
    section_name = str(section or "").strip()
    if not section_name or not isinstance(registry, dict):
        return RuleResolution(section=section_name, payload={})

    registry_version = str(registry.get("version") or "").strip() or None
    value = _registry_section(registry, section_name)
    if value is None:
        return RuleResolution(section=section_name, payload={}, version=registry_version)
    return _resolve_section_value(
        section_name,
        value,
        context=dict(context or {}),
        registry_version=registry_version,
        effective_at=_today(effective_at),
    )


def resolve_rule(
    section: str,
    *,
    context: dict[str, Any] | None = None,
    effective_at: date | datetime | None = None,
    legacy: dict[str, Any] | None = None,
    legacy_source: str = "legacy",
) -> RuleResolution:
    section_name = str(section or "").strip()
    if not section_name:
        return RuleResolution(section="", payload={})

    registry = _load_registry()
    registry_version = str(registry.get("version") or "").strip() or None
    if _registry_section(registry, section_name) is not None:
        try:
            resolved = resolve_rule_from_registry(
                section_name,
                registry,
                context=dict(context or {}),
                effective_at=effective_at,
            )
            if resolved.found:
                return resolved
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=f"rule_registry.resolve_rule.{section_name}",
                log_key=f"rule_registry.resolve_rule.{section_name}",
                log_window_seconds=300,
            )

    if isinstance(legacy, dict) and legacy:
        return RuleResolution(
            section=section_name,
            payload=dict(legacy),
            rule_id=section_name,
            version=str(legacy.get("version") or "").strip() or None,
            source=legacy_source,
        )
    return RuleResolution(section=section_name, payload={}, version=registry_version)


def get_rule_section_payload(
    section: str,
    *,
    context: dict[str, Any] | None = None,
    effective_at: date | datetime | None = None,
    legacy: dict[str, Any] | None = None,
    legacy_source: str = "legacy",
) -> dict[str, Any]:
    return resolve_rule(
        section,
        context=context,
        effective_at=effective_at,
        legacy=legacy,
        legacy_source=legacy_source,
    ).payload


def _validate_int_like(
    value: Any,
    errors: list[str],
    path: str,
    *,
    min_value: int | None = None,
    allow_blank: bool = True,
) -> None:
    if value in (None, "") and allow_blank:
        return
    try:
        parsed = int(str(value).strip())
    except Exception:
        errors.append(f"{path}: must be integer")
        return
    if min_value is not None and parsed < min_value:
        errors.append(f"{path}: must be >= {min_value}")


def _validate_string_or_list(value: Any, errors: list[str], path: str) -> None:
    if value in (None, ""):
        return
    if isinstance(value, str):
        return
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return
    errors.append(f"{path}: must be string or string list")


def _validate_us_defaults_payload(
    payload: Any,
    errors: list[str],
    warnings: list[str],
    path: str,
) -> None:
    if not isinstance(payload, dict):
        errors.append(f"{path}: must be object")
        return

    for field, min_value in (
        ("internal_offset_days", 0),
        ("renewal_start_year", 1),
        ("renewal_end_year", 1),
        ("renewal_days_before", 0),
    ):
        if field in payload:
            _validate_int_like(payload.get(field), errors, f"{path}.{field}", min_value=min_value)

    if "renewal_case_types" in payload:
        _validate_string_or_list(
            payload.get("renewal_case_types"), errors, f"{path}.renewal_case_types"
        )

    end_by_type = payload.get("renewal_end_year_by_case_type")
    if end_by_type is not None:
        if not isinstance(end_by_type, dict):
            errors.append(f"{path}.renewal_end_year_by_case_type: must be object")
        else:
            for key, value in end_by_type.items():
                if not str(key or "").strip():
                    errors.append(f"{path}.renewal_end_year_by_case_type: blank case type")
                    continue
                _validate_int_like(
                    value,
                    errors,
                    f"{path}.renewal_end_year_by_case_type.{key}",
                    min_value=1,
                    allow_blank=False,
                )

    templates = payload.get("templates")
    if templates is None:
        templates = payload.get("default_deadlines")
    if templates is None:
        warnings.append(f"{path}.templates: no default deadline templates")
        return
    if not isinstance(templates, list):
        errors.append(f"{path}.templates: must be list")
        return
    for idx, template in enumerate(templates):
        t_path = f"{path}.templates[{idx}]"
        if not isinstance(template, dict):
            errors.append(f"{t_path}: must be object")
            continue
        if not str(template.get("title") or "").strip():
            errors.append(f"{t_path}.title: required")
        if not str(template.get("base") or "").strip():
            errors.append(f"{t_path}.base: required")
        if "due_offset_days" in template:
            _validate_int_like(template.get("due_offset_days"), errors, f"{t_path}.due_offset_days")
        if "internal_offset_days" in template:
            _validate_int_like(
                template.get("internal_offset_days"),
                errors,
                f"{t_path}.internal_offset_days",
                min_value=0,
            )


def _validate_section(section: str, value: Any, errors: list[str], warnings: list[str]) -> None:
    if section not in _KNOWN_SECTIONS:
        warnings.append(f"unknown_section:{section}")
    if not isinstance(value, (dict, list)):
        errors.append(f"{section}: section must be object or list")
        return
    entries = value if isinstance(value, list) else None
    if isinstance(value, dict):
        if isinstance(value.get("entries"), list):
            entries = value.get("entries")
        elif isinstance(value.get("rules"), list):
            entries = value.get("rules")
    if entries is not None:
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"{section}[{idx}]: rule entry must be object")
                continue
            if entry.get("enabled") is False:
                continue
            if not (str(entry.get("id") or entry.get("key") or "").strip()):
                warnings.append(f"{section}[{idx}]: id or key is recommended")
            if "payload" in entry and not isinstance(entry.get("payload"), dict):
                errors.append(f"{section}[{idx}].payload: must be object")
            if section == "us_defaults":
                payload = entry.get("payload") if "payload" in entry else entry
                _validate_us_defaults_payload(
                    payload, errors, warnings, f"{section}[{idx}].payload"
                )
            for date_key in ("effective_from", "effective_to"):
                if entry.get(date_key) and _parse_date(entry.get(date_key)) is None:
                    errors.append(f"{section}[{idx}].{date_key}: invalid date")
        return

    if section == "case_profiles":
        profiles = value.get("profiles") if isinstance(value, dict) else None
        if profiles is not None and not isinstance(profiles, (dict, list)):
            errors.append("case_profiles.profiles: must be object or list")
    if section == "us_defaults":
        templates = value.get("templates") if isinstance(value, dict) else None
        if templates is not None and not isinstance(templates, list):
            errors.append("us_defaults.templates: must be list")
        _validate_us_defaults_payload(value, errors, warnings, "us_defaults")
    if isinstance(value, dict):
        for date_key in ("effective_from", "effective_to"):
            if value.get(date_key) and _parse_date(value.get(date_key)) is None:
                errors.append(f"{section}.{date_key}: invalid date")


def validate_rule_registry_payload(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
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
            "errors": ["registry must be a JSON object"],
            "warnings": [],
            "preview": {},
        }

    if not str(payload.get("version") or "").strip():
        warnings.append("version is recommended")

    raw_sections = payload.get("rules") if isinstance(payload.get("rules"), dict) else payload
    sections = {
        str(k): v
        for k, v in raw_sections.items()
        if str(k) not in {"version", "metadata", "schema", "rules"}
    }
    if not sections:
        warnings.append("no rule sections found")

    for section, value in sections.items():
        _validate_section(section, value, errors, warnings)

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "preview": preview_rule_registry(payload),
    }


def preview_rule_registry(payload: Any | None = None) -> dict[str, Any]:
    registry = payload if isinstance(payload, dict) else _load_registry()
    if not isinstance(registry, dict):
        return {}
    raw_sections = registry.get("rules") if isinstance(registry.get("rules"), dict) else registry
    preview: dict[str, Any] = {
        "version": registry.get("version"),
        "sections": {},
    }
    for section, value in raw_sections.items():
        if str(section) in {"version", "metadata", "schema", "rules"}:
            continue
        if isinstance(value, list):
            count = len([row for row in value if isinstance(row, dict)])
            resolved = _resolve_section_value(
                str(section),
                value,
                context={},
                registry_version=str(registry.get("version") or "") or None,
                effective_at=date.today(),
            )
        elif isinstance(value, dict):
            entries = value.get("entries") or value.get("rules")
            count = len(entries) if isinstance(entries, list) else 1
            resolved = _resolve_section_value(
                str(section),
                value,
                context={},
                registry_version=str(registry.get("version") or "") or None,
                effective_at=date.today(),
            )
        else:
            count = 0
            resolved = RuleResolution(section=str(section), payload={})
        preview["sections"][str(section)] = {
            "rule_count": count,
            "active_rule": resolved.audit_meta(),
            "payload_keys": sorted((resolved.payload or {}).keys())[:50],
        }
    return preview
