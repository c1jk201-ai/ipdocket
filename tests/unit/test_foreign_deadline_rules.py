from __future__ import annotations

from app.services.deadlines.foreign_deadline_rules import compute_response_deadline


def test_compute_response_deadline_parses_iso_date_and_period_months():
    deadline, reason = compute_response_deadline(
        jurisdiction="US",
        doc_type="OA",
        mailing_date="2025-01-15T09:00:00",
        oa_date=None,
        response_period="2 months",
        rules_path="",
        holidays_path="",
    )

    assert deadline == "2025-03-15"
    assert reason == "rule_months"


def test_compute_response_deadline_parses_day_period():
    deadline, reason = compute_response_deadline(
        jurisdiction="US",
        doc_type="OA",
        mailing_date="2025-01-01",
        oa_date=None,
        response_period="30 days",
        rules_path="",
        holidays_path="",
    )

    assert deadline == "2025-01-31"
    assert reason == "rule_days"
