def test_help_requires_login(client):
    res = client.get("/help/", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/auth/login" in (res.headers.get("Location") or "")


def test_help_page_renders_employee_manual(authenticated_client):
    res = authenticated_client.get("/help/")
    assert res.status_code == 200
    body = res.data.decode("utf-8", errors="replace")
    assert "IPM Help" in body
    assert 'id="helpFilterInput"' in body
    assert "Quick Start" in body
