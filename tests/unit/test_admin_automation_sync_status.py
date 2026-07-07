from __future__ import annotations

import uuid
from datetime import datetime


def test_admin_automation_changeset_sync_status_pending_then_completed(
    app, db_session, admin_client, sample_matter
):
    from app.models.email_automation import AutomationChangeSet
    from app.ops.models import DurableJob

    cs = AutomationChangeSet(
        id=uuid.uuid4().hex,
        run_id=uuid.uuid4().hex,
        matter_id=str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id),
        docket_upserts=[{"name_ref": "US:OA", "category": "WORK", "due_date": "2026-02-20"}],
        applied=True,
        applied_at=datetime.utcnow(),
        applied_by="admin",
        rollback_key=uuid.uuid4().hex,
    )
    db_session.add(cs)
    db_session.commit()
    cs_id = cs.id

    job = DurableJob(
        queue="deferred",
        task="deferred.sync",
        payload={
            "docket_queue": {},
            "annuity_queue": {},
            "workflow_queue": {},
            "_meta": {"change_set_id": cs.id},
        },
        status="queued",
        attempts=1,
        max_attempts=5,
        last_error="ValueError: docket sync failed",
    )
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    res = admin_client.get(f"/admin/api/automation/changeset/{cs_id}/sync_status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["status"] == "pending"
    assert data["jobs"]
    assert data["jobs"][0]["id"] == job_id
    assert data["jobs"][0]["retry_cause"] == "ValueError: docket sync failed"
    assert data["jobs"][0]["retry_state"] in {"retry_ready", "retry_waiting"}
    assert data["jobs"][0]["next_retry_at"]

    # The request may clear the scoped session; reload the job before mutating.
    fresh = db_session.get(DurableJob, job_id)
    assert fresh is not None
    fresh.status = "succeeded"
    db_session.add(fresh)
    db_session.commit()

    res2 = admin_client.get(f"/admin/api/automation/changeset/{cs_id}/sync_status")
    assert res2.status_code == 200
    data2 = res2.get_json()
    assert data2["success"] is True
    assert data2["status"] == "completed"
