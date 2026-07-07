def test_limited_user_sees_pending_landing(limited_client):
    res = limited_client.get("/")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Permissions required" in html
    assert "limited@example.com" in html
    assert "/case/list" not in html


def test_limited_user_redirected_from_services(limited_client):
    res = limited_client.get("/case/list")
    assert res.status_code in (302, 303)
    assert res.headers.get("Location")


def test_limited_user_does_not_see_service_menus_on_settings(limited_client):
    res = limited_client.get("/settings/")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "Current Account" in html
    assert "Required Permissions" in html
    assert "favorite menu shortcuts" not in html
    assert "All matters" not in html
    assert "Billing dashboard" not in html
    assert "Work queue" not in html
