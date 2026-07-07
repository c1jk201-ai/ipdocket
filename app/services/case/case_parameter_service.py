"""
CaseParameterService - Facade for case parameter management.

This service now delegates to the new Registry + Mapping architecture.
It maintains backward compatibility with existing code while using
the more robust underlying system.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.services.case.case_kind import resolve_profile_case_kind, resolve_public_case_kind
from app.services.case.case_policy_service import (
    get_case_profile_group_map,
    get_case_profile_override,
)
from app.services.case.profile_syncs import resolve_case_profile_syncs
from app.services.case_fields import FieldRegistry, MappingService
from app.services.case_fields import get_allowed_keys as _get_allowed_keys
from app.services.case_fields import get_fields_for_case
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaseProfile:
    """
    division/type    scale down  File.
    - namespace: MatterCustomField.namespace
    - arg_key: sync/auto_status   kwargs key (In Progress: outgoing -> out_*   )
    - allowed_keys: customfield Save   List
    - id_sync/ev_sync: Identifiers/ sync  (if none None)
    - auto_status: _apply_auto_status_from_db  
    - supports_image: Image   
    """

    namespace: str
    arg_key: str
    allowed_keys: List[str]
    id_sync: Optional[Callable[..., Any]]
    ev_sync: Optional[Callable[..., Any]]
    auto_status: bool = True
    supports_image: bool = False
    division: str = ""
    case_type: str = ""
    mapping_division: str = ""
    mapping_type: str = ""
    group: str = ""


class CaseParameterService:
    """
    Facade service for case parameter management.

    Provides the same interface as before but delegates to:
    - FieldRegistry: Field definitions (type, label, validators)
    - MappingService: Which fields appear for each case type

    This class is kept for backward compatibility with existing code.
    New code should use MappingService directly.
    """

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Initialize the underlying services
        registry = FieldRegistry.instance()
        registry.initialize()

        mapping = MappingService.instance()
        mapping.initialize()

    @staticmethod
    def _effective_input_type(item: Dict[str, Any]) -> str:
        input_type = str((item or {}).get("input_type") or "text").strip() or "text"
        serializer = str((item or {}).get("serializer") or "").strip()
        key = str((item or {}).get("key") or "").strip()
        if input_type == "text" and (
            serializer == "date" or key.endswith("_date") or key.endswith("_deadline")
        ):
            return "date"
        return input_type

    @classmethod
    def _apply_widget_overrides(
        cls,
        fields: List[Dict],
        division: str,
        case_type: str,
    ) -> List[Dict]:
        """
        Normalize widget types for templates that expect legacy select_* tokens.

        - Convert select + options_source into the legacy widget string (ex: select_department)
        - Force filing/right type fields to use the expected dropdown widgets per case type
        """
        _div, ctype = resolve_profile_case_kind(division, case_type)
        try:
            from app.services.case.case_menu_config import case_menu_profile_values

            menu_profile = case_menu_profile_values(division, case_type)
            if menu_profile:
                _menu_div, menu_type, _namespace = menu_profile
                ctype = menu_type or ctype
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case_parameter_service.apply_widget_overrides.menu_profile",
                log_key="case_parameter_service.apply_widget_overrides.menu_profile",
                log_window_seconds=300,
            )
        adjusted: List[Dict] = []

        for field in fields:
            item = dict(field or {})
            input_type = cls._effective_input_type(item)
            item["input_type"] = input_type
            options_source = (item.get("options_source") or "").strip()
            key = (item.get("key") or "").strip()

            if input_type == "select" and options_source:
                item["input_type"] = options_source

            if key == "filing_type":
                if ctype == "DESIGN":
                    item["input_type"] = "select_design_filing_type"
                elif ctype == "TRADEMARK":
                    item["input_type"] = "select_tm_filing_type"
                else:
                    item["input_type"] = "select_filing_type"
            elif key == "filing_kind" and ctype == "DESIGN":
                item["input_type"] = "select_design_filing_kind"
            elif key == "right_type":
                if ctype == "TRADEMARK":
                    item["input_type"] = "select_tm_right_type"
                elif ctype in ("LITIGATION", "MISC"):
                    item["input_type"] = "select_litigation_right_type"

            adjusted.append(item)

        return adjusted

    @classmethod
    def get_fields(
        cls,
        business_area: Optional[str] = None,
        division: Optional[str] = None,
        case_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve fields matching criteria.

        Returns list of field dictionaries with:
            key, label, input_type, order, col, required, options_source
        """
        cls.get_instance()  # Ensure initialized
        fields = get_fields_for_case(division or "", case_type or "")
        return cls._apply_widget_overrides(fields, division or "", case_type or "")

    @classmethod
    def get_field_layout(
        cls, division: str, case_type: str
    ) -> List[Tuple[Tuple[str, str, str, str, Any], Tuple[str, str, str, str, Any]]]:
        """
        Returns a list of tuples for 2-column layout.
        Format: ( (label, key, widget, group, group_order), ... )

        This method maintains backward compatibility with the legacy format.
        """
        cls.get_instance()  # Ensure initialized

        fields = get_fields_for_case(division, case_type)
        fields = cls._apply_widget_overrides(fields, division, case_type)

        def _cell(field: Dict[str, Any]) -> Tuple[str, str, str, str, Any]:
            return (
                field.get("label", ""),
                field.get("key", ""),
                field.get("input_type", "text"),
                str(field.get("group") or ""),
                field.get("group_order"),
            )

        # Group by order (each order value is one row)
        rows: Dict[float, List[Dict]] = {}
        for f in fields:
            order = f.get("order", 0)
            if order not in rows:
                rows[order] = []
            rows[order].append(f)

        layout = []
        for order in sorted(rows.keys()):
            row_fields = sorted(rows[order], key=lambda x: x.get("col", 1))
            if len(row_fields) > 2:
                logger.warning(
                    "Case field layout overflow (%s/%s order=%s): %s",
                    division,
                    case_type,
                    order,
                    [f.get("key", "") for f in row_fields],
                )

                blank = {
                    "key": "__blank__",
                    "label": "",
                    "input_type": "blank",
                    "group": "",
                    "group_order": None,
                }
                left_fields = [f for f in row_fields if int(f.get("col", 1) or 1) == 1]
                right_fields = [f for f in row_fields if int(f.get("col", 1) or 1) == 2]

                # If all fields land on one side, fall back to sequential pairing.
                if not left_fields or not right_fields:
                    for i in range(0, len(row_fields), 2):
                        left = row_fields[i]
                        right = row_fields[i + 1] if (i + 1) < len(row_fields) else blank
                        layout.append(
                            (
                                _cell(left),
                                _cell(right),
                            )
                        )
                    continue

                max_len = max(len(left_fields), len(right_fields))
                for i in range(max_len):
                    if i < len(left_fields):
                        left = left_fields[i]
                        right = right_fields[i] if i < len(right_fields) else blank
                    else:
                        # No left slot available; move remaining right items to left to avoid empty rows.
                        left = right_fields[i] if i < len(right_fields) else blank
                        right = blank
                    layout.append(
                        (_cell(left), _cell(right))
                    )
                continue

            # Ensure we have exactly 2 columns
            if len(row_fields) == 1:
                row_fields.append(
                    {
                        "key": "__blank__",
                        "label": "",
                        "input_type": "blank",
                        "group": "",
                        "group_order": None,
                    }
                )
            elif len(row_fields) == 0:
                continue

            # Format: (label, key, widget, group, group_order)
            left = row_fields[0]
            right = (
                row_fields[1]
                if len(row_fields) > 1
                else {
                    "key": "__blank__",
                    "label": "",
                    "input_type": "blank",
                    "group": "",
                    "group_order": None,
                }
            )

            layout.append(
                (
                    _cell(left),
                    _cell(right),
                )
            )

        return layout

    @classmethod
    def get_field_layout_with_meta(
        cls, division: str, case_type: str
    ) -> Tuple[
        List[Tuple[Tuple[str, str, str, str, Any], Tuple[str, str, str, str, Any]]],
        Dict[str, Dict[str, Any]],
    ]:
        """
        Returns layout pairs plus field metadata for rendering.

        Layout format matches get_field_layout(), while meta includes:
            label, input_type, required, group, options_source, help_text
        """
        cls.get_instance()  # Ensure initialized

        fields = get_fields_for_case(division, case_type)
        fields = cls._apply_widget_overrides(fields, division, case_type)
        meta: Dict[str, Dict[str, Any]] = {}
        for f in fields:
            key = f.get("key", "")
            if not key:
                continue
            meta[key] = {
                "label": f.get("label", "") or key,
                "input_type": f.get("input_type", "text"),
                "required": bool(f.get("required")),
                "group": str(f.get("group") or ""),
                "group_order": f.get("group_order"),
                "options_source": f.get("options_source"),
                "options": list(f.get("options") or []),
                "help_text": f.get("help_text"),
                "serializer": f.get("serializer") or "string",
                "default_value": f.get("default_value"),
                "deprecated": bool(f.get("deprecated")),
            }

        layout = cls.get_field_layout(division, case_type)

        def _label_with_required(label: str, key: str) -> str:
            if not key or key == "__blank__":
                return label
            info = meta.get(key) or {}
            if info.get("required") and not label.strip().endswith("*"):
                return f"{label} *"
            return label

        adjusted = []
        for left, right in layout:
            adjusted.append(
                (
                    (
                        _label_with_required(left[0], left[1]),
                        left[1],
                        left[2],
                        left[3] if len(left) > 3 else "",
                        left[4] if len(left) > 4 else None,
                    ),
                    (
                        _label_with_required(right[0], right[1]),
                        right[1],
                        right[2],
                        right[3] if len(right) > 3 else "",
                        right[4] if len(right) > 4 else None,
                    ),
                )
            )

        return adjusted, meta

    @classmethod
    def get_allowed_keys(cls, division: str, case_type: str) -> Set[str]:
        """Get all allowed field keys for filtering form data."""
        cls.get_instance()  # Ensure initialized
        keys = set(_get_allowed_keys(division, case_type))
        if "filing_deadline" in keys:
            keys.add("filing_deadline_type")
        return keys

    @classmethod
    def validate_required_fields(
        cls, form_data: dict, division: str, case_type: str
    ) -> List[Dict[str, str]]:
        """
        Validate required fields and return list of missing field details.

        Returns:
            List of dicts with missing field keys and labels.
        """
        cls.get_instance()  # Ensure initialized
        mapping_service = MappingService.instance()
        missing = mapping_service.validate_required_fields(form_data, division, case_type) or []
        return [
            {
                "key": str(item.get("key", "") or ""),
                "label": str(item.get("label", "") or ""),
            }
            for item in missing
            if isinstance(item, dict)
        ]

    @classmethod
    def get_namespace(cls, division: str, case_type: str) -> str:
        """Get the storage namespace for a case type."""
        cls.get_instance()
        mapping_service = MappingService.instance()
        return str(mapping_service.get_namespace(division, case_type) or "")

    @classmethod
    def get_case_profile(cls, division: str, case_type: str) -> CaseProfile:
        """
         : division/type to
        - namespace / allowed_keys
        - sync function wiring (id/events)
        - supports_image / auto_status
           resolve.
        """
        cls.get_instance()

        menu_item: dict[str, Any] | None = None
        try:
            from app.services.case.case_menu_config import find_case_menu_item

            menu_item = find_case_menu_item(division, case_type)
        except Exception:
            menu_item = None

        public_div, public_typ = resolve_public_case_kind(division, case_type)
        mapping_div, mapping_typ = resolve_profile_case_kind(division, case_type)

        if menu_item:
            public_div = (menu_item.get("division") or public_div or "").strip().upper()
            public_typ = (menu_item.get("type") or public_typ or "").strip().upper()
            mapping_div = (
                menu_item.get("profile_division") or mapping_div or public_div or ""
            ).strip().upper()
            mapping_typ = (
                menu_item.get("profile_type") or mapping_typ or public_typ or ""
            ).strip().upper()
            if mapping_typ in {"LITIGATION", "MISC"}:
                mapping_div = ""

        if not public_typ and not mapping_typ:
            raise ValueError(f"Unsupported case profile: division={division!r}, type={case_type!r}")

        # Normalize "PATENT-like" types to a single profile group (shared namespace/sync signatures).
        group_map = get_case_profile_group_map()
        profile_group = group_map.get(mapping_typ) or (
            "PATENT" if mapping_typ in {"PATENT", "UTILITY"} else (mapping_typ or public_typ)
        )
        if menu_item and (menu_item.get("profile_group") or "").strip():
            profile_group = (menu_item.get("profile_group") or "").strip().upper()

        override = get_case_profile_override(public_div, public_typ, group=profile_group) or (
            get_case_profile_override(mapping_div, mapping_typ, group=profile_group)
        )
        override_group = (
            (override.get("group") or override.get("mapping_type") or "").strip().upper()
        )
        if override_group:
            profile_group = override_group

        if profile_group == "LITIGATION":
            mapping_div = ""
            mapping_type = "LITIGATION"
            arg_key = "litigation"
            supports_image = False
            auto_status = False
        elif profile_group == "MISC":
            mapping_div = ""
            mapping_type = "MISC"
            arg_key = "misc"
            supports_image = False
            auto_status = False
        elif profile_group == "PCT":
            # PCT is modeled as outgoing in field mappings, but profile resolution should be robust
            # even when callers pass an empty/incorrect division.
            mapping_div = "OUT"
            mapping_type = "PCT"
            arg_key = "pct"
            supports_image = False
            auto_status = True
        else:
            prefix_map = {"DOM": "dom", "INC": "inc", "OUT": "out"}
            prefix = prefix_map.get(mapping_div)
            if not prefix:
                raise ValueError(
                    f"Unsupported case profile: division={division!r}, type={case_type!r}"
                )

            mapping_type = profile_group
            if mapping_type == "TRADEMARK":
                # Legacy helper signatures use *_tm for incoming/outgoing, but *_trademark for domestic.
                # - inc_tm / out_tm
                # - dom_trademark
                arg_key = "dom_trademark" if prefix == "dom" else f"{prefix}_tm"
            else:
                arg_key = f"{prefix}_{mapping_type.lower()}"
            supports_image = mapping_type in {"DESIGN", "TRADEMARK"}
            auto_status = True

        if override:
            mapping_div = (override.get("mapping_division") or mapping_div or "").strip().upper()
            mapping_type = (override.get("mapping_type") or mapping_type or "").strip().upper()
            arg_key = (override.get("arg_key") or arg_key or "").strip() or arg_key
            if "supports_image" in override:
                supports_image = bool(override.get("supports_image"))
            if "auto_status" in override:
                auto_status = bool(override.get("auto_status"))

        namespace = (cls.get_namespace(public_div, public_typ) or "").strip()
        if not namespace and menu_item:
            namespace = (menu_item.get("namespace") or "").strip()
        if not namespace:
            raise ValueError(f"Unsupported case profile: division={division!r}, type={case_type!r}")

        allowed_keys = sorted(cls.get_allowed_keys(public_div, public_typ))

        if menu_item:
            if menu_item.get("supports_image") is not None:
                supports_image = bool(menu_item.get("supports_image"))
            if menu_item.get("auto_status") is not None:
                auto_status = bool(menu_item.get("auto_status"))

        id_sync, ev_sync = resolve_case_profile_syncs(
            mapping_division=mapping_div or public_div,
            mapping_type=mapping_type,
            division=division,
            case_type=case_type,
        )

        return CaseProfile(
            namespace=namespace,
            arg_key=arg_key,
            allowed_keys=allowed_keys,
            id_sync=id_sync,
            ev_sync=ev_sync,
            auto_status=auto_status,
            supports_image=supports_image,
            division=public_div,
            case_type=public_typ,
            mapping_division=mapping_div,
            mapping_type=mapping_type,
            group=profile_group,
        )

    @classmethod
    def reload_if_changed(cls) -> bool:
        """Reload mapping config if file has changed."""
        cls.get_instance()
        registry_reloaded = FieldRegistry.instance().reload_if_changed()
        mapping_reloaded = MappingService.instance().reload_if_changed()
        return bool(registry_reloaded or mapping_reloaded)
