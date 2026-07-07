from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.models.ip_records import Family, Matter, MatterCustomField, MatterFamily, VMatterOverview


def _query_params(href: str) -> dict[str, str]:
    parsed = parse_qs(urlparse(href).query)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def test_case_detail_family_section_has_related_create_link(authenticated_client, sample_matter):
    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    resp = authenticated_client.get(f"/case/{matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    link = soup.select_one(
        f'#sec-family a[href*="/case/matter/create/select?family_link_target_id={matter_id}"]'
    )
    assert link is not None
    assert "Create related application" in link.get_text(strip=True)


def test_case_detail_family_section_renders_network_links(admin_client, db_session, sample_matter):
    source_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    related = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text UI Text",
        right_group="DOM",
        matter_type="PATENT",
        status_blue="Text",
    )
    db_session.add_all(
        [
            related,
            VMatterOverview(
                matter_id=related.matter_id,
                our_ref=related.our_ref,
                right_name=related.right_name,
                right_group=related.right_group,
                matter_type=related.matter_type,
                status_blue=related.status_blue,
            ),
            Family(family_id="fam-ui-network", family_key="FAM-UI-NETWORK"),
            MatterFamily(
                mf_id=f"mf-{uuid.uuid4().hex}",
                matter_id=source_id,
                family_id="fam-ui-network",
                link_role="manual",
            ),
            MatterFamily(
                mf_id=f"mf-{uuid.uuid4().hex}",
                matter_id=related.matter_id,
                family_id="fam-ui-network",
                link_role="manual",
            ),
        ]
    )
    db_session.commit()

    resp = admin_client.get(f"/case/{source_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    network = soup.select_one("#sec-family .family-network")
    assert network is not None
    assert "Text Text" in network.get_text(" ", strip=True)
    assert "FAM-UI-NETWORK" in network.get_text(" ", strip=True)

    related_link = soup.select_one(
        f'#sec-family .family-network a.family-network__node--related[href*="/case/{related.matter_id}"]'
    )
    assert related_link is not None
    related_text = related_link.get_text(" ", strip=True)
    assert related.our_ref in related_text
    assert "Text" in related_text


def test_matter_create_select_propagates_family_target_id(authenticated_client):
    target_id = "TARGET-MATTER-ID"
    resp = authenticated_client.get(f"/case/matter/create/selectNewfamily_link_target_id={target_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    create_links = [(a.get("href") or "") for a in soup.select('a[href*="/case/matter/create?"]')]
    assert create_links
    assert all(f"family_link_target_id={target_id}" in href for href in create_links)


def test_matter_create_select_with_unknown_source_hides_family_mode_shortcuts(authenticated_client):
    target_id = "TARGET-MATTER-ID"
    resp = authenticated_client.get(
        f"/case/matter/create/selectNewfamily_link_target_id={target_id}&family_create_mode=pct"
    )
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    mode_links = soup.select('a[href*="family_create_mode="]')
    assert not mode_links


def test_matter_create_form_keeps_family_target_hidden_field(authenticated_client):
    target_id = "TARGET-MATTER-ID"
    resp = authenticated_client.get(
        f"/case/matter/createNewdivision=DOM&type=PATENT&family_link_target_id={target_id}"
    )
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    hidden = soup.select_one('input[name="family_link_target_id"]')
    assert hidden is not None
    assert (hidden.get("value") or "") == target_id


def test_matter_create_form_ignores_existing_family_source_without_edit_access(
    authenticated_client, db_session
):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.commit()

    resp = authenticated_client.get(
        f"/case/matter/createLegacydivision=DOM&type=PATENT&family_link_target_id={source.matter_id}&family_create_mode=priority"
    )
    assert resp.status_code == 200

    html = resp.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert "Text Text Text Text" not in html
    assert soup.select_one('input[name="family_link_target_id"]') is None
    assert soup.select_one('input[name="family_create_mode"]') is None


def test_matter_create_select_ignores_existing_family_source_without_edit_access(
    authenticated_client, db_session
):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.commit()

    resp = authenticated_client.get(
        f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}&family_create_mode=priority"
    )
    assert resp.status_code == 200

    html = resp.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert "Text Text Text Text Text" not in html
    assert not soup.select('a[href*="family_create_mode="]')
    assert all(
        f"family_link_target_id={source.matter_id}" not in (a.get("href") or "")
        for a in soup.select('a[href*="/case/matter/create?"]')
    )


def test_matter_create_ignores_incompatible_family_mode_for_target(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26TM{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(
        f"/case/matter/createLegacydivision=OUT&type=TRADEMARK&family_link_target_id={source.matter_id}&family_create_mode=pct",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    mode_hidden = soup.select_one('input[name="family_create_mode"]')
    assert mode_hidden is None


def test_matter_create_select_shows_family_mode_shortcuts(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    mode_links = {
        (a.get_text(strip=True), (a.get("href") or ""))
        for a in soup.select('a[href*="family_create_mode="]')
    }
    assert len(mode_links) == 4
    hrefs = [href for _text, href in mode_links]
    assert any("family_create_mode=priority" in href for href in hrefs)
    assert any("family_create_mode=divisional" in href for href in hrefs)
    assert any("family_create_mode=paris" in href for href in hrefs)
    assert any("family_create_mode=pct" in href for href in hrefs)


def test_matter_create_select_uses_madrid_shortcut_for_trademark_source(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26TM{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    family_links = [
        _query_params(a.get("href") or "") for a in soup.select('a[href*="family_create_mode="]')
    ]
    modes = {params.get("family_create_mode") for params in family_links}
    assert "madrid" in modes
    assert "pct" not in modes
    assert all((params.get("type") or "") != "PCT" for params in family_links)

    madrid = next(params for params in family_links if params.get("family_create_mode") == "madrid")
    assert madrid.get("division") == "ETC"
    assert madrid.get("type") == "MADRID"


def test_matter_create_select_uses_hague_shortcut_for_design_source(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26DR{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="DESIGN",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    family_links = [
        _query_params(a.get("href") or "") for a in soup.select('a[href*="family_create_mode="]')
    ]
    modes = {params.get("family_create_mode") for params in family_links}
    assert "hague" in modes
    assert "pct" not in modes
    assert all((params.get("type") or "") != "PCT" for params in family_links)

    hague = next(params for params in family_links if params.get("family_create_mode") == "hague")
    assert hague.get("division") == "ETC"
    assert hague.get("type") == "HAGUE"


def test_matter_create_select_inferrs_trademark_from_right_name_when_type_is_dirty(
    admin_client, db_session
):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26TM{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    family_links = [
        _query_params(a.get("href") or "") for a in soup.select('a[href*="family_create_mode="]')
    ]
    modes = {params.get("family_create_mode") for params in family_links}
    assert "madrid" in modes
    assert "pct" not in modes
    assert all((params.get("type") or "") != "PCT" for params in family_links)


def test_matter_create_select_inferrs_design_from_right_name_when_type_is_dirty(
    admin_client, db_session
):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26DR{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    family_links = [
        _query_params(a.get("href") or "") for a in soup.select('a[href*="family_create_mode="]')
    ]
    modes = {params.get("family_create_mode") for params in family_links}
    assert "hague" in modes
    assert "pct" not in modes
    assert all((params.get("type") or "") != "PCT" for params in family_links)


def test_matter_create_select_shows_pct_national_phase_shortcut_for_pct_source(
    admin_client, db_session
):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
        right_name="PCT Text Text",
        right_group="ETC",
        matter_type="PCT",
    )
    db_session.add(source)
    db_session.commit()

    resp = admin_client.get(f"/case/matter/create/selectNewfamily_link_target_id={source.matter_id}")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    family_links = [
        _query_params(a.get("href") or "") for a in soup.select('a[href*="family_create_mode="]')
    ]
    assert len(family_links) == 1
    assert family_links[0].get("family_create_mode") == "national_phase"
    assert family_links[0].get("division") == "OUT"
    assert family_links[0].get("type") == "PATENT"


def test_family_priority_mode_prefills_priority_fields(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="domestic_patent",
            data={
                "application_no": "10-2026-000111",
                "application_date": "2026-01-10",
                "client_name": "Text",
            },
        )
    )
    db_session.commit()

    resp = admin_client.get(
        f"/case/matter/createLegacydivision=DOM&type=PATENT&family_link_target_id={source.matter_id}&family_create_mode=priority"
    )
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")

    mode_hidden = soup.select_one('input[name="family_create_mode"]')
    assert mode_hidden is not None
    assert (mode_hidden.get("value") or "") == "priority"

    priority_no = soup.select_one('input[name="priority_no"]')
    assert priority_no is not None
    assert (priority_no.get("value") or "") == "10-2026-000111"

    priority_date = soup.select_one('input[name="priority_date"]')
    assert priority_date is not None
    assert (priority_date.get("value") or "") == "2026-01-10"

    filing_type_selected = soup.select_one('select[name="filing_type"] option[selected]')
    assert filing_type_selected is not None
    assert (filing_type_selected.get("value") or "") == "Priority Filing"


def test_family_paris_mode_prefills_outgoing_route(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="domestic_patent",
            data={
                "application_no": "10-2026-000222",
                "application_date": "2026-02-03",
            },
        )
    )
    db_session.commit()

    resp = admin_client.get(
        f"/case/matter/createLegacydivision=OUT&type=PATENT&family_link_target_id={source.matter_id}&family_create_mode=paris"
    )
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")

    app_route_selected = soup.select_one('select[name="app_route"] option[selected]')
    assert app_route_selected is not None
    assert (app_route_selected.get("value") or "") == "items"

    priority_no = soup.select_one('input[name="priority_no"]')
    assert priority_no is not None
    assert (priority_no.get("value") or "") == "10-2026-000222"

    priority_date = soup.select_one('input[name="priority_date"]')
    assert priority_date is not None
    assert (priority_date.get("value") or "") == "2026-02-03"


def test_family_pct_mode_is_inferred_for_domestic_to_pct_target(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="domestic_patent",
            data={
                "application_no": "10-2026-000333",
                "application_date": "2026-02-10",
            },
        )
    )
    db_session.commit()

    resp = admin_client.get(
        f"/case/matter/createNewdivision=ETC&type=PCT&family_link_target_id={source.matter_id}"
    )
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")

    mode_hidden = soup.select_one('input[name="family_create_mode"]')
    assert mode_hidden is not None
    assert (mode_hidden.get("value") or "") == "pct"

    priority_no = soup.select_one('input[name="priority_no"]')
    assert priority_no is not None
    assert (priority_no.get("value") or "") == "10-2026-000333"

    priority_date = soup.select_one('input[name="priority_date"]')
    assert priority_date is not None
    assert (priority_date.get("value") or "") == "2026-02-10"


def test_family_national_phase_mode_prefills_pct_and_priority_fields(admin_client, db_session):
    source = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26PD{uuid.uuid4().hex[:4].upper()}PCT",
        right_name="PCT Text Text",
        right_group="ETC",
        matter_type="PCT",
    )
    db_session.add(source)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(source.matter_id),
            namespace="pct",
            data={
                "application_no": "PCT/US2026/000777",
                "application_date": "2026-03-05",
                "priority_no": "10-2025-0099999",
                "priority_date": "2025-03-05",
            },
        )
    )
    db_session.commit()

    resp = admin_client.get(
        f"/case/matter/createNewdivision=OUT&type=PATENT&family_link_target_id={source.matter_id}"
    )
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")

    mode_hidden = soup.select_one('input[name="family_create_mode"]')
    assert mode_hidden is not None
    assert (mode_hidden.get("value") or "") == "national_phase"

    pct_no = soup.select_one('input[name="pct_application_no"]')
    assert pct_no is not None
    assert (pct_no.get("value") or "") == "PCT/US2026/000777"

    pct_date = soup.select_one('input[name="pct_application_date"]')
    assert pct_date is not None
    assert (pct_date.get("value") or "") == "2026-03-05"

    priority_no = soup.select_one('input[name="priority_no"]')
    assert priority_no is not None
    assert (priority_no.get("value") or "") == "10-2025-0099999"

    priority_date = soup.select_one('input[name="priority_date"]')
    assert priority_date is not None
    assert (priority_date.get("value") or "") == "2025-03-05"

    app_route_selected = soup.select_one('select[name="app_route"] option[selected]')
    assert app_route_selected is not None
    assert (app_route_selected.get("value") or "") == "PCT-NP"
