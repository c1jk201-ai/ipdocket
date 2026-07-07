from __future__ import annotations

import pytest
from bs4 import BeautifulSoup


def _assert_fields_present(client, url: str, field_names: list[str]) -> None:
    resp = client.get(url)
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    for field_name in field_names:
        assert soup.select_one(f'[name="{field_name}"]') is not None


def test_dom_patent_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=DOM&type=PATENT",
        ["pct_deadline", "abandon_date"],
    )


def test_dom_design_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=DOM&type=DESIGN",
        ["related_applications", "common_memo"],
    )


def test_dom_trademark_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=DOM&type=TRADEMARK",
        ["tm_registration_payment_term", "memo2", "stand_reason"],
    )


def test_inc_patent_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=INC&type=PATENT",
        ["apply_plan_date", "trans_expend_date"],
    )


def test_inc_design_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=INC&type=DESIGN",
        ["trans_charge", "trans_expend_date"],
    )


def test_inc_trademark_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=INC&type=TRADEMARK",
        ["tm_registration_payment_term", "apply_plan_date", "trans_expend_date"],
    )


def test_out_patent_create_renders_tail_fields(authenticated_client):
    _assert_fields_present(
        authenticated_client,
        "/case/matter/create?division=OUT&type=PATENT",
        ["trans_charge", "trans_expend_date"],
    )


@pytest.mark.parametrize(
    ("url", "panel_id", "link_label"),
    [
        ("/case/matter/create?division=DOM&type=PATENT", "caseCategoryDomestic", "Patent"),
        ("/case/matter/create?division=DOM&type=TRADEMARK", "caseCategoryDomestic", "Trademark"),
        ("/case/matter/create?division=DOM&type=DESIGN", "caseCategoryDomestic", "Design"),
        ("/case/matter/create?division=DOM&type=UTILITY", "caseCategoryDomestic", "Utility"),
        ("/case/matter/create?division=INC&type=PATENT", "caseCategoryIncoming", "Patent"),
        ("/case/matter/create?division=INC&type=TRADEMARK", "caseCategoryIncoming", "Trademark"),
        ("/case/matter/create?division=INC&type=DESIGN", "caseCategoryIncoming", "Design"),
        ("/case/matter/create?division=INC&type=UTILITY", "caseCategoryIncoming", "Utility"),
        ("/case/matter/create?division=OUT&type=PATENT", "caseCategoryOverseas", "Patent"),
        ("/case/matter/create?division=OUT&type=DESIGN", "caseCategoryOverseas", "Design"),
        ("/case/matter/create?division=OUT&type=UTILITY", "caseCategoryOverseas", "Utility"),
        ("/case/matter/create?division=ETC&type=PCT", "caseCategoryOther", "PCT"),
        ("/case/matter/create?division=ETC&type=MADRID", "caseCategoryOther", "Madrid"),
        ("/case/matter/create?division=ETC&type=HAGUE", "caseCategoryOther", "Hague"),
        ("/case/matter/create?division=ETC&type=COPYRIGHT", "caseCategoryOther", "Copyright"),
        (
            "/case/matter/create?division=ETC&type=LITIGATION",
            "caseCategoryOther",
            "Proceedings / Litigation",
        ),
        ("/case/matter/create?division=ETC&type=MISC", "caseCategoryOther", "Other"),
        ("/case/matter/create?division=OUT&type=PCT", "caseCategoryOther", "PCT"),
        ("/case/matter/create?division=OUT&type=TRADEMARK", "caseCategoryOther", "Madrid"),
        (
            "/case/matter/create?division=OUT&type=DESIGN&app_route=HAGUE",
            "caseCategoryOther",
            "Hague",
        ),
    ],
)
def test_create_matter_marks_matching_case_menu_active(authenticated_client, url, panel_id, link_label):
    resp = authenticated_client.get(url, follow_redirects=True)
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    expected_panel = soup.select_one(f"#{panel_id}")
    expected_link = None
    if expected_panel is not None:
        for link in expected_panel.select("a.nav-link"):
            if link.get_text(" ", strip=True) == link_label:
                expected_link = link
                break

    assert expected_panel is not None
    assert expected_link is not None
    assert "show" in (expected_panel.get("class") or [])
    assert "active" in (expected_link.get("class") or [])

    for other_panel_id in {
        "caseCategoryDomestic",
        "caseCategoryIncoming",
        "caseCategoryOverseas",
        "caseCategoryOther",
    } - {panel_id}:
        other_panel = soup.select_one(f"#{other_panel_id}")
        assert other_panel is not None
        assert "show" not in (other_panel.get("class") or [])
