from urllib.parse import urlparse


def test_status_wizard_redirect_respects_script_root(admin_client, sample_matter):
    """
    Ensure status-wizard POST returns to the case detail endpoint via url_for(),
    so reverse-proxy SCRIPT_NAME prefixes are preserved.
    """
    matter_id = getattr(sample_matter, "_test_matter_id", str(sample_matter.matter_id))

    response = admin_client.post(
        f"/case/matter/{matter_id}/status-wizard",
        data={
            "new_status": "OPEN",
            "preset": "OA",
            "create_dockets": "n",
            "create_workflows": "n",
        },
        follow_redirects=False,
        environ_overrides={"SCRIPT_NAME": "/ipm"},
    )

    assert response.status_code in (302, 303)
    location_path = urlparse(response.headers.get("Location") or "").path
    assert location_path == f"/ipm/case/{matter_id}"
