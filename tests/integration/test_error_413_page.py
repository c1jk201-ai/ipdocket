import io


def test_request_entity_too_large_renders_friendly_html(app):
    old_max_content_length = app.config.get("MAX_CONTENT_LENGTH")
    app._got_first_request = False

    @app.post("/_test/request-too-large")
    def _request_too_large():
        from flask import request

        request.files["file"]
        return "ok"

    app.config["MAX_CONTENT_LENGTH"] = 10

    try:
        with app.test_client() as client:
            resp = client.post(
                "/_test/request-too-large",
                data={"file": (io.BytesIO(b"x" * 32), "large.pdf")},
                content_type="multipart/form-data",
            )
    finally:
        app.config["MAX_CONTENT_LENGTH"] = old_max_content_length

    assert resp.status_code == 413
    html = resp.get_data(as_text=True)
    assert "Upload too large" in html
    assert "Current upload limit:" in html
