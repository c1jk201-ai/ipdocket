"""
Patent-specific field definitions.
"""

from ..field_types import FieldDefinition, date_format_validator
from ..registry import FieldRegistry

PATENT_FIELDS = [
    FieldDefinition(
        key="title",
        label="Title ",
        input_type="text",
    ),
    FieldDefinition(
        key="priority_exam_request",
        label="Examination",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="exam_request_date",
        label="Examination request date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="exam_request_deadline",
        label="Examination requestDeadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="expedited_request_date",
        label="Examination ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="expedited_decision_date",
        label="Examination ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="gazette_decision_received",
        label="Publication decision notice Upload",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="gazette_decision_date",
        label="Publication decision",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="special_case_claimed",
        label="Filing ",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="special_claim_doc_deadline",
        label="  Deadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="original_registration_date",
        label="Registration date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="original_registration_no",
        label="Registration No.",
        input_type="text",
    ),
    FieldDefinition(
        key="applicant_name",
        label="Applicant name",
        input_type="text",
    ),
]

FieldRegistry.instance().register_many(PATENT_FIELDS)
