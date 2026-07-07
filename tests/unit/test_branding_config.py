def test_branding_context_reads_system_config(app, db_session):
    from app.models.system_config import SystemConfig
    from app.services.core.branding import get_branding
    from app.services.core.config_service import ConfigService

    SystemConfig.set_config("BRAND_APP_NAME", "Example Firm Docket")
    SystemConfig.set_config("BRAND_SHORT_NAME", "Example IP")
    SystemConfig.set_config("BRAND_LOGO_PATH", "branding/example-logo.png")
    SystemConfig.set_config("BRAND_FAVICON_PATH", "branding/example-favicon.png")
    SystemConfig.set_config("BRAND_PRIMARY_COLOR", "#123456")
    SystemConfig.set_config("BRAND_ACCENT_COLOR", "#abcdef")
    db_session.commit()
    ConfigService.clear_cache()

    branding = get_branding()

    assert branding.app_name == "Example Firm Docket"
    assert branding.short_name == "Example IP"
    assert branding.logo_path == "branding/example-logo.png"
    assert branding.favicon_path == "branding/example-favicon.png"
    assert branding.primary_color == "#123456"
    assert branding.primary_rgb == "18, 52, 86"
    assert "--app-indigo: #123456" in branding.style
    assert "--app-accent: #abcdef" in branding.style


def test_admin_config_page_has_branding_controls(admin_client):
    res = admin_client.get("/admin/config")

    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert 'id="branding-settings-form"' in body
    assert 'id="brand-logo-path"' in body
    assert 'data-brand-upload="logo"' in body
    assert 'data-brand-upload="favicon"' in body
