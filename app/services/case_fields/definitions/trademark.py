"""
Trademark-specific field definitions.
"""

from ..field_types import FieldDefinition
from ..registry import FieldRegistry

TRADEMARK_FIELDS = [
    FieldDefinition(
        key="tm_type",
        label="TrademarkType",
        input_type="select",
        options_source="select_tm_type",
    ),
    FieldDefinition(
        key="tm_name",
        label="Trademark",
        input_type="text",
    ),
    FieldDefinition(
        key="application_classes",
        label="Filing classes",
        input_type="text",
    ),
    FieldDefinition(
        key="application_goods",
        label="Filing goods/services",
        input_type="textarea",
    ),
    FieldDefinition(
        key="registration_classes",
        label="Registration ",
        input_type="text",
    ),
    FieldDefinition(
        key="registration_goods",
        label="Registration ",
        input_type="textarea",
    ),
    FieldDefinition(
        key="inhouse_status",
        label="InternalStatus",
        input_type="text",
    ),
    FieldDefinition(
        key="exhibition_date",
        label=" ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="cancellation_request_date",
        label="Trademark Cancel Billing",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="cancellation_decision_result",
        label="Trademark Cancel ",
        input_type="text",
    ),
    # Filing type for trademark (different widget)
    FieldDefinition(
        key="tm_filing_type",
        label="Filing type",
        input_type="select",
        options_source="select_tm_filing_type",
    ),
    FieldDefinition(
        key="tm_right_type",
        label="Type",
        input_type="select",
        options_source="select_tm_right_type",
    ),
    FieldDefinition(
        key="tm_registration_payment_term",
        label="Trademark Registration Payment",
        input_type="select",
        options_source="select_tm_registration_payment_term",
    ),
]

FieldRegistry.instance().register_many(TRADEMARK_FIELDS)
