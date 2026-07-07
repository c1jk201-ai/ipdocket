"""
Parameter Conflict Resolver

Detects and resolves conflicts between existing Matter data and newly extracted
parameters from USPTO and other matter documents.
"""

from __future__ import annotations

from app.extensions import db
from app.services.case.case_parameter_service import CaseParameterService
from app.services.parameter_conflict.parameter_conflict_detector import (
    detect_conflicts as _detect_conflicts_impl,
)
from app.services.parameter_conflict.parameter_conflict_loader import (
    load_matter_data as _load_matter_data_impl,
)
from app.services.parameter_conflict.parameter_conflict_types import (
    ConflictItem,
    ParameterExtractionResult,
)
from app.services.parameter_conflict.parameter_conflict_updater import (
    apply_field as _apply_field_impl,
)
from app.services.parameter_conflict.parameter_conflict_updater import (
    apply_parameters as _apply_parameters_impl,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


class ParameterConflictResolver:
    """
    Detects conflicts between existing Matter data and extracted parameters.
    """

    # Field definitions with display labels
    FIELD_DEFINITIONS = {
        "right_name": {
            "label": "Invention Title",
            "table": "matter",
            "priority": 1,
        },
        "title_en": {
            "label": "Title ()",
            "table": "matter_custom_field",
            "priority": 1,
        },
        "title": {
            "label": "Title ()",
            "table": "matter_custom_field",
            "priority": 1,
        },
        "proposal_title": {
            "label": "Proposed title",
            "table": "matter_custom_field",
            "priority": 1,
        },
        "application_agent": {
            "label": "Filing Representative",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "application_applicant_name": {
            "label": "Filing Applicant",
            "table": "matter_custom_field",
            "priority": 2,
            "hidden": False,
        },
        "application_applicant_customer_no": {
            "label": "PatentClient",
            "table": "matter_custom_field",
            "priority": 2,
            "hidden": False,
        },
        "filing_type": {
            "label": "Filing type",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "right_type": {
            "label": "Type",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "tm_name": {
            "label": "Trademark",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "tm_type": {
            "label": "TrademarkType",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "application_classes": {
            "label": "Filing classes",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "application_goods": {
            "label": "Filing goods/services",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "article_name": {
            "label": "Design target  ",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "is_partial": {
            "label": "Design ",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "registrant_name": {
            "label": "Registration",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "pct_application_no": {
            "label": "PCT Application No.",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "pct_application_date": {
            "label": "PCT Filing date",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "app_no": {
            "label": "Application No.",
            "table": "matter_identifier",
            "priority": 1,
        },
        "app_type": {
            "label": "Filing type",
            "table": "matter_event",
            "priority": 2,
        },
        "exam_request": {
            "label": "Examination request ",
            "table": "matter_event",
            "priority": 2,
        },
        "claim_count": {
            "label": "Billing ",
            "table": "matter_event",
            "priority": 2,
        },
        "app_date": {
            "label": "Filing date",
            "table": "matter_event",
            "priority": 1,
        },
        "applicant": {
            "label": "Applicant",
            "table": "matter_party_role",
            "priority": 2,
        },
        "inventor": {
            "label": "Inventor",
            "table": "matter_party_role",
            "priority": 2,
        },
        "attorney": {
            "label": "Responsible attorney",
            "table": "matter_staff_assignment",
            "priority": 3,
        },
        # Custom fields for UI display (hidden from confirmation - they duplicate main fields)
        "application_no": {
            "label": "Application No.",
            "table": "matter_custom_field",
            "priority": 1,
            "hidden": True,  # Duplicates identifier_Application No.
        },
        "application_date": {
            "label": "Filing date",
            "table": "matter_custom_field",
            "priority": 1,
            "hidden": True,  # Duplicates event APP_DATE
        },
        "foreign_filing_deadline": {
            "label": "ForeignFilingDeadline",
            "table": "matter_event",
            "priority": 1,
        },
        "inventor_name": {
            "label": "Inventor",
            "table": "matter_custom_field",
            "priority": 2,
            "hidden": True,  # Duplicates party_INVENTOR
        },
        "exam_requested": {
            "label": "Examination request",
            "table": "matter_custom_field",
            "priority": 2,
            "hidden": True,  # Duplicates event EXAM_REQ
        },
        "exam_request_date": {
            "label": "Examination request date",
            "table": "matter_custom_field",
            "priority": 2,
            "hidden": True,  # Duplicates logic based on exam_requested
        },
        "priority_exam_request": {
            "label": "Examination",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "expedited_request_date": {
            "label": "Examination ",
            "table": "matter_custom_field",
            "priority": 2,
        },
        "claims_total": {
            "label": "Billing",
            "table": "matter_custom_field",
            "priority": 2,
        },
    }

    _CONFLICT_DEFINITIONS_CACHE: dict | None = None

    @classmethod
    def _load_conflict_definitions(cls) -> dict:
        if cls._CONFLICT_DEFINITIONS_CACHE is not None:
            return cls._CONFLICT_DEFINITIONS_CACHE
        try:
            from app.services.case_fields.unified_config import load_unified_registry_data

            data, _meta = load_unified_registry_data()
            conflict_fields = (data or {}).get("conflict_fields")
            if isinstance(conflict_fields, dict):
                cls._CONFLICT_DEFINITIONS_CACHE = conflict_fields
                return cls._CONFLICT_DEFINITIONS_CACHE
        except Exception as exc:
            # Best-effort: fall back to built-in field definitions.
            report_swallowed_exception(
                exc,
                context="parameter_conflict_resolver._load_conflict_definitions",
                log_key="parameter_conflict_resolver._load_conflict_definitions",
                log_window_seconds=300,
            )
        cls._CONFLICT_DEFINITIONS_CACHE = cls.FIELD_DEFINITIONS
        return cls._CONFLICT_DEFINITIONS_CACHE

    def __init__(self, matter_id: str):
        """
        Initialize resolver for a specific matter.

        Args:
            matter_id: The matter ID to compare against
        """
        self.matter_id = matter_id
        self._matter_data = None
        self._load_matter_data()

    def _load_matter_data(self):
        """Load current matter data from database"""
        self._matter_data = _load_matter_data_impl(matter_id=self.matter_id)
        return

    def detect_conflicts(self, extracted_params: dict) -> ParameterExtractionResult:
        """
        Compare extracted parameters with existing data and detect conflicts.

        Args:
            extracted_params: Dictionary from document mapping helpers.

        Returns:
            ParameterExtractionResult with categorized fields
        """
        if extracted_params:
            extracted_params = {
                key: value
                for key, value in extracted_params.items()
                if key not in {"applicant_name", "attorney", "staff_assignments"}
            }
        return _detect_conflicts_impl(
            matter_id=self.matter_id,
            matter_data=self._matter_data or {},
            extracted_params=extracted_params,
            field_definitions=self._load_conflict_definitions(),
            get_custom_field_namespace=self._get_custom_field_namespace,
        )

    def apply_parameters(
        self,
        auto_apply: list[ConflictItem],
        user_selections: dict[str, str],  # field_name -> 'current' or 'new'
        conflicts: list[ConflictItem] = None,
    ) -> dict:
        """
        Apply the selected parameters to the matter.

        Args:
            auto_apply: Fields to automatically apply
            user_selections: Dictionary mapping field_name to user choice
            conflicts: List of conflict items to look up 'new' values from

        Returns:
            Summary of applied changes
        """
        return _apply_parameters_impl(
            matter_id=self.matter_id,
            auto_apply=auto_apply,
            user_selections=user_selections,
            conflicts=conflicts,
            get_custom_field_namespace=self._get_custom_field_namespace,
        )

    def _get_custom_field_namespace(self):
        """Determine the namespace for custom fields."""
        m = db.session.execute(
            text("SELECT right_group, matter_type, our_ref FROM matter WHERE matter_id = :mid"),
            {"mid": self.matter_id},
        ).fetchone()

        if m:
            rg = (m.right_group or "").strip().upper()
            mt = (m.matter_type or "").strip().upper()
            if not mt:
                return "domestic_patent"
            if mt == "PCT" and not rg:
                rg = "OUT"
            ns = (CaseParameterService.get_namespace(rg, mt) or "").strip()
            if not ns and not rg and mt not in ("LITIGATION", "PCT", "MISC"):
                ns = (CaseParameterService.get_namespace("DOM", mt) or "").strip()
            if ns:
                return ns

        return "domestic_patent"

    def _apply_field(self, item: ConflictItem):
        """Apply a single field update"""
        return _apply_field_impl(
            matter_id=self.matter_id,
            item=item,
            get_custom_field_namespace=self._get_custom_field_namespace,
        )
