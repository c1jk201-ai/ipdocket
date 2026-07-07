from __future__ import annotations


def test_admin_upload_runs_redirects_to_automation_queue(admin_client):
    res = admin_client.get("/admin/upload-runsNewdays=30&limit=100")

    assert res.status_code == 302
    location = res.headers["Location"]
    assert "/doc/automation-queue" in location
    assert "days=30" in location
    assert "status=ALL" in location


def test_admin_upload_runs_preserves_queue_compatible_status(admin_client):
    res = admin_client.get("/admin/upload-runsNewdays=14&status=BLOCKED")

    assert res.status_code == 302
    location = res.headers["Location"]
    assert "/doc/automation-queue" in location
    assert "days=14" in location
    assert "status=BLOCKED" in location
