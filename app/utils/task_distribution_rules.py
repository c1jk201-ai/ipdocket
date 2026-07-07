from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)

_RULES_CACHE: dict[str, dict[str, Any]] = {}
_RULES_CACHE_LOCK = threading.RLock()
_ALLOWED_DISTRIBUTE_TO = {"owner", "role_set", "all_staff", "none"}
_ALLOWED_MATCH_FIELDS = {
    "category",
    "source",
    "name_ref_exact",
    "name_ref_prefix",
    "name_ref_contains",
    "name_ref_regex",
    "name_free_contains",
    "name_free_regex",
}


@dataclass(frozen=True)
class DistributionDecision:
    distribute_to: str
    role_codes: tuple[str, ...] = ()
    rule_id: str | None = None
    priority: int | None = None


def _rules_path() -> str:
    if has_app_context():
        configured = current_app.config.get("TASK_DISTRIBUTION_RULES_PATH")
        if configured:
            return configured
    env_path = os.environ.get("TASK_DISTRIBUTION_RULES_PATH")
    if env_path:
        return env_path
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_dir, "data", "task_distribution_rules.json")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_list(value: Any, *, upper: bool = False, lower: bool = False) -> tuple[str, ...]:
    out: list[str] = []
    for item in _as_list(value):
        s = str(item).strip()
        if not s:
            continue
        if upper:
            s = s.upper()
        elif lower:
            s = s.lower()
        out.append(s)
    return tuple(out)


def _compile_regex_list(values: Any) -> tuple[tuple[re.Pattern[str], ...], tuple[str, ...]]:
    patterns: list[re.Pattern[str]] = []
    invalid: list[str] = []
    for raw in _as_list(values):
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            patterns.append(re.compile(raw, re.IGNORECASE))
        except re.error as e:
            logger.error("Invalid distribution rule regex '%s': %s", raw, e)
            invalid.append(raw)
    return tuple(patterns), tuple(invalid)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _truncate_for_log(value: str | None, *, limit: int = 160) -> str:
    s = (value or "").strip()
    if len(s) <= limit:
        return s
    return f"{s[:limit]}..."


def _distribution_audit_enabled() -> bool:
    if has_app_context():
        return _as_bool(current_app.config.get("TASK_DISTRIBUTION_AUDIT_LOG"), default=False)
    return _as_bool(os.environ.get("TASK_DISTRIBUTION_AUDIT_LOG"), default=False)


def _emit_distribution_audit(
    *,
    rule_id: str | None,
    priority: int | None,
    distribute_to: str,
    role_codes: tuple[str, ...],
    category: str,
    source: str,
    name_ref: str,
    name_free: str,
) -> None:
    if not _distribution_audit_enabled():
        return
    logger.info(
        "task_distribution_audit rule_id=%s priority=%s distribute_to=%s roles=%s category=%s source=%s name_ref=%s name_free=%s",
        rule_id or "default_action",
        priority,
        distribute_to,
        ",".join(role_codes) if role_codes else "-",
        category or "-",
        source or "-",
        _truncate_for_log(name_ref),
        _truncate_for_log(name_free),
    )


def _normalize_action(action: dict[str, Any] | None) -> dict[str, Any]:
    action = action or {}
    distribute_to = str(action.get("distribute_to") or "owner").strip().lower()
    roles = _normalize_list(action.get("roles"), lower=True)
    return {"distribute_to": distribute_to, "roles": roles}


def _validate_action(action: dict[str, Any]) -> tuple[bool, str | None]:
    distribute_to = str(action.get("distribute_to") or "").strip().lower()
    if distribute_to not in _ALLOWED_DISTRIBUTE_TO:
        return False, f"invalid distribute_to {distribute_to!r}"
    if distribute_to == "role_set" and not action.get("roles"):
        return False, "role_set requires non-empty roles"
    return True, None


def _normalize_rule(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        return None
    match = raw.get("match") or {}
    if not isinstance(match, dict):
        match = {}
    unknown_match_keys = sorted(set(match.keys()) - _ALLOWED_MATCH_FIELDS)
    if unknown_match_keys:
        logger.error(
            "Invalid distribution rule '%s': unknown match fields: %s",
            rule_id,
            ", ".join(unknown_match_keys),
        )
        return None

    action = _normalize_action(raw.get("action"))
    action_ok, action_err = _validate_action(action)
    if not action_ok:
        logger.error("Invalid distribution rule '%s': %s", rule_id, action_err)
        return None

    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0

    name_ref_regex, invalid_ref_regex = _compile_regex_list(match.get("name_ref_regex"))
    name_free_regex, invalid_free_regex = _compile_regex_list(match.get("name_free_regex"))
    if invalid_ref_regex or invalid_free_regex:
        invalid_values = ", ".join([*invalid_ref_regex, *invalid_free_regex])
        logger.error(
            "Invalid distribution rule '%s': regex compile failure(s): %s",
            rule_id,
            invalid_values,
        )
        return None

    compiled = {
        "id": rule_id,
        "enabled": _as_bool(raw.get("enabled"), default=True),
        "priority": priority,
        "action": action,
        "match": {
            "category": _normalize_list(match.get("category"), upper=True),
            "source": _normalize_list(match.get("source"), lower=True),
            "name_ref_exact": _normalize_list(match.get("name_ref_exact"), upper=True),
            "name_ref_prefix": _normalize_list(match.get("name_ref_prefix"), upper=True),
            "name_ref_contains": _normalize_list(match.get("name_ref_contains"), upper=True),
            "name_ref_regex": name_ref_regex,
            "name_free_contains": _normalize_list(match.get("name_free_contains"), upper=True),
            "name_free_regex": name_free_regex,
        },
    }
    return compiled


def load_distribution_rules() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = _rules_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None

    with _RULES_CACHE_LOCK:
        cached = _RULES_CACHE.get(path)
        if cached and cached.get("mtime") == mtime:
            return cached["rules"], cached["default_action"]

    if not os.path.exists(path):
        default_action = _normalize_action({})
        with _RULES_CACHE_LOCK:
            _RULES_CACHE[path] = {"mtime": mtime, "rules": [], "default_action": default_action}
        return [], default_action

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("Failed to load task distribution rules from %s: %s", path, exc)
        default_action = _normalize_action({})
        with _RULES_CACHE_LOCK:
            _RULES_CACHE[path] = {"mtime": mtime, "rules": [], "default_action": default_action}
        return [], default_action

    rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for raw in data.get("rules", []) if isinstance(data, dict) else []:
        normalized = _normalize_rule(raw)
        if normalized:
            if not normalized.get("enabled", True):
                continue
            rid = str(normalized.get("id") or "").strip()
            if rid and rid in seen_rule_ids:
                logger.error(
                    "Duplicate task distribution rule id '%s' in %s; skipping later entry",
                    rid,
                    path,
                )
                continue
            if rid:
                seen_rule_ids.add(rid)
            rules.append(normalized)

    rules.sort(key=lambda r: r["priority"], reverse=True)
    default_action = _normalize_action(data.get("default_action") if isinstance(data, dict) else {})
    default_action_ok, default_action_err = _validate_action(default_action)
    if not default_action_ok:
        logger.error(
            "Invalid task distribution default_action in %s: %s; falling back to owner",
            path,
            default_action_err,
        )
        default_action = _normalize_action({})
    with _RULES_CACHE_LOCK:
        _RULES_CACHE[path] = {"mtime": mtime, "rules": rules, "default_action": default_action}
    return rules, default_action


def resolve_distribution_decision(
    *,
    category: str | None,
    name_ref: str | None,
    name_free: str | None,
    source: str | None = None,
) -> DistributionDecision:
    rules, default_action = load_distribution_rules()
    cat = (category or "").strip().upper()
    ref = (name_ref or "").strip()
    ref_upper = ref.upper()
    free = (name_free or "").strip()
    free_upper = free.upper()
    ref_compact = _compact_text(ref_upper)
    free_compact = _compact_text(free_upper)
    src = (source or "").strip().lower()

    for rule in rules:
        match = rule["match"]
        if match["category"] and cat not in match["category"]:
            continue
        if match["source"] and src not in match["source"]:
            continue
        if match["name_ref_exact"] and ref_upper not in match["name_ref_exact"]:
            continue
        if match["name_ref_prefix"] and not any(
            ref_upper.startswith(p) or ref_compact.startswith(_compact_text(p))
            for p in match["name_ref_prefix"]
        ):
            continue
        if match["name_ref_contains"] and not any(
            k in ref_upper or _compact_text(k) in ref_compact for k in match["name_ref_contains"]
        ):
            continue
        if match["name_ref_regex"] and not any(p.search(ref) for p in match["name_ref_regex"]):
            continue
        if match["name_free_contains"] and not any(
            k in free_upper or _compact_text(k) in free_compact for k in match["name_free_contains"]
        ):
            continue
        if match["name_free_regex"] and not any(p.search(free) for p in match["name_free_regex"]):
            continue

        action = rule["action"]
        decision = DistributionDecision(
            distribute_to=action["distribute_to"],
            role_codes=tuple(action.get("roles", ())),
            rule_id=rule["id"],
            priority=rule["priority"],
        )
        _emit_distribution_audit(
            rule_id=decision.rule_id,
            priority=decision.priority,
            distribute_to=decision.distribute_to,
            role_codes=decision.role_codes,
            category=cat,
            source=src,
            name_ref=ref,
            name_free=free,
        )
        return decision

    decision = DistributionDecision(
        distribute_to=default_action["distribute_to"],
        role_codes=tuple(default_action.get("roles", ())),
        rule_id=None,
        priority=None,
    )
    _emit_distribution_audit(
        rule_id=decision.rule_id,
        priority=decision.priority,
        distribute_to=decision.distribute_to,
        role_codes=decision.role_codes,
        category=cat,
        source=src,
        name_ref=ref,
        name_free=free,
    )
    return decision


def validate_rules(raise_on_error: bool = False) -> tuple[bool, list[str]]:
    """
    Validate all loaded rules and return status and list of errors.
    If raise_on_error is True, raises ValueError on the first error found.
    """
    errors = []
    try:
        load_distribution_rules()
    except Exception as e:
        errors.append(f"Failed to load rules file: {e}")
        if raise_on_error:
            raise

    # Re-check regexes explicitly since load_distribution_rules swallows them
    # We need to manually inspect the raw JSON to find invalid regexes
    path = _rules_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                default_action = _normalize_action((data or {}).get("default_action"))
                if default_action.get("distribute_to") not in _ALLOWED_DISTRIBUTE_TO:
                    errors.append(
                        "default_action.distribute_to must be one of "
                        f"{sorted(_ALLOWED_DISTRIBUTE_TO)}: "
                        f"{default_action.get('distribute_to')!r}"
                    )
                if default_action.get("distribute_to") == "role_set" and not default_action.get(
                    "roles"
                ):
                    errors.append("default_action.role_set must define non-empty roles")

                seen_ids: set[str] = set()
                for i, rule in enumerate(data.get("rules", [])):
                    rule_id = str(rule.get("id") or "").strip()
                    if not rule_id:
                        errors.append(f"Rule #{i} is missing id")
                    elif rule_id in seen_ids:
                        errors.append(f"Duplicate rule id: {rule_id}")
                    else:
                        seen_ids.add(rule_id)

                    match = rule.get("match") or {}
                    unknown_match_keys = sorted(set(match.keys()) - _ALLOWED_MATCH_FIELDS)
                    if unknown_match_keys:
                        errors.append(
                            f"Rule #{i} ({rule.get('id')}) has unknown match fields: "
                            f"{', '.join(unknown_match_keys)}"
                        )

                    action = _normalize_action(rule.get("action"))
                    distribute_to = action.get("distribute_to")
                    if distribute_to not in _ALLOWED_DISTRIBUTE_TO:
                        errors.append(
                            f"Rule #{i} ({rule.get('id')}) has invalid distribute_to "
                            f"{distribute_to!r}; allowed={sorted(_ALLOWED_DISTRIBUTE_TO)}"
                        )
                    if distribute_to == "role_set" and not action.get("roles"):
                        errors.append(f"Rule #{i} ({rule.get('id')}) uses role_set without roles")

                    match = rule.get("match") or {}
                    for field in ("name_ref_regex", "name_free_regex"):
                        for raw in _as_list(match.get(field)):
                            if not raw:
                                continue
                            try:
                                re.compile(raw, re.IGNORECASE)
                            except re.error as e:
                                msg = (
                                    f"Rule #{i} ({rule.get('id')}) has invalid {field} '{raw}': {e}"
                                )
                                errors.append(msg)
        except Exception as e:
            errors.append(f"Error inspecting raw rules file: {e}")

    if errors:
        logger.error("Task distribution rules validation failed: %s", errors)
        if raise_on_error:
            raise ValueError(f"Task distribution rules validation failed: {errors}")
        return False, errors
    return True, []
