from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


def test_matter_create_select_has_hague_shortcut(authenticated_client):
    resp = authenticated_client.get("/case/matter/create/select")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    hague_link = None
    for a in soup.select('a[href*="/case/matter/create?"]'):
        href = a.get("href") or ""
        if "division=ETC" in href and "type=HAGUE" in href:
            hague_link = a
            break

    assert hague_link is not None
    assert "Hague" in hague_link.get_text(" ", strip=True)


def test_hague_case_list_page_is_accessible(authenticated_client):
    resp = authenticated_client.get("/case/etc/hague")
    assert resp.status_code == 200
    assert "Hague" in resp.get_data(as_text=True)


def test_hague_legacy_list_page_redirects_to_etc_path(authenticated_client):
    resp = authenticated_client.get("/case/hague", follow_redirects=False)
    assert resp.status_code == 302
    assert (resp.headers.get("Location") or "").endswith("/case/etc/hague")


def test_hague_create_page_prefills_app_route(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=HAGUE")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    forced = soup.select_one('input[name="_forced_app_route"]')
    assert forced is not None
    assert (forced.get("value") or "") == "HAGUE"

    selected = soup.select_one("select:disabled option[selected]")
    assert selected is not None
    assert (selected.get("value") or "") == "HAGUE"

    hague_no = soup.select_one('input[name="hague_application_no"]')
    assert hague_no is not None
    hague_date = soup.select_one('input[name="hague_application_date"]')
    assert hague_date is not None


def test_hague_legacy_create_page_redirects_to_etc_query(authenticated_client):
    resp = authenticated_client.get(
        "/case/matter/createNewdivision=OUT&type=DESIGN&app_route=HAGUE",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    location = resp.headers.get("Location") or ""
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["HAGUE"]


def test_madrid_create_page_prefills_foreign_trademark_fields(authenticated_client):
    resp = authenticated_client.get("/case/matter/createNewdivision=ETC&type=MADRID")
    assert resp.status_code == 200

    soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
    forced = soup.select_one('input[name="_forced_app_route"]')
    assert forced is None

    app_route = soup.select_one('select[name="app_route"]')
    assert app_route is not None
    assert app_route.get("disabled") is None

    madrid_no = soup.select_one('input[name="madrid_application_no"]')
    assert madrid_no is not None
    madrid_date = soup.select_one('input[name="madrid_application_date"]')
    assert madrid_date is not None


def test_madrid_legacy_create_page_redirects_to_etc_query(authenticated_client):
    resp = authenticated_client.get(
        "/case/matter/createNewdivision=OUT&type=TRADEMARK&app_route=%EB%A7%88%EB%93%9C%EB%A6%AC%EB%93%9C",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    location = resp.headers.get("Location") or ""
    parsed = urlparse(location)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["MADRID"]
