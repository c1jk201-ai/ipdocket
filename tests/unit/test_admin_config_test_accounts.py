def test_admin_config_page_renders_test_account_controls(admin_client):
    res = admin_client.get("/admin/config")
    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert 'class="admin-page-jumpbar mb-4"' in body
    assert 'href="#config-login"' in body
    assert 'href="#config-tax-invoice"' in body
    assert 'id="test-account-form"' in body
    assert "/auth/test-login" in body


def test_admin_config_page_marks_raw_settings_as_advanced(admin_client):
    res = admin_client.get("/admin/config")
    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert 'href="#config-raw"' in body
    assert "Raw Settings List (Advanced)" in body
    assert "Raw Settings Add (Advanced)" in body


def test_admin_config_page_links_dedicated_case_menu_editor(admin_client):
    res = admin_client.get("/admin/config")
    assert res.status_code == 200
    body = (res.data or b"").decode("utf-8", errors="ignore")
    assert "/admin/matter-create-menu" in body
    assert "Open Matter Create Menu" in body
    assert "/admin/case-parameters" in body
    assert '<div id="case-menu-editor"' not in body
