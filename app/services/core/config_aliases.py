from __future__ import annotations

from typing import Any, Mapping, MutableMapping

CONFIG_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "STAFF_PROFESSIONAL_ROLES": ("STAFF_ATTORNEY_ROLES",),
    "STAFF_ATTORNEY_ROLES": ("STAFF_PROFESSIONAL_ROLES",),
}


def _has_nonblank_value(value: object) -> bool:
    return bool(str(value or "").strip())


def expand_config_update_aliases(data: Mapping[str, Any]) -> dict[str, Any]:
    expanded = dict(data)
    for key, aliases in CONFIG_KEY_ALIASES.items():
        if key not in expanded:
            continue
        for alias in aliases:
            expanded.setdefault(alias, expanded[key])
    return expanded


def expand_config_delete_aliases(key: str) -> set[str]:
    normalized = str(key or "").strip()
    if not normalized:
        return set()
    return {normalized, *CONFIG_KEY_ALIASES.get(normalized, ())}


def apply_config_read_aliases(data: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, aliases in CONFIG_KEY_ALIASES.items():
        if not _has_nonblank_value(data.get(key)):
            continue
        for alias in aliases:
            if not _has_nonblank_value(data.get(alias)):
                data[alias] = data[key]
    return data
