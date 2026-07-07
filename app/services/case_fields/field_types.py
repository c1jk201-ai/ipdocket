"""
Field type definitions and validators for case parameter system v2.

This module defines the core FieldDefinition dataclass and common validators.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional

# === Validators ===


def required_validator(value: Any) -> tuple[bool, str]:
    """Check if value is not empty."""
    if value is None:
        return False, "Required Item"
    if isinstance(value, str) and not value.strip():
        return False, "Required Item"
    return True, ""


def date_format_validator(value: Any) -> tuple[bool, str]:
    """Check if value is a valid date string (YYYY-MM-DD)."""
    if not value:
        return True, ""  # Empty is OK (use required_validator for mandatory)
    if isinstance(value, date):
        return True, ""
    if isinstance(value, str):
        stripped_value = value.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", stripped_value):
            try:
                datetime.strptime(stripped_value, "%Y-%m-%d")
            except ValueError:
                return False, "    (YYYY-MM-DD)"
            return True, ""
    return False, "    (YYYY-MM-DD)"


def max_length_validator(max_len: int) -> Callable[[Any], tuple[bool, str]]:
    """Factory for max length validator."""

    def validator(value: Any) -> tuple[bool, str]:
        if value is None:
            return True, ""
        if isinstance(value, str) and len(value) > max_len:
            return False, f" {max_len} Input "
        return True, ""

    return validator


def regex_validator(pattern: str, message: str) -> Callable[[Any], tuple[bool, str]]:
    """Factory for regex pattern validator."""
    compiled = re.compile(pattern)

    def validator(value: Any) -> tuple[bool, str]:
        if not value:
            return True, ""
        if isinstance(value, str) and compiled.match(value.strip()):
            return True, ""
        return False, message

    return validator


def application_number_validator(value: Any) -> tuple[bool, str]:
    """Validate application number format (XX-YYYY-XXXXXXX)."""
    if not value:
        return True, ""
    pattern = r"^\d{2}-\d{4}-\d{7}$"
    if isinstance(value, str) and re.match(pattern, value.strip()):
        return True, ""
    return False, "Application No. format: 40-2025-0123456"


# === Serializers ===


def _serialize_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return bool(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        lowered = s.lower()
        if lowered in ("y", "yes", "true", "1", "on", "t"):
            return True
        if lowered in ("n", "no", "false", "0", "off", "f"):
            return False
    raise ValueError(f"Invalid bool value: {v!r}")


SERIALIZERS = {
    "string": lambda v: (
        "" if v is None or (isinstance(v, str) and not v.strip()) else str(v).strip()
    ),
    "date": lambda v: (
        v
        if isinstance(v, date)
        else (
            None
            if v is None or (isinstance(v, str) and not v.strip())
            else v.strip() if isinstance(v, str) else v
        )
    ),
    "int": lambda v: None if v is None or (isinstance(v, str) and not v.strip()) else int(v),
    "bool": _serialize_bool,
}


# === Field Definition ===


@dataclass
class FieldDefinition:
    """
    Defines a single field's schema.

    This is the "source of truth" for what a field IS, separate from
    where it appears (which is defined in the mapping config).
    """

    key: str  # Unique identifier
    label: str  # display label
    input_type: str  # text, date, select, textarea, client_search, blank
    validators: list[Callable] = field(default_factory=list)
    options_source: Optional[str] = None  # For select: function name or code list key
    options: list[dict[str, str]] = field(default_factory=list)
    help_text: str = ""
    serializer: str = "string"  # string, date, int, bool
    deprecated: bool = False  # Legacy field marker
    default_value: Any = None

    def validate(self, value: Any) -> list[str]:
        """Run all validators and return list of error messages."""
        errors = []
        for validator in self.validators:
            ok, msg = validator(value)
            if not ok:
                errors.append(msg)
        return errors

    def serialize(self, value: Any) -> Any:
        """Serialize value using configured serializer."""
        serializer_fn = SERIALIZERS.get(self.serializer, SERIALIZERS["string"])
        try:
            return serializer_fn(value)
        except (ValueError, TypeError):
            return value


# === Common Input Types ===

INPUT_TYPES = {
    "text": {"html_type": "text", "css_class": "form-control"},
    "number": {"html_type": "number", "css_class": "form-control"},
    "date": {"html_type": "date", "css_class": "form-control"},
    "textarea": {"html_type": "textarea", "css_class": "form-control", "rows": 3},
    "select": {"html_type": "select", "css_class": "form-select"},
    "select_yn": {
        "html_type": "select",
        "css_class": "form-select",
        "options": [("", "-"), ("Y", "Yes"), ("N", "No")],
    },
    "client_search": {
        "html_type": "text",
        "css_class": "form-control client-search",
        "data_autocomplete": "client",
    },
    "blank": {"html_type": "hidden", "css_class": ""},
}
