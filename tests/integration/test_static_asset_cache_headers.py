from __future__ import annotations


def test_versioned_static_asset_cache_headers_do_not_vary_on_cookie(client) -> None:
    response = client.get("/static/css/app.cssNewv=test-version")

    assert response.status_code == 200
    cache_control = response.headers.get("Cache-Control", "")
    assert "public" in cache_control
    assert "max-age=31536000" in cache_control
    assert "immutable" in cache_control
    assert "cookie" not in response.headers.get("Vary", "").lower()
