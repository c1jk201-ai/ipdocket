from __future__ import annotations

from flask import Flask

from app.security.headers import init_security_headers


def _build_app(*, allow_sameorigin: bool = True, csp_mode: str | None = "ENFORCE") -> Flask:
    app = Flask(__name__)
    config = {
        "SECURITY_HEADERS_ENABLED": True,
        "SECURITY_FRAME_ALLOW_SAMEORIGIN": allow_sameorigin,
        "CSP_POLICY": "default-src 'self'; frame-ancestors 'none';",
        "CSP_REPORT_URI": "",
    }
    if csp_mode is not None:
        config["CSP_MODE"] = csp_mode
    app.config.update(config)
    init_security_headers(app)

    @app.get("/files/<string:file_id>/preview")
    def preview(file_id: str):
        return f"preview:{file_id}"

    @app.get("/normal")
    def normal():
        return "ok"

    return app


def test_preview_endpoint_allows_same_origin_frame_headers():
    app = _build_app()
    client = app.test_client()

    resp = client.get("/files/abc123/preview")

    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = resp.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'self'" in csp
    assert "frame-ancestors 'none'" not in csp


def test_csp_defaults_to_enforce_when_mode_is_unset():
    app = _build_app(csp_mode=None)
    client = app.test_client()

    resp = client.get("/normal")

    assert resp.status_code == 200
    assert resp.headers.get("Content-Security-Policy")
    assert "Content-Security-Policy-Report-Only" not in resp.headers


def test_normal_endpoint_allows_same_origin_frame_by_default():
    app = _build_app()
    client = app.test_client()

    resp = client.get("/normal")

    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = resp.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'self'" in csp


def test_popup_query_allows_same_origin_frame_for_other_pages():
    app = _build_app(allow_sameorigin=False)
    client = app.test_client()

    resp = client.get("/normal?popup=1")

    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = resp.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'self'" in csp


def test_strict_mode_denies_normal_endpoint_without_popup():
    app = _build_app(allow_sameorigin=False)
    client = app.test_client()

    resp = client.get("/normal")

    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "DENY"
    csp = resp.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'none'" in csp
