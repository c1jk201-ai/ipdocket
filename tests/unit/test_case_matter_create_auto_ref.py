from __future__ import annotations

from bs4 import BeautifulSoup


def test_pct_create_page_shows_auto_ref_button(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=PCT")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"][data-type="PCT"]')
    assert btn is not None
    assert (btn.get("data-country") or "") == "PCT"


def test_domestic_trademark_create_page_uses_configured_ref_button_without_old_segments(
    authenticated_client,
):
    resp = authenticated_client.get("/case/matter/createNewdivision=DOM&type=TRADEMARK")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    assert soup.select_one("#ourRefYY") is None
    assert soup.select_one("#ourRefNum") is None
    our_ref_input = soup.select_one("#ourRefInput")
    assert our_ref_input is not None
    our_ref_col = our_ref_input.find_parent("div", class_="col-md-3")
    assert our_ref_col is not None
    assert "TD" not in our_ref_col.get_text()
    btn = soup.select_one('[data-auto-our-ref="1"][data-type="TRADEMARK"]')
    assert btn is not None
    assert (btn.get("data-target-id") or "") == "ourRefInput"


def test_madrid_create_page_auto_ref_uses_public_madrid_type(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=MADRID")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"]')
    assert btn is not None
    assert (btn.get("data-type") or "") == "MADRID"


def test_hague_create_page_auto_ref_uses_public_hague_type(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=HAGUE")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"]')
    assert btn is not None
    assert (btn.get("data-type") or "") == "HAGUE"


def test_copyright_create_page_auto_ref_uses_public_copyright_type(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=COPYRIGHT")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"]')
    assert btn is not None
    assert (btn.get("data-type") or "") == "COPYRIGHT"


def test_litigation_create_page_shows_auto_ref_button(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=LITIGATION")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"][data-type="LITIGATION"]')
    assert btn is not None
    assert (btn.get("data-country") or "") == "US"


def test_misc_create_page_shows_auto_ref_button(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=MISC")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    btn = soup.select_one('[data-auto-our-ref="1"][data-type="MISC"]')
    assert btn is not None
    assert (btn.get("data-country") or "") == "US"
