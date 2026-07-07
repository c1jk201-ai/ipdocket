from __future__ import annotations

from app.utils.annuity_deadline_routing import (
    calendar_endpoint_for_docket,
    is_annuity_status_red_deadline,
)


def test_is_annuity_status_red_deadline_true_for_annuity_label():
    assert is_annuity_status_red_deadline(
        name_ref="MGMT:STATUS_RED:4RenewalDeadline",
        title="4RenewalDeadline",
    )


def test_is_annuity_status_red_deadline_false_for_non_annuity_status_red():
    assert not is_annuity_status_red_deadline(
        name_ref="MGMT:STATUS_RED:Text",
        title="Text",
    )


def test_is_annuity_status_red_deadline_false_for_partial_match_label():
    assert not is_annuity_status_red_deadline(
        name_ref="MGMT:STATUS_RED:Text",
        title="Text",
    )


def test_calendar_endpoint_for_annuity_status_red_is_renewal():
    assert (
        calendar_endpoint_for_docket(
            name_ref="MGMT:STATUS_RED:10RenewalDeadline",
            title="10RenewalDeadline",
        )
        == "annuities.calendar_month"
    )


def test_calendar_endpoint_for_general_docket_is_deadline():
    assert (
        calendar_endpoint_for_docket(
            name_ref="NOTICE:OA:123",
            title="Text Text Text",
        )
        == "deadlines.calendar_month"
    )
