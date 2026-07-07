from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


def _extract_create_href(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", string=lambda s: bool(s and s.strip() == "Create Case"))
    assert link is not None
    return link.get("href") or ""


def test_pct_list_create_button_uses_etc_public_classification(authenticated_client):
    resp = authenticated_client.get("/case/etc/pct")
    assert resp.status_code == 200

    href = _extract_create_href(resp.get_data(as_text=True))
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["PCT"]


def test_madrid_list_create_button_presets_app_route(authenticated_client):
    resp = authenticated_client.get("/case/etc/madrid")
    assert resp.status_code == 200

    href = _extract_create_href(resp.get_data(as_text=True))
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["MADRID"]


def test_hague_list_create_button_presets_app_route(authenticated_client):
    resp = authenticated_client.get("/case/etc/hague")
    assert resp.status_code == 200

    href = _extract_create_href(resp.get_data(as_text=True))
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["HAGUE"]


def test_copyright_list_create_button_presets_misc_copyright(authenticated_client):
    resp = authenticated_client.get("/case/etc/copyright")
    assert resp.status_code == 200

    href = _extract_create_href(resp.get_data(as_text=True))
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    assert parsed.path.endswith("/case/matter/create")
    assert params.get("division") == ["ETC"]
    assert params.get("type") == ["COPYRIGHT"]
