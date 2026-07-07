from __future__ import annotations


def test_automation_inbox_applied_filter_redirects_to_queue(admin_client):
    res = admin_client.get("/admin/automation/inboxNewstatus=APPLIED")

    assert res.status_code == 302
    location = res.headers["Location"]
    assert "/doc/automation-queue" in location
    assert "status=APPLIED" in location


def test_automation_inbox_default_filter_redirects_to_review_ready_queue(admin_client):
    res = admin_client.get("/admin/automation/inbox")

    assert res.status_code == 302
    location = res.headers["Location"]
    assert "/doc/automation-queue" in location
    assert ("status=REVIEW,READY" in location) or ("status=REVIEW%2CREADY" in location)
