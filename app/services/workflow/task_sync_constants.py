from __future__ import annotations

import re

_USPTO_OA_NAME_REF_PREFIX = "USPTO_OA:OFFICE_ACTION:"
_OA_MAIN_NOTICE_REF_RE = re.compile(r"^NOTICE:OA:([^:]+)$", re.IGNORECASE)
_OA_TITLE_PREFIX_RE = re.compile(r"^(?:Notice\s*\s*Deadline|Notice\s*)\s*[·:]\s*")
_AUTO_CLEANUP_NOTE_MARKERS = (
    "[Auto:  target ]",
    "[Auto:  Create ]",
    "[Auto: Task Create ]",
)
_OWNER_RECOVERED_NOTE_MARKER = "[Auto:  Contact Auto]"
_OWNER_FALLBACK_NOTE_MARKER = "[Auto: DefaultContact ]"
_OWNER_UNASSIGNED_NOTE_MARKER = "[Auto: Contact Confirm]"
_MANUAL_WORKFLOW_ASSIGNMENT_KEY = "manual_workflow_assignment"
_DUPLICATE_OA_DEADLINE_CLOSE_REASON = "duplicate_oa_deadline_consolidated"
_STATUS_RED_CORE_DEADLINE_SOURCES = {
    "ForeignFilingDeadline": {
        "deadline_codes": ("FOREIGN_FILING_PARIS",),
        "deadline_custom_keys": ("foreign_filing_deadline",),
        "done_custom_keys": ("foreign_filing_date",),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("FOREIGN_FILING_DEADLINE", "ForeignFilingDeadline"),
        "done_event_keys": ("FOREIGN_FILING_DATE", "ForeignFiling date"),
    },
    "Examination requestDeadline": {
        "deadline_codes": ("REQUEST_EXAMINATION",),
        "deadline_custom_keys": ("exam_deadline", "exam_request_deadline"),
        "done_custom_keys": ("exam_request_date",),
        "done_truthy_custom_keys": ("exam_requested",),
        "deadline_event_keys": ("EXAM_REQUEST_DEADLINE", "Examination request Due date"),
        "done_event_keys": ("EXAM_REQUEST_DATE", "EXAM_REQUESTED", "Examination request date"),
    },
    "PCTDomesticDeadline": {
        "deadline_codes": ("PCT_NATIONAL_PHASE",),
        "deadline_custom_keys": ("national_phase_deadline",),
        "done_custom_keys": ("national_phase_last_entry_date",),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("PCT_NATIONAL_PHASE_DEADLINE", "NATIONAL_PHASE_DEADLINE"),
        "done_event_keys": ("NATIONAL_PHASE_ENTRY_DATE", "NATIONAL_PHASE_LAST_ENTRY_DATE"),
    },
    "RegistrationDeadline": {
        "deadline_codes": ("REGISTRATION_DEADLINE",),
        "deadline_custom_keys": ("reg_deadline",),
        "done_custom_keys": ("registration_date", "reg_extension_date", "reg_fee_paid_date"),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("REGISTRATION_DEADLINE", "RegistrationDue date"),
        "done_event_keys": (
            "REGISTRATION_DATE",
            "REGISTRATION_FEE_PAID",
            "Registration date",
            "RegistrationPeriod",
        ),
    },
    "RegistrationDeadline": {
        "deadline_codes": ("PENALTY_REG_DEADLINE",),
        "deadline_custom_keys": ("reg_penalty_deadline",),
        "done_custom_keys": ("registration_date", "reg_extension_date", "reg_fee_paid_date"),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("PENALTY_REG_DEADLINE", "RegistrationDue date"),
        "done_event_keys": (
            "REGISTRATION_DATE",
            "REGISTRATION_FEE_PAID",
            "Registration date",
            "RegistrationPeriod",
        ),
    },
}
_TRUTHY_STATUS_TOKENS = {"1", "Y", "YES", "TRUE", "T", "ON"}
