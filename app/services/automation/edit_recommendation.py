import re
from typing import List


class EditRecommendationService:
    """
    Service to provide field recommendations for Matter Edit page
    based on the current Status (Red Status) of the matter.
    """

    # Mapping of Status to Reference Field Keys (Superset of all case types)
    RECOMMENDATION_MAP = {
        # --- Application Stage ---
        "FilingDeadline": [
            "application_date",
            "application_no",
            "filing_deadline",
            "title",
            "title_en",  # Title / ( Title)
            "applicant_name",
            "client_name",
            "design_filing_type",
            "tm_filing_type",
            "image_file",
            "image",
            "application_ol_sent_date",
        ],
        "ForeignFilingDeadline": [
            "priority_date",
            "priority_no",
            "foreign_filing_deadline",
            "pct_application_no",
            "pct_application_date",
            "madrid_application_no",
            "madrid_application_date",
            "ep_application_no",
            "ctm_application_no",
            "original_app_date",
            "parent_application_no",
        ],
        # --- Examination / OA Stage ---
        "Examination requestDeadline": [
            "exam_request_date",
            "exam_request_date",  # sometimes key varies or typo
            "exam_request_deadline",
            "exam_deadline",
            "application_no",
        ],
        "OA ": [
            "oa_date",
            "response_deadline",
            "response_date",
            "argument_date",
            "amendment_date",
            "opinion_deadline",
        ],
        "Deadline": [
            "oa_date",
            "response_deadline",
            "response_date",
            "argument_date",
            "amendment_date",
            "opinion_deadline",
        ],
        # --- Registration Stage ---
        "RegistrationDeadline": [
            "registration_date",
            "registration_no",
            "reg_deadline",
            "reg_decision_date",
            "reg_decision_received",
            "reg_fee_paid_date",
            "reg_ol_sent_date",
        ],
        "RegistrationDeadline": [
            "registration_date",
            "registration_no",
            "reg_penalty_deadline",
            "reg_fee_paid_date",
        ],
        # --- Maintenance/Expiry ---
        "Term expired": ["term_expiry_date", "annuity_payment_date", "next_annuity_deadline"],
        "Annuity FeePayment": ["next_annuity_deadline", "annuity_paid_date"],
        # --- Termination ---
        "Abandoned": ["abandon_date", "abandon_reason"],
        "Matter closed": ["complete_date", "close_reason", "decision_date"],
        # --- Litigation / Appeal ---
        " /Billing/Deadline": [
            "request_date",
            "request_deadline",
            "response_date",
            "detailed_reason_deadline",
            "appeal_date",
            "appeal_deadline",
            "judgment_appeal_date",
            "judgment_appeal_deadline",
        ],
        "": [
            "request_date",
            "request_deadline",
            "response_date",
            "detailed_reason_deadline",
            "appeal_date",
            "appeal_deadline",
            "judgment_appeal_date",
            "judgment_appeal_deadline",
        ],
    }

    @classmethod
    def _normalize_status_red(cls, status_red: str) -> str:
        status = (status_red or "").strip()
        if not status:
            return ""
        # Normalize common display suffixes that may be attached in UI/DB
        # e.g. "ForeignFilingDeadline (2026-08-19)", "ForeignFilingDeadline[2026-08-19]".
        status = re.sub(r"\s*\[[^\]]*\]\s*$", "", status)
        status = re.sub(r"\s*\([^)]*\)\s*$", "", status)
        status = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s*$", "", status)
        return status.strip()

    @classmethod
    def get_recommended_fields(cls, status_red: str) -> List[str]:
        """
        Returns a list of field keys that should be highlighted for the given status.
        Args:
            status_red: The 'Red Status' string (e.g., 'FilingDeadline').
        Returns:
            List of string keys.
        """
        status = cls._normalize_status_red(status_red)
        if not status:
            return []

        fields = cls.RECOMMENDATION_MAP.get(status)
        if fields:
            # Keep order stable, remove accidental duplicates.
            return list(dict.fromkeys(fields))

        # Fallback: match labels that only differ by whitespace.
        compact = status.replace(" ", "")
        for key, value in cls.RECOMMENDATION_MAP.items():
            if key.replace(" ", "") == compact:
                return list(dict.fromkeys(value))
        return []
