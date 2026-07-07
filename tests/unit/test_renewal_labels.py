from __future__ import annotations

from app.utils.renewal_labels import (
    normalize_renewal_jurisdiction,
    normalize_renewal_right_type,
    renewal_cycle_label,
    renewal_workflow_name,
)


def test_renewal_cycle_label_for_trademark_uses_renewal_wording():
    assert renewal_cycle_label(5, right_type="TRADEMARK") == "2 Registration"
    assert renewal_cycle_label(10, right_type="TRADEMARK") == "1 Updated"
    assert renewal_cycle_label(15, right_type="TRADEMARK") == "2 2 Registration"
    assert renewal_cycle_label(20, right_type="TRADEMARK") == "2 Updated"


def test_renewal_workflow_name_for_trademark_uses_trademark_label():
    assert renewal_workflow_name(5, right_type="TRADEMARK") == "Trademark 2 Registration"
    assert renewal_workflow_name(10, right_type="TRADEMARK") == "Trademark 1 Updated"


def test_renewal_label_normalizes_matter_type_aliases():
    assert normalize_renewal_right_type("US_TRADEMARK") == "TRADEMARK"
    assert normalize_renewal_right_type("26TD0001US") == "TRADEMARK"
    assert normalize_renewal_jurisdiction("US_TRADEMARK") == "USPTO"
    assert normalize_renewal_jurisdiction("26TD0001US") == "USPTO"
    assert renewal_cycle_label(10, right_type="US_TRADEMARK") == "Section 8/9 Renewal"
    assert (
        renewal_workflow_name(10, right_type="26TD0001US")
        == "Trademark Section 8/9 Renewal"
    )


def test_renewal_label_for_uspto_trademark_uses_section_wording():
    assert (
        renewal_cycle_label(6, right_type="TRADEMARK", jurisdiction="USPTO")
        == "Section 8 Declaration"
    )
    assert (
        renewal_cycle_label(10, right_type="TRADEMARK", jurisdiction="USPTO")
        == "Section 8/9 Renewal"
    )
    assert (
        renewal_workflow_name(10, right_type="TRADEMARK", jurisdiction="USPTO")
        == "Trademark Section 8/9 Renewal"
    )


def test_renewal_cycle_label_for_patent_keeps_annuity_wording():
    assert renewal_cycle_label(4, right_type="PATENT") == "4"
