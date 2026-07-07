from __future__ import annotations

from app.blueprints.help import routes as help_routes


def test_help_page_loads_employee_manual(admin_client):
    res = admin_client.get("/help/")
    assert res.status_code == 200

    html = res.data.decode("utf-8")
    assert "IPM Help" in html
    assert 'id="helpFilterInput"' in html
    assert "Quick Start" in html
    assert "embedded://help-manual" in html


def test_help_page_uses_embedded_manual_when_files_missing(admin_client, monkeypatch):
    monkeypatch.setattr(help_routes, "_help_doc_candidates", lambda _root: [])

    res = admin_client.get("/help/")
    assert res.status_code == 200

    html = res.data.decode("utf-8")
    assert "embedded://help-manual" in html
    assert "IPM Help" in html
    assert "Quick Start" in html
