"""
Common field definitions shared across all case types.

These fields appear in multiple case type configurations.
"""

from ..field_types import FieldDefinition, date_format_validator, required_validator
from ..registry import FieldRegistry

# === Staff & Assignment Fields ===

COMMON_FIELDS = [
    FieldDefinition(
        key="manager",
        label="Manager",
        input_type="text",
        validators=[required_validator],
    ),
    FieldDefinition(
        key="attorney",
        label="Responsible attorney",
        input_type="text",
        validators=[required_validator],
    ),
    FieldDefinition(
        key="application_agent",
        label="Filing Representative",
        input_type="text",
    ),
    FieldDefinition(
        key="application_applicant_name",
        label="Filing Applicant",
        input_type="text",
    ),
    FieldDefinition(
        key="application_applicant_customer_no",
        label="PatentClient",
        input_type="text",
    ),
    FieldDefinition(
        key="handler",
        label="Handler",
        input_type="text",
    ),
    FieldDefinition(
        key="drawing_handler",
        label="Contact",
        input_type="text",
    ),
    FieldDefinition(
        key="drafter",
        label="",
        input_type="text",
    ),
    FieldDefinition(
        key="assignee1",
        label="(1)",
        input_type="text",
    ),
    FieldDefinition(
        key="assignee2",
        label="(2)",
        input_type="text",
    ),
    # === Client Fields ===
    FieldDefinition(
        key="client_name",
        label="Client",
        input_type="client_search",
    ),
    FieldDefinition(
        key="client_mgmt_no",
        label="Client ",
        input_type="text",
    ),
    FieldDefinition(
        key="client_contact",
        label="Client Contact",
        input_type="text",
    ),
    FieldDefinition(
        key="applicant_contact",
        label="Applicant Contact",
        input_type="text",
    ),
    # === Department & Assignment Dates ===
    FieldDefinition(
        key="department",
        label="ResponsibleDepartment",
        input_type="select",
        options_source="select_department",
    ),
    FieldDefinition(
        key="retained_date",
        label="Engagement date",
        input_type="date",
        serializer="date",
        validators=[date_format_validator],
    ),
    FieldDefinition(
        key="draft_sent_date",
        label="Draft",
        input_type="date",
        serializer="date",
    ),
    # === Application Fields ===
    FieldDefinition(
        key="filing_type",
        label="Filing type",
        input_type="text",
    ),
    FieldDefinition(
        key="right_type",
        label="Type",
        input_type="text",
    ),
    FieldDefinition(
        key="application_date",
        label="Filing date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="application_no",
        label="Application No.",
        input_type="text",
    ),
    FieldDefinition(
        key="filing_deadline",
        label="Filing deadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="filing_deadline_type",
        label="FilingDeadline Type",
        input_type="select_deadline_type",
    ),
    # === Priority Fields ===
    FieldDefinition(
        key="priority_claimed",
        label="Priority ",
        input_type="select_yn",
    ),
    FieldDefinition(
        key="priority_date",
        label="",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="priority_no",
        label="Priority",
        input_type="text",
    ),
    FieldDefinition(
        key="parent_application_date",
        label="Filing date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="parent_application_no",
        label="Parent application No.",
        input_type="text",
    ),
    # === Publication Fields ===
    FieldDefinition(
        key="publication_date",
        label="Publication date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="publication_no",
        label="Publication No.",
        input_type="text",
    ),
    FieldDefinition(
        key="gazette_date",
        label="Publication",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="gazette_no",
        label="Publication",
        input_type="text",
    ),
    # === Registration Fields ===
    FieldDefinition(
        key="reg_decision_received",
        label="Notice of allowance Upload",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="reg_decision_date",
        label="Notice of allowance",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="reg_deadline",
        label="RegistrationDue date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="reg_extension_date",
        label="RegistrationPeriod",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="registration_date",
        label="Registration date",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="registration_no",
        label="Registration No.",
        input_type="text",
    ),
    FieldDefinition(
        key="term_expiry_date",
        label=" Period ",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="registrant_name",
        label="Registration",
        input_type="text",
    ),
    # === Rejection & Appeal Fields ===
    FieldDefinition(
        key="rejection_received_date",
        label="Final rejection Upload",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="rejection_date",
        label="",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="appeal_deadline",
        label=" BillingDeadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="appeal_date",
        label=" Billing",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="appeal_no",
        label="",
        input_type="text",
    ),
    FieldDefinition(
        key="appeal_decision_date",
        label="",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="appeal_decision_result",
        label=" ",
        input_type="text",
    ),
    # === Opposition Fields ===
    FieldDefinition(
        key="opposition_date",
        label="",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="opposition_no",
        label="",
        input_type="text",
    ),
    FieldDefinition(
        key="opposition_decision_date",
        label="",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="opposition_decision_result",
        label=" ",
        input_type="text",
    ),
    # === Foreign Filing Fields ===
    FieldDefinition(
        key="foreign_filing_deadline",
        label="ForeignFilingDeadline",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="foreign_filing_date",
        label="ForeignFiling date",
        input_type="date",
        serializer="date",
    ),
    # === Termination Fields ===
    FieldDefinition(
        key="abandon_date",
        label="Abandoned/Withdrawn",
        input_type="date",
        serializer="date",
    ),
    FieldDefinition(
        key="abandon_reason",
        label="Abandoned/Withdrawn Reason",
        input_type="text",
    ),
    FieldDefinition(
        key="complete_date",
        label="Done/Closed",
        input_type="date",
        serializer="date",
    ),
    # === Memo & Misc Fields ===
    FieldDefinition(
        key="memo2",
        label="Notes",
        input_type="textarea",
    ),
    FieldDefinition(
        key="misc",
        label="Other",
        input_type="textarea",
    ),
    FieldDefinition(
        key="misc_memo",
        label="Other Notes",
        input_type="textarea",
    ),
    FieldDefinition(
        key="common_memo",
        label="   ",
        input_type="textarea",
    ),
    FieldDefinition(
        key="related_applications",
        label="   ",
        input_type="text",
    ),
    FieldDefinition(
        key="stand_reason",
        label="WaitingReason",
        input_type="select",
        options_source="select_stand_reason",
    ),
    # === Old Reference ===
    FieldDefinition(
        key="old_our_ref",
        label="Former Our Ref.",
        input_type="text",
    ),
    # === Special placeholder ===
    FieldDefinition(
        key="__blank__",
        label="",
        input_type="blank",
    ),
]

# Register all common fields
FieldRegistry.instance().register_many(COMMON_FIELDS)
