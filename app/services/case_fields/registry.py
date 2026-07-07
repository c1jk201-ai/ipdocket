"""
Field Registry: Central catalog of all field definitions.

This is the "source of truth" for what fields exist and what they are.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .field_types import (
    FieldDefinition,
    date_format_validator,
    application_number_validator,
    max_length_validator,
    regex_validator,
    required_validator,
)
from .labels import coerce_field_label

logger = logging.getLogger(__name__)


def _normalize_option_items(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        value = ""
        label = ""
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            label = str(item.get("label") or value).strip()
        elif isinstance(item, (list, tuple)) and item:
            value = str(item[0] or "").strip()
            label = str(item[1] if len(item) > 1 else item[0]).strip()
        elif item is not None:
            value = str(item).strip()
            label = value
        if not value or value in seen:
            continue
        options.append({"value": value, "label": label or value})
        seen.add(value)
    return options


class FieldRegistry:
    """
    Singleton registry holding all field definitions.

    Usage:
        registry = FieldRegistry.instance()
        field = registry.get("manager")
    """

    _instance: Optional["FieldRegistry"] = None
    _fields: Dict[str, FieldDefinition]
    _initialized: bool = False
    _unified_meta: dict | None = None

    def __new__(cls) -> "FieldRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._fields = {}
            cls._instance._initialized = False
            cls._instance._unified_meta = None
        return cls._instance

    @classmethod
    def instance(cls) -> "FieldRegistry":
        """Get the singleton instance."""
        return cls()

    def register(self, field: FieldDefinition) -> None:
        """Register a field definition."""
        if field.key in self._fields:
            logger.warning(f"Field '{field.key}' already registered, overwriting")
        self._fields[field.key] = field

    def register_many(self, fields: list[FieldDefinition]) -> None:
        """Register multiple field definitions."""
        for f in fields:
            self.register(f)

    def get(self, key: str) -> Optional[FieldDefinition]:
        """Get a field definition by key."""
        return self._fields.get(key)

    def exists(self, key: str) -> bool:
        """Check if a field key exists."""
        return key in self._fields

    def validate_keys(self, keys: list[str]) -> list[str]:
        """
        Validate that all keys exist in registry.
        Returns list of invalid keys.
        """
        return [k for k in keys if not self.exists(k)]

    def all_keys(self) -> set[str]:
        """Get all registered field keys."""
        return set(self._fields.keys())

    def all_fields(self) -> dict[str, FieldDefinition]:
        """Get all registered field definitions keyed by field key."""
        return dict(self._fields)

    def deprecated_keys(self) -> set[str]:
        """Get all deprecated field keys."""
        return {k for k, v in self._fields.items() if v.deprecated}

    def initialize(self) -> None:
        """
        Load field definitions from unified registry (preferred) or legacy modules.
        Called once at app startup.
        """
        if self._initialized:
            return

        if self._load_definitions_from_unified():
            self._initialized = True
            logger.info(f"FieldRegistry initialized with {len(self._fields)} fields")
            return

        # Fallback: Import all definition modules to trigger registration
        from .definitions import common, design, litigation, patent, pct, trademark

        # Fallback: Auto-register fields from CSV
        try:
            import csv
            import os

            # registry.py is in app/services/case_fields
            # csv is in app/data
            curr_dir = os.path.dirname(__file__)
            csv_path = os.path.abspath(
                os.path.join(curr_dir, "..", "..", "data", "case_parameter_mapping.csv")
            )

            if os.path.exists(csv_path):
                with open(csv_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    fallback_count = 0
                    for row in reader:
                        key = row.get("param_key")
                        if key and not self.exists(key):
                            self.register(
                                FieldDefinition(
                                    key=key,
                                    label=coerce_field_label(key, row.get("label") or key),
                                    input_type=row.get("widget") or "text",
                                    help_text="Auto-registered (Fallback)",
                                )
                            )
                            fallback_count += 1
                    if fallback_count:
                        logger.info(
                            f"Fallback: Registered {fallback_count} missing fields from CSV"
                        )
        except Exception as e:
            logger.error(f"Fallback registration failed: {e}")

        self._initialized = True
        logger.info(f"FieldRegistry initialized with {len(self._fields)} fields")

    def _load_definitions_from_unified_data(self, data: dict) -> bool:
        definitions = data.get("field_definitions") or data.get("definitions")
        if not isinstance(definitions, (dict, list)):
            return False

        items: Iterable[tuple[str, Any]]
        if isinstance(definitions, dict):
            items = definitions.items()
        else:
            items = [(d.get("key"), d) for d in definitions if isinstance(d, dict)]

        for key, info in items:
            if not key or not isinstance(info, dict):
                continue
            self.register(
                FieldDefinition(
                    key=key,
                    label=coerce_field_label(key, info.get("label") or key),
                    input_type=info.get("input_type") or "text",
                    validators=self._parse_validator_specs(info.get("validators")),
                    options_source=info.get("options_source"),
                    options=_normalize_option_items(info.get("options")),
                    help_text=info.get("help_text") or "",
                    serializer=info.get("serializer") or "string",
                    deprecated=bool(info.get("deprecated")),
                    default_value=info.get("default_value"),
                )
            )

        return bool(self._fields)

    def _load_definitions_from_unified(self) -> bool:
        from .unified_config import load_unified_registry_data

        data, meta = load_unified_registry_data()
        if not isinstance(data, dict):
            return False

        ok = self._load_definitions_from_unified_data(data)
        if ok:
            self._unified_meta = dict(meta or {})
        return ok

    def _parse_validator_specs(self, raw: Any) -> list:
        if not raw:
            return []
        if isinstance(raw, (str, dict)):
            specs = [raw]
        elif isinstance(raw, list):
            specs = raw
        else:
            logger.warning(f"Unsupported validators spec: {raw}")
            return []

        validators = []
        for spec in specs:
            if isinstance(spec, str):
                validator = self._validator_from_name(spec)
                if validator:
                    validators.append(validator)
                else:
                    logger.warning(f"Unknown validator name: {spec}")
                continue
            if isinstance(spec, dict):
                vtype = (spec.get("type") or "").strip()
                validator = self._validator_from_spec(vtype, spec)
                if validator:
                    validators.append(validator)
                else:
                    logger.warning(f"Unknown validator spec: {spec}")
                continue
            logger.warning(f"Invalid validator entry: {spec}")

        return validators

    def _validator_from_name(self, name: str):
        lookup = {
            "required": required_validator,
            "date_format": date_format_validator,
            "application_number": application_number_validator,
        }
        return lookup.get((name or "").strip())

    def _validator_from_spec(self, vtype: str, spec: dict):
        vtype = (vtype or "").strip()
        if vtype == "required":
            return required_validator
        if vtype == "date_format":
            return date_format_validator
        if vtype == "application_number":
            return application_number_validator
        if vtype == "max_length":
            try:
                return max_length_validator(int(spec.get("value")))
            except (TypeError, ValueError):
                return None
        if vtype == "regex":
            pattern = spec.get("pattern")
            message = spec.get("message") or "Invalid format"
            if not pattern:
                return None
            return regex_validator(pattern, message)
        return None

    def reset(self) -> None:
        """Reset registry (for testing only)."""
        self._fields.clear()
        self._initialized = False
        self._unified_meta = None

    def reload_if_changed(self) -> bool:
        """
        Reload unified registry if the underlying config changed.

        Note: This is best-effort and intended for long-running processes that
        support runtime config updates (SystemConfig) without redeploy.
        """
        from .unified_config import load_unified_registry_data

        data, meta = load_unified_registry_data()
        if not isinstance(data, dict):
            return False

        if self._unified_meta and dict(meta or {}) == self._unified_meta:
            return False

        self._fields.clear()
        self._initialized = False
        self._unified_meta = None

        ok = self._load_definitions_from_unified_data(data)
        if not ok:
            return False
        self._unified_meta = dict(meta or {})
        self._initialized = True
        logger.info("FieldRegistry reloaded from %s", (meta or {}).get("source"))
        return True


# Convenience function
def get_field(key: str) -> Optional[FieldDefinition]:
    """Get a field definition from the global registry."""
    return FieldRegistry.instance().get(key)
