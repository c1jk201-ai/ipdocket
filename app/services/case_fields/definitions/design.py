"""
Design-specific field definitions.
"""

from ..field_types import FieldDefinition
from ..registry import FieldRegistry

DESIGN_FIELDS = [
    FieldDefinition(
        key="image",
        label="/Image",
        input_type="text",
    ),
    FieldDefinition(
        key="article_name",
        label="Design target  ",
        input_type="text",
    ),
    FieldDefinition(
        key="is_similar_design",
        label="Design ",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="design_type",
        label="DesignType",
        input_type="text",
    ),
]

FieldRegistry.instance().register_many(DESIGN_FIELDS)
