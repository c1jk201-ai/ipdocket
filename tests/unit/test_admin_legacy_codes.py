def test_admin_index_does_not_link_legacy_code_management(admin_client):
    response = admin_client.get("/admin")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Legacy Code Registry" not in html
    assert "/admin/codes" not in html


def test_legacy_codes_page_explains_disabled_registry(admin_client):
    response = admin_client.get("/admin/codes")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Legacy code registry is no longer editable." in html
    assert "moved into dedicated pages or operational data" in html
    assert "Current Destinations" in html
    assert "Roles & Permissions" in html
    assert "Deadline Rules" in html
    assert "code-tabs" not in html


def test_legacy_codes_api_write_is_disabled(admin_client):
    response = admin_client.post(
        "/admin/api/codes",
        json={"group_id": "ROLES", "code": "TEMP", "name": "Temporary"},
    )

    assert response.status_code == 410
    assert response.get_json()["error"] == "legacy_code_registry_disabled"


def test_legacy_code_groups_api_write_is_disabled(admin_client):
    response = admin_client.post(
        "/admin/api/codes/groups",
        json={"code": "ROLES", "name": "Roles"},
    )

    assert response.status_code == 410
    assert response.get_json()["error"] == "legacy_code_registry_disabled"
