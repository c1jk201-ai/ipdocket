"""
Case fields package initialization.

Provides the Field Registry and Mapping Service for case parameter management v2.
"""

from .field_types import INPUT_TYPES, SERIALIZERS, FieldDefinition
from .mapping_service import (
    MappingService,
    get_allowed_keys,
    get_fields_for_case,
    validate_required_fields,
)
from .registry import FieldRegistry, get_field

__all__ = [
    "FieldDefinition",
    "FieldRegistry",
    "get_field",
    "MappingService",
    "get_fields_for_case",
    "validate_required_fields",
    "get_allowed_keys",
    "INPUT_TYPES",
    "SERIALIZERS",
]
