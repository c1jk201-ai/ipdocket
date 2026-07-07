"""
Mapping Service: Loads and manages case field mappings from JSON.

This module is responsible for:
1. Loading mappings from JSON config
2. Validating field keys against the registry
3. Providing fields for a given case type with layout info
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.services.case.case_kind import resolve_profile_case_kind, resolve_public_case_kind

from .grouping import apply_default_field_groups_for_mapping_key
from .registry import FieldRegistry

logger = logging.getLogger(__name__)


@dataclass
class FieldMapping:
    """A single field's mapping configuration for a case type."""

    key: str
    order: float
    col: int  # 1 or 2 (for 2-column layout)
    required: bool = False
    group: str = ""
    group_order: float | None = None


@dataclass
class CaseTypeMapping:
    """Complete mapping configuration for a case type."""

    case_type_key: str  # e.g. "DOM:PATENT" or "LITIGATION"
    namespace: str
    fields: List[FieldMapping]
    extra_allowed: Set[str] = field(default_factory=set)


class MappingService:
    """
    Service for loading and querying case field mappings.

    The mapping config (JSON) defines WHICH fields appear for each case type
    and HOW they are laid out. The field DEFINITIONS come from FieldRegistry.
    """

    _instance: Optional["MappingService"] = None
    _mappings: Dict[str, CaseTypeMapping]
    _config_path: str
    _config_mtime: float
    _initialized: bool = False
    _source_meta: dict
    _allow_system_config: bool

    def __new__(cls) -> "MappingService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._mappings = {}
            cls._instance._configured_path = ""
            cls._instance._config_mtime = 0.0
            cls._instance._initialized = False
            cls._instance._source_meta = {}
            cls._instance._allow_system_config = True
        return cls._instance

    @classmethod
    def instance(cls) -> "MappingService":
        return cls()

    def initialize(self, config_path: Optional[str] = None) -> None:
        """
        Load mappings from JSON config.

        Args:
            config_path: Path to JSON config. If None, uses default location.
        """
        explicit_path = config_path is not None
        if config_path is None:
            # Default path relative to app
            base = Path(__file__).parent.parent.parent / "data"
            unified_path = base / "unified_field_registry.json"
            if unified_path.exists():
                config_path = str(unified_path)
            else:
                config_path = str(base / "case_field_mappings.json")

        self._config_path = config_path
        # When a custom config_path is provided, treat it as an explicit file override.
        self._allow_system_config = not explicit_path
        self._load_config()
        self._initialized = True

    def _apply_loaded_config(self, data: Dict[str, Any], meta: Dict[str, Any]) -> None:
        self._source_meta = dict(meta or {})
        self._config_mtime = float(self._source_meta.get("mtime") or 0.0)

        # Initialize registry first
        registry = FieldRegistry.instance()
        registry.reload_if_changed()
        registry.initialize()

        # Support both formats:
        # - New: {"mappings": {...}}
        # - Legacy: {"IP:DOM:PATENT": {...}, ...}
        mappings_data = data.get("mappings")
        if not isinstance(mappings_data, dict):
            mappings_data = data if isinstance(data, dict) else {}

        mappings_out: Dict[str, CaseTypeMapping] = {}

        # First pass: load non-inherited mappings
        for key, mapping in mappings_data.items():
            if not isinstance(mapping, dict):
                continue
            # Treat inherit=None/null as "no inheritance"
            if mapping.get("inherit"):
                continue
            self._load_mapping(mappings_out, key, mapping, registry)

        # Second pass: resolve inheritance
        for key, mapping in mappings_data.items():
            if not isinstance(mapping, dict):
                continue
            parent_key = mapping.get("inherit")
            if not parent_key:
                continue
            if parent_key not in mappings_out:
                logger.error(f"Mapping '{key}' inherits from unknown '{parent_key}'")
                continue
            parent = mappings_out[parent_key]
            mappings_out[key] = CaseTypeMapping(
                case_type_key=key,
                namespace=mapping.get("namespace", parent.namespace),
                fields=list(parent.fields),  # Copy parent fields
                extra_allowed=set(parent.extra_allowed) | set(mapping.get("extra_allowed", [])),
            )

        self._merge_case_menu_mappings(mappings_out, registry)
        self._source_meta.update(self._case_menu_meta())
        self._mappings = mappings_out
        logger.info(f"Loaded {len(self._mappings)} case type mappings")

    def _case_menu_meta(self) -> dict[str, Any]:
        try:
            from app.services.case.case_menu_config import get_case_menu_config

            payload = json.dumps(
                get_case_menu_config(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return {"case_menu_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
        except Exception:
            return {}

    def _merge_case_menu_mappings(
        self,
        mappings_out: Dict[str, CaseTypeMapping],
        registry: FieldRegistry,
    ) -> None:
        """Apply administrator-configured matter create menu field mappings."""
        try:
            from app.services.case.case_menu_config import get_case_menu_mapping_overrides
        except Exception:
            return

        for override in get_case_menu_mapping_overrides():
            if not isinstance(override, dict):
                continue
            key = str(override.get("key") or "").strip().upper()
            if not key:
                continue

            inherit_key = str(override.get("inherit") or "").strip().upper()
            parent = mappings_out.get(inherit_key) if inherit_key else None
            fields = override.get("fields") if isinstance(override.get("fields"), list) else []
            namespace = str(override.get("namespace") or "").strip()
            extra_allowed = set(override.get("extra_allowed") or [])

            if key == inherit_key and not fields and not extra_allowed:
                continue

            if fields:
                mapping_payload = {
                    "namespace": namespace or (parent.namespace if parent else ""),
                    "fields": fields,
                    "extra_allowed": sorted(extra_allowed | (parent.extra_allowed if parent else set())),
                }
                self._load_mapping(mappings_out, key, mapping_payload, registry)
                continue

            if parent:
                mappings_out[key] = CaseTypeMapping(
                    case_type_key=key,
                    namespace=namespace or parent.namespace,
                    fields=list(parent.fields),
                    extra_allowed=set(parent.extra_allowed) | extra_allowed,
                )

    def _load_config(self) -> None:
        """Load and parse JSON config with validation."""
        from .unified_config import load_unified_registry_data

        data, meta = load_unified_registry_data(
            self._config_path,
            allow_system_config=self._allow_system_config,
        )
        if not isinstance(data, dict):
            if not os.path.exists(self._config_path):
                logger.error(f"Mapping config not found: {self._config_path}")
            return
        self._apply_loaded_config(data, meta)

    def _load_mapping(
        self,
        target: Dict[str, CaseTypeMapping],
        key: str,
        mapping: Dict[str, Any],
        registry: FieldRegistry,
    ) -> None:
        """Load a single mapping configuration."""
        fields_data = mapping.get("fields", [])
        if not isinstance(fields_data, list):
            fields_data = []

        dedup: Dict[str, FieldMapping] = {}
        duplicate_keys: Set[str] = set()
        invalid_keys: List[str] = []

        for fd in fields_data:
            if not isinstance(fd, dict):
                continue
            field_key = fd.get("key", "")
            if not field_key:
                continue

            # Validate key exists in registry
            if not registry.exists(field_key):
                invalid_keys.append(field_key)
                continue

            try:
                order = float(fd.get("order", 0) or 0)
            except Exception:
                order = 0.0
            try:
                col = int(fd.get("col", 1) or 1)
            except Exception:
                col = 1
            if col not in (1, 2):
                col = 1

            required = bool(fd.get("required", False))
            group = str(fd.get("group") or fd.get("section") or "").strip()
            try:
                group_order = (
                    float(fd.get("group_order", fd.get("section_order")))
                    if fd.get("group_order", fd.get("section_order")) not in (None, "")
                    else None
                )
            except Exception:
                group_order = None

            existing = dedup.get(field_key)
            if existing:
                duplicate_keys.add(field_key)
                merged_required = bool(existing.required) or required
                # Prefer the earliest layout position when duplicates exist.
                if (order, col) < (existing.order, existing.col):
                    dedup[field_key] = FieldMapping(
                        key=field_key,
                        order=order,
                        col=col,
                        required=merged_required,
                        group=group or existing.group,
                        group_order=group_order if group_order is not None else existing.group_order,
                    )
                else:
                    dedup[field_key] = FieldMapping(
                        key=field_key,
                        order=existing.order,
                        col=existing.col,
                        required=merged_required,
                        group=existing.group or group,
                        group_order=existing.group_order if existing.group_order is not None else group_order,
                    )
                continue

            dedup[field_key] = FieldMapping(
                key=field_key,
                order=order,
                col=col,
                required=required,
                group=group,
                group_order=group_order,
            )

        if invalid_keys:
            logger.warning(f"Mapping '{key}' references undefined fields: {invalid_keys}")
        if duplicate_keys:
            logger.warning(
                "Mapping '%s' has duplicate field keys (deduped): %s",
                key,
                sorted(duplicate_keys),
            )

        field_rows = apply_default_field_groups_for_mapping_key(
            [
                {
                    "key": fm.key,
                    "order": fm.order,
                    "col": fm.col,
                    "required": fm.required,
                    "group": fm.group,
                    "group_order": fm.group_order,
                }
                for fm in sorted(dedup.values(), key=lambda f: (f.order, f.col))
            ],
            key,
        )

        target[key] = CaseTypeMapping(
            case_type_key=key,
            namespace=mapping.get("namespace", ""),
            fields=[
                FieldMapping(
                    key=str(field.get("key") or ""),
                    order=float(field.get("order") or 0),
                    col=2 if int(field.get("col") or 1) == 2 else 1,
                    required=bool(field.get("required", False)),
                    group=str(field.get("group") or ""),
                    group_order=(
                        float(field.get("group_order"))
                        if field.get("group_order") not in (None, "")
                        else None
                    ),
                )
                for field in field_rows
            ],
            extra_allowed=set(mapping.get("extra_allowed", [])),
        )

    def reload_if_changed(self) -> bool:
        """
        Reload config if file has been modified.

        Returns:
            True if config was reloaded
        """
        from .unified_config import load_unified_registry_data

        data, meta = load_unified_registry_data(
            self._config_path,
            allow_system_config=self._allow_system_config,
        )
        if not isinstance(data, dict):
            return False

        next_meta = dict(meta or {})
        next_meta.update(self._case_menu_meta())
        if next_meta == (self._source_meta or {}):
            return False

        logger.info("Mapping config changed, reloading...")
        self._apply_loaded_config(data, next_meta)
        return True

    def get_mapping(self, division: str, case_type: str) -> Optional[CaseTypeMapping]:
        """
        Get mapping for a case type.

        Args:
            division: DOM, INC, OUT, or empty for LITIGATION
            case_type: PATENT, DESIGN, TRADEMARK, or LITIGATION

        Returns:
            CaseTypeMapping or None if not found
        """
        if not self._initialized:
            self.initialize()

        raw_div = (division or "").strip().upper()
        try:
            from app.services.case.case_menu_config import (
                find_case_menu_item,
                normalize_case_menu_division,
                normalize_case_menu_type,
            )

            menu_div = normalize_case_menu_division(division)
            menu_typ = normalize_case_menu_type(case_type)
        except Exception:
            find_case_menu_item = None
            menu_div = raw_div
            menu_typ = (case_type or "").strip().upper()

        direct_candidates = []
        if menu_div and menu_typ:
            direct_candidates.extend([f"IP:{menu_div}:{menu_typ}", f"{menu_div}:{menu_typ}"])
        if menu_typ:
            direct_candidates.extend([menu_typ, f"IP:{menu_typ}"])
        for key in direct_candidates:
            mapping = self._mappings.get(key)
            if mapping:
                return mapping

        public_div, public_typ = resolve_public_case_kind(division, case_type)
        div, typ = resolve_profile_case_kind(division, case_type)

        if public_typ in ("LITIGATION", "MISC") or typ in ("LITIGATION", "MISC"):
            for key in (
                f"IP:{public_div}:{public_typ}" if public_div and public_typ else "",
                f"{public_div}:{public_typ}" if public_div and public_typ else "",
                public_typ,
                f"IP:{public_typ}" if public_typ else "",
                typ,
                f"IP:{typ}" if typ else "",
            ):
                if not key:
                    continue
                mapping = self._mappings.get(key)
                if mapping:
                    return mapping
            return None

        if not public_typ and not typ:
            return None

        # Prefer business-area specific keys (legacy config uses "IP:...")
        candidates = []
        if public_div and public_typ:
            candidates.extend([f"IP:{public_div}:{public_typ}", f"{public_div}:{public_typ}"])
        if div and typ:
            candidates.extend([f"IP:{div}:{typ}", f"{div}:{typ}"])
        for key in candidates:
            mapping = self._mappings.get(key)
            if mapping:
                return mapping

        if find_case_menu_item:
            try:
                item = find_case_menu_item(division, case_type)
            except Exception:
                item = None
            if item:
                profile_div = str(item.get("profile_division") or "").strip().upper()
                profile_typ = str(item.get("profile_type") or "").strip().upper()
                profile_candidates = []
                if profile_div and profile_typ:
                    profile_candidates.extend(
                        [f"IP:{profile_div}:{profile_typ}", f"{profile_div}:{profile_typ}"]
                    )
                if profile_typ:
                    profile_candidates.extend([profile_typ, f"IP:{profile_typ}"])
                for key in profile_candidates:
                    mapping = self._mappings.get(key)
                    if mapping:
                        return mapping
        return None

    def get_fields_for_case(self, division: str, case_type: str) -> List[Dict[str, Any]]:
        """
        Get ordered field list with all metadata for rendering.

        Returns list of dicts with:
            - key, label, input_type, order, col, required
            - options_source (if select)
        """
        mapping = self.get_mapping(division, case_type)
        if not mapping:
            return []

        registry = FieldRegistry.instance()
        result = []

        for fm in mapping.fields:
            field_def = registry.get(fm.key)
            if not field_def:
                continue

            result.append(
                {
                    "key": fm.key,
                    "label": field_def.label,
                    "input_type": field_def.input_type,
                    "order": fm.order,
                    "col": fm.col,
                    "required": fm.required,
                    "group": fm.group,
                    "group_order": fm.group_order,
                    "options_source": field_def.options_source,
                    "options": list(field_def.options or []),
                    "help_text": field_def.help_text,
                    "serializer": field_def.serializer,
                    "default_value": field_def.default_value,
                    "deprecated": field_def.deprecated,
                }
            )

        return result

    def get_allowed_keys(self, division: str, case_type: str) -> Set[str]:
        """Get all allowed field keys for a case type."""
        mapping = self.get_mapping(division, case_type)
        if not mapping:
            return set()

        keys = {fm.key for fm in mapping.fields if fm.key != "__blank__"}
        keys.update(mapping.extra_allowed)
        return keys

    def get_required_keys(self, division: str, case_type: str) -> Set[str]:
        """Get required field keys for a case type."""
        mapping = self.get_mapping(division, case_type)
        if not mapping:
            return set()

        return {fm.key for fm in mapping.fields if fm.required}

    def validate_required_fields(
        self, form_data: Dict[str, Any], division: str, case_type: str
    ) -> List[Dict[str, str]]:
        """
        Check which required fields are missing.

        Returns:
            List of dicts with missing field keys and labels.
        """
        mapping = self.get_mapping(division, case_type)
        if not mapping:
            return []

        registry = FieldRegistry.instance()
        missing: List[Dict[str, str]] = []

        for fm in mapping.fields:
            if not fm.required:
                continue

            value = form_data.get(fm.key)
            if not value or (isinstance(value, str) and not value.strip()):
                field_def = registry.get(fm.key)
                label = field_def.label if field_def else fm.key
                missing.append({"key": fm.key, "label": label})

        return missing

    def get_namespace(self, division: str, case_type: str) -> str:
        """Get the storage namespace for a case type."""
        mapping = self.get_mapping(division, case_type)
        return mapping.namespace if mapping else ""


# Convenience functions
def get_fields_for_case(division: str, case_type: str) -> List[Dict[str, Any]]:
    """Get field list for a case type."""
    return MappingService.instance().get_fields_for_case(division, case_type)


def validate_required_fields(
    form_data: Dict[str, Any], division: str, case_type: str
) -> List[Dict[str, str]]:
    """Validate required fields and return missing field details."""
    return MappingService.instance().validate_required_fields(form_data, division, case_type)


def get_allowed_keys(division: str, case_type: str) -> Set[str]:
    """Get allowed field keys for filtering form data."""
    return MappingService.instance().get_allowed_keys(division, case_type)
