"""
Case Schema Definitions.

This module defines the schema for different U.S. case types.
It specifies which parameters/fields are relevant and should be managed for each case type.

Used by:
- ParameterConflictResolver: To check conflicts only for relevant fields.
- Upload Analysis: To filter extracted data based on the target matter's type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CaseSchema:
    code: str
    name: str
    description: str = ""
    allowed_fields: list[str] = field(
        default_factory=list
    )  # List of keys (e.g. 'app_no', 'tm_name')


# Common fields shared by most types
_COMMON = [
    "our_ref",
    "app_no",
    "app_date",
    "dispatch_no",
    "app_type",
    "applicants",
    "inventors",
    "agent",
    "related_applications",
    "priority_claims",
]

# Right-specific fields
_PATENT = ["title", "title_en", "claim_count", "exam_request", "exam_type"]
_DESIGN = [
    "title",
    "title_en",
    "article_name",
    "partial_design",
    "exam_request",
]  # Design can have exam request (some systems) or just registration
_TRADEMARK = [
    "tm_name",
    "tm_type",
    "nice_classes",
    "designated_goods",
    "tm_registration_payment_term",
]


# Helper to combine
def _schema(base, specific):
    return list(set(base + specific))


# 1. US
DOMESTIC_PATENT = CaseSchema(
    code="US_PATENT", name="US - Patent", allowed_fields=_schema(_COMMON, _PATENT)
)

DOMESTIC_UTILITY = CaseSchema(
    code="US_UTILITY", name="US - Utility", allowed_fields=_schema(_COMMON, _PATENT)
)

DOMESTIC_DESIGN = CaseSchema(
    code="US_DESIGN", name="US - Design", allowed_fields=_schema(_COMMON, _DESIGN)
)

DOMESTIC_TRADEMARK = CaseSchema(
    code="US_TRADEMARK", name="US - Trademark", allowed_fields=_schema(_COMMON, _TRADEMARK)
)

# 2. Inbound US
INCOMING_PATENT = CaseSchema(
    code="In_PATENT", name="Inbound US - Patent", allowed_fields=_schema(_COMMON, _PATENT)
)

INCOMING_UTILITY = CaseSchema(
    code="In_UTILITY", name="Inbound US - Utility", allowed_fields=_schema(_COMMON, _PATENT)
)

INCOMING_DESIGN = CaseSchema(
    code="In_DESIGN", name="Inbound US - Design", allowed_fields=_schema(_COMMON, _DESIGN)
)

INCOMING_TRADEMARK = CaseSchema(
    code="In_TRADEMARK", name="Inbound US - Trademark", allowed_fields=_schema(_COMMON, _TRADEMARK)
)

# 3. Foreign - Usually handled via different docs, but schema is good to have
OVERSEAS_PATENT = CaseSchema(
    code="OS_PATENT", name="Foreign · Patent", allowed_fields=_schema(_COMMON, _PATENT)
)

OVERSEAS_UTILITY = CaseSchema(
    code="OS_UTILITY", name="Foreign · Utility", allowed_fields=_schema(_COMMON, _PATENT)
)

OVERSEAS_DESIGN = CaseSchema(
    code="OS_DESIGN", name="Foreign · Design", allowed_fields=_schema(_COMMON, _DESIGN)
)

OVERSEAS_TRADEMARK = CaseSchema(
    code="OS_TRADEMARK", name="Foreign · Trademark", allowed_fields=_schema(_COMMON, _TRADEMARK)
)

# 4. Proceedings / litigation
DISPUTE_GENERIC = CaseSchema(
    code="DISPUTE",
    name="Proceedings / Litigation",
    allowed_fields=_COMMON + ["title", "exam_type"],  # Simplified
)

# Registry
CASE_SCHEMAS = {
    s.code: s
    for s in [
        DOMESTIC_PATENT,
        DOMESTIC_UTILITY,
        DOMESTIC_DESIGN,
        DOMESTIC_TRADEMARK,
        INCOMING_PATENT,
        INCOMING_UTILITY,
        INCOMING_DESIGN,
        INCOMING_TRADEMARK,
        OVERSEAS_PATENT,
        OVERSEAS_UTILITY,
        OVERSEAS_DESIGN,
        OVERSEAS_TRADEMARK,
        DISPUTE_GENERIC,
    ]
}


def get_case_schema(matter_type_code: str) -> CaseSchema | None:
    """Get schema by matter type code."""
    # This assumes matter_type_code matches the keys in CASE_SCHEMAS.
    # Logic might need mapping if Matter table uses different codes.
    return CASE_SCHEMAS.get(matter_type_code)


def get_schema_for_matter(matter) -> CaseSchema | None:
    """Determine schema for a matter instance."""
    # Mapping logic depends on how 'matter' stores its type.
    # Assuming matter.matter_type or matter.right_group + category
    # For now, we return None or a default if logic not clear

    # Placeholder logic - implement based on actual Matter model fields
    # e.g. if matter.country == 'US' and matter.right_type == 'PATENT' -> DOMESTIC_PATENT
    return None
