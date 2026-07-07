from __future__ import annotations


def test_admin_index_page_renders_hub_links(admin_client, monkeypatch):
    from app.services.core.config_service import ConfigService

    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    ConfigService.clear_cache()

    res = admin_client.get(
        "/admin",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )

    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Core Admin" in html
    assert "System Settings" in html
    assert "Staff & Users" in html
    assert "Roles & Permissions" in html
    assert "Security Settings" in html
    assert "Error Reports" in html
    assert 'href="/admin/config"' in html
    assert 'href="/admin/users"' in html
    assert 'href="/admin/roles"' in html
    assert 'href="/admin/security/health"' in html
    assert 'href="/admin/errors"' in html
    assert "Deadline Rules" not in html
    assert "Task Playbooks" not in html
    assert "Operations Monitoring" not in html
    assert 'id="adminToolSearch"' not in html
    assert 'class="admin-nav-details"' not in html
    assert 'href="/workflow/playbooks"' not in html
    assert 'href="/admin/data-quality"' not in html
    assert "/admin/codes" not in html
