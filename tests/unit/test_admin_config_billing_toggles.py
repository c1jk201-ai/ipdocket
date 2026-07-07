def test_admin_config_page_shows_billing_feature_toggles(admin_client, db_session):
    response = admin_client.get("/admin/config")

    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert "Enable expense, ledger, and sales tax reports" in html
    assert "Log invoice timeline to matter notes" in html
    assert "Login, access control, billing, and raw configuration." in html
