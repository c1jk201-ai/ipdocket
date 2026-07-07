"""
PCT-specific field definitions.
"""

from ..field_types import FieldDefinition
from ..registry import FieldRegistry

PCT_FIELDS = [
    FieldDefinition(
        key="self_designated",
        label="",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="application_language",
        label="FilingLanguage",
        input_type="select_application_language",
    ),
    FieldDefinition(
        key="preliminary_exam_request_date",
        label="Preliminary examination Billing",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="preliminary_exam_deadline",
        label="Preliminary examination Due date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="translation_deadline",
        label=" Due date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="translation_submitted_date",
        label=" ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="international_search_report_received_date",
        label=" Upload",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="claim_amendment_deadline",
        label="Billing Deadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="national_phase_countries",
        label="Domestic ",
        input_type="text",
    ),
    FieldDefinition(
        key="national_phase_last_entry_date",
        label="Domestic ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="national_phase_notice_deadline",
        label="Domestic Deadline Guidance Due date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="national_phase_deadline",
        label="Domestic Due date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="national_phase_19m_deadline",
        label="Domestic Deadline 1  Notice",
        input_type="date",
        serializer="date",
    ),
]

FieldRegistry.instance().register_many(PCT_FIELDS)
