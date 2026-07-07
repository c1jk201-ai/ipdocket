"""
Form context builder to eliminate repetitive render_template calls in general.py.

This module provides a centralized way to build template context for matter creation
and editing forms, reducing code duplication from ~20 identical render_template blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from flask import render_template


@dataclass
class MatterFormContext:
    """Context holder for matter form templates.

    Replaces 12+ repeated template parameters with a single context builder.
    """

    division: str
    case_type: str
    form_data: Dict[str, Any]
    staff_picker: Dict[str, Any]
    staff_assignment: Dict[str, Any]

    # Optional overrides (lazy-loaded from helpers if not provided)
    _field_overrides: Dict[str, list] = field(default_factory=dict)

    def is_kind(self, div: str, mtype: str) -> bool:
        """Check if this context matches a specific division/type combination."""
        return self.division == div and self.case_type == mtype

    def to_template_kwargs(self) -> Dict[str, Any]:
        """Convert context to template keyword arguments.

        Lazily imports field constants from helpers to avoid circular imports.
        """
        from ..helpers import (
            DOMESTIC_DESIGN_FIELDS,
            DOMESTIC_PATENT_FIELDS,
            DOMESTIC_TRADEMARK_FIELDS,
            INCOMING_DESIGN_FIELDS,
            INCOMING_PATENT_FIELDS,
            INCOMING_TRADEMARK_FIELDS,
            LITIGATION_FIELDS,
            MISC_FIELDS,
            OUTGOING_DESIGN_FIELDS,
            OUTGOING_PATENT_FIELDS,
            OUTGOING_TRADEMARK_FIELDS,
            PATENT_LIKE_TYPES,
            PCT_FIELDS,
        )

        case_type_upper = (self.case_type or "").upper()
        is_patent_like = case_type_upper in PATENT_LIKE_TYPES
        is_pct = case_type_upper == "PCT"

        return {
            "division": self.division,
            "case_type": self.case_type,
            "dom_patent_fields": self._field_overrides.get("dom_patent_fields")
            or (DOMESTIC_PATENT_FIELDS if self.division == "DOM" and is_patent_like else []),
            "dom_design_fields": self._field_overrides.get("dom_design_fields")
            or (DOMESTIC_DESIGN_FIELDS if self.is_kind("DOM", "DESIGN") else []),
            "dom_trademark_fields": self._field_overrides.get("dom_trademark_fields")
            or (DOMESTIC_TRADEMARK_FIELDS if self.is_kind("DOM", "TRADEMARK") else []),
            "inc_patent_fields": self._field_overrides.get("inc_patent_fields")
            or (INCOMING_PATENT_FIELDS if self.division == "INC" and is_patent_like else []),
            "inc_design_fields": self._field_overrides.get("inc_design_fields")
            or (INCOMING_DESIGN_FIELDS if self.is_kind("INC", "DESIGN") else []),
            "inc_trademark_fields": self._field_overrides.get("inc_trademark_fields")
            or (INCOMING_TRADEMARK_FIELDS if self.is_kind("INC", "TRADEMARK") else []),
            "out_patent_fields": self._field_overrides.get("out_patent_fields")
            or (OUTGOING_PATENT_FIELDS if self.division == "OUT" and is_patent_like else []),
            "out_design_fields": self._field_overrides.get("out_design_fields")
            or (OUTGOING_DESIGN_FIELDS if self.is_kind("OUT", "DESIGN") else []),
            "out_trademark_fields": self._field_overrides.get("out_trademark_fields")
            or (OUTGOING_TRADEMARK_FIELDS if self.is_kind("OUT", "TRADEMARK") else []),
            "pct_fields": self._field_overrides.get("pct_fields") or (PCT_FIELDS if is_pct else []),
            "litigation_fields": self._field_overrides.get("litigation_fields")
            or (LITIGATION_FIELDS if self.case_type == "LITIGATION" else []),
            "misc_fields": self._field_overrides.get("misc_fields")
            or (MISC_FIELDS if self.case_type == "MISC" else []),
            "staff_picker": self.staff_picker,
            "staff_assignment": self.staff_assignment,
            "form_data": self.form_data,
        }


def render_matter_form(
    ctx: MatterFormContext, template: str = "case/matter_create.html", **extra_kwargs
):
    """Render a matter form template with the given context.

    Args:
        ctx: MatterFormContext instance
        template: Template path (default: matter_create.html)
        **extra_kwargs: Additional template variables to merge

    Returns:
        Rendered template response

    Example:
        ctx = MatterFormContext(
            division="DOM",
            case_type="PATENT",
            form_data=dict(request.form),
            staff_picker=staff_picker,
            staff_assignment=staff_assignment,
        )
        return render_matter_form(ctx)
    """
    kwargs = ctx.to_template_kwargs()
    kwargs.update(extra_kwargs)
    return render_template(template, **kwargs)


def build_matter_form_context(
    division: str,
    case_type: str,
    form_data: Dict[str, Any],
    staff_picker: Dict[str, Any],
    staff_assignment: Dict[str, Any],
) -> MatterFormContext:
    """Factory function to create a MatterFormContext.

    This is the primary entry point for routes that need to render form templates.

    Args:
        division: Case division (DOM, INC, OUT, or empty)
        case_type: Case type (PATENT, DESIGN, TRADEMARK, LITIGATION)
        form_data: Form data dictionary
        staff_picker: Staff picker context (from _build_staff_picker_context)
        staff_assignment: Staff assignment context (from _build_staff_assignment_context)

    Returns:
        MatterFormContext ready for render_matter_form()
    """
    return MatterFormContext(
        division=division,
        case_type=case_type,
        form_data=form_data,
        staff_picker=staff_picker,
        staff_assignment=staff_assignment,
    )
