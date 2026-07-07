"""
Litigation-specific field definitions.
"""

from ..field_types import FieldDefinition, required_validator
from ..registry import FieldRegistry

LITIGATION_FIELDS = [
    FieldDefinition(
        key="case_name",
        label="Matter",
        input_type="select",
        options_source="select_litigation_case",
    ),
    FieldDefinition(
        key="case_no",
        label="Matter reference",
        input_type="text",
    ),
    FieldDefinition(
        key="application_reg_no",
        label="Filing/Registration ",
        input_type="text",
    ),
    FieldDefinition(
        key="application_reg_date",
        label="Filing/Registration ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="applicant_registrant",
        label="Applicant/Registration",
        input_type="text",
    ),
    FieldDefinition(
        key="request_deadline",
        label="/Billing/ Deadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="request_date",
        label="/Billing/ ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="detailed_reason_submitted",
        label="  ",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="detailed_reason_deadline",
        label="  Deadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="detailed_reason_date",
        label="  ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="claimant_name",
        label="Billing",
        input_type="client_search",
    ),
    FieldDefinition(
        key="claimant_agent",
        label="Billing Representative",
        input_type="text",
    ),
    FieldDefinition(
        key="respondent_name",
        label="Billing",
        input_type="client_search",
    ),
    FieldDefinition(
        key="respondent_agent",
        label="Billing Representative",
        input_type="text",
    ),
    FieldDefinition(
        key="court",
        label=" Statutory",
        input_type="select",
        options_source="select_litigation_court",
    ),
    FieldDefinition(
        key="court_other",
        label="Statutory Other",
        input_type="text",
    ),
    FieldDefinition(
        key="decision_date",
        label="// ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="decision_received_date",
        label="// Upload",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="decision_result",
        label="// ",
        input_type="select",
        options_source="select_litigation_result",
    ),
    FieldDefinition(
        key="judgment_appeal_deadline",
        label="Due date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="judgment_appeal_date",
        label="Billing",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="litigation_right_type",
        label="Type",
        input_type="select",
        options_source="select_litigation_right_type",
    ),
    FieldDefinition(
        key="litigation_title",
        label="Title ",
        input_type="text",
    ),
]

FieldRegistry.instance().register_many(LITIGATION_FIELDS)
