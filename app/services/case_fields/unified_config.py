from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

UNIFIED_FIELD_REGISTRY_KEY = "UNIFIED_FIELD_REGISTRY_JSON"


def _default_unified_registry_path() -> Path:
    return Path(__file__).parent.parent.parent / "data" / "unified_field_registry.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        payload = str(value)
    return _sha256_text(payload)


def _merge_list_unique(base: Any, override: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in (base if isinstance(base, list) else []) + (
        override if isinstance(override, list) else []
    ):
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker not in seen:
            out.append(value)
            seen.add(marker)
    return out


def _merge_mapping_config(base: Any, override: Any) -> dict[str, Any]:
    if not isinstance(base, dict):
        base = {}
    if not isinstance(override, dict):
        override = {}

    merged = dict(base)
    merged.update({k: v for k, v in override.items() if k not in {"fields", "extra_allowed"}})

    base_fields = base.get("fields") if isinstance(base.get("fields"), list) else []
    override_fields = override.get("fields") if isinstance(override.get("fields"), list) else None
    if override_fields:
        merged["fields"] = override_fields
    elif base_fields:
        merged["fields"] = base_fields
    elif override_fields is not None:
        merged["fields"] = override_fields

    merged["extra_allowed"] = _merge_list_unique(
        base.get("extra_allowed"),
        override.get("extra_allowed"),
    )
    return merged


def _merge_with_file_baseline(data: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Treat SystemConfig registry JSON as an override, not a replacement.

    The bundled file is the safety baseline for case-create parameters. Missing baseline
    definitions/mappings are restored so a partial admin save cannot erase standard matter
    parameters.
    """
    if not isinstance(data, dict) or not isinstance(baseline, dict):
        return data

    merged = dict(baseline)
    merged.update({k: v for k, v in data.items() if k not in {"field_definitions", "mappings"}})

    baseline_defs = (
        baseline.get("field_definitions")
        if isinstance(baseline.get("field_definitions"), dict)
        else {}
    )
    override_defs = (
        data.get("field_definitions") if isinstance(data.get("field_definitions"), dict) else {}
    )
    merged["field_definitions"] = {**baseline_defs, **override_defs}

    baseline_mappings = (
        baseline.get("mappings") if isinstance(baseline.get("mappings"), dict) else {}
    )
    override_mappings = data.get("mappings") if isinstance(data.get("mappings"), dict) else {}
    mapping_keys = set(baseline_mappings) | set(override_mappings)
    merged["mappings"] = {
        key: _merge_mapping_config(baseline_mappings.get(key), override_mappings.get(key))
        for key in sorted(mapping_keys)
    }
    return merged


def _load_from_system_config() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        from flask import has_app_context

        if not has_app_context():
            return None, {"source": "system_config", "enabled": False}
        from app.services.core.config_service import ConfigService

        raw = ConfigService.get_raw(UNIFIED_FIELD_REGISTRY_KEY, None, allow_blank=False)
        if raw is None:
            return None, {"source": "system_config", "enabled": True, "present": False}
        data = ConfigService.get_json(UNIFIED_FIELD_REGISTRY_KEY, None)
        if not isinstance(data, dict):
            logger.warning(
                "Invalid %s: expected JSON object but got %s",
                UNIFIED_FIELD_REGISTRY_KEY,
                type(data).__name__,
            )
            return None, {"source": "system_config", "enabled": True, "present": True}

        digest = _sha256_text(raw) if isinstance(raw, str) else _sha256_json(data)
        return data, {
            "source": "system_config",
            "key": UNIFIED_FIELD_REGISTRY_KEY,
            "sha256": digest,
        }
    except Exception as exc:
        logger.warning("Failed to load unified registry from SystemConfig: %s", exc)
        return None, {"source": "system_config", "enabled": True, "error": str(exc)}


def _load_from_file(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.exists():
        return None, {"source": "file", "path": str(path), "present": False}

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.error("Unified registry file is not a JSON object: %s", path)
            return None, {"source": "file", "path": str(path), "present": True}
        mtime = os.path.getmtime(str(path))
        return data, {
            "source": "file",
            "path": str(path),
            "mtime": mtime,
            "sha256": _sha256_text(raw),
        }
    except Exception as exc:
        logger.error("Failed to load unified registry file %s: %s", path, exc)
        return None, {"source": "file", "path": str(path), "present": True, "error": str(exc)}


def load_unified_registry_data(
    config_path: str | None = None,
    *,
    allow_system_config: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """
    Load unified field registry JSON from the best available source.

    Precedence (when allowed):
    1) SystemConfig / app config / env via ConfigService key `UNIFIED_FIELD_REGISTRY_JSON`
    2) Filesystem JSON (default: `app/data/unified_field_registry.json`)
    """
    path = Path(config_path) if config_path else _default_unified_registry_path()
    if allow_system_config:
        data, meta = _load_from_system_config()
        if isinstance(data, dict):
            baseline, baseline_meta = _load_from_file(path)
            if isinstance(baseline, dict):
                merged = _merge_with_file_baseline(data, baseline)
                meta = {
                    **meta,
                    "baseline_source": baseline_meta.get("source"),
                    "baseline_path": baseline_meta.get("path"),
                    "baseline_sha256": baseline_meta.get("sha256"),
                    "baseline_merged": True,
                }
                return merged, meta
            return data, meta

    return _load_from_file(path)
