from __future__ import annotations

from app.models.ip_records import Matter
from app.utils.error_logging import report_swallowed_exception


def build_annuity_empty_hint(matter: Matter | None, matter_id: str) -> dict:
    """
    Build a read-only hint for an empty annuity section.

    Keep this free of get-or-create helpers: case detail is a GET view and must
    not mutate facts just to explain why the annuity table is empty.
    """
    mid = (matter_id or "").strip()
    if not matter or not mid:
        return {"kind": "generic"}

    try:
        from dateutil.relativedelta import relativedelta

        from app.models.matter_facts import MatterFacts
        from app.services.annuity.annuity_service import (
            ANNUITY_RIGHT_TYPES,
            _allow_reg_fee_paid_fallback_for_matter,
            _get_reg_fee_paid_date,
            _get_term_expiry_date,
            _get_term_years,
            _infer_right_type,
        )

        right_type = _infer_right_type(matter)
        if not right_type:
            return {"kind": "right_type_unknown"}
        if right_type not in ANNUITY_RIGHT_TYPES:
            return {"kind": "right_type_not_supported", "right_type": right_type}

        facts = MatterFacts.query.get(mid)
        registration_date = getattr(facts, "registration_date", None) if facts else None
        registration_source = (
            (getattr(facts, "registration_date_source", None) or "").strip() if facts else ""
        )

        if right_type == "TRADEMARK":
            term_expiry_date = _get_term_expiry_date(mid)
            if term_expiry_date:
                extended = term_expiry_date + relativedelta(months=6)
                return {
                    "kind": "ready_term_expiry",
                    "right_type": right_type,
                    "base_label": "Period ",
                    "base_date": term_expiry_date.isoformat(),
                    "expected_cycle_label": "10",
                    "expected_due_date": term_expiry_date.isoformat(),
                    "expected_extended_due_date": extended.isoformat(),
                }

            if registration_date:
                term_years = _get_term_years("TRADEMARK")
                due_date = registration_date + relativedelta(years=term_years)
                extended = due_date + relativedelta(months=6)
                return {
                    "kind": "ready_registration_date",
                    "right_type": right_type,
                    "base_label": "Registration date",
                    "base_date": registration_date.isoformat(),
                    "base_source": registration_source,
                    "expected_cycle_label": f"{term_years}",
                    "expected_due_date": due_date.isoformat(),
                    "expected_extended_due_date": extended.isoformat(),
                }

            reg_fee_paid_date = _get_reg_fee_paid_date(mid)
            if reg_fee_paid_date and _allow_reg_fee_paid_fallback_for_matter(matter, right_type):
                term_years = _get_term_years("TRADEMARK")
                due_date = reg_fee_paid_date + relativedelta(years=term_years)
                extended = due_date + relativedelta(months=6)
                return {
                    "kind": "ready_reg_fee_fallback",
                    "right_type": right_type,
                    "base_label": "Registration Payment",
                    "base_date": reg_fee_paid_date.isoformat(),
                    "base_source": "reg_fee_paid_date_fallback",
                    "expected_cycle_label": f"{term_years}",
                    "expected_due_date": due_date.isoformat(),
                    "expected_extended_due_date": extended.isoformat(),
                    "needs_review": True,
                }

            return {"kind": "missing_registration_date", "right_type": right_type}

        if registration_date:
            return {
                "kind": "ready_registration_date",
                "right_type": right_type,
                "base_label": "Registration date",
                "base_date": registration_date.isoformat(),
                "base_source": registration_source,
            }

        return {"kind": "missing_registration_date", "right_type": right_type}
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_context.annuity_empty_hint",
            log_key="case.detail_context.annuity_empty_hint",
            log_window_seconds=300,
        )
        return {"kind": "generic"}
