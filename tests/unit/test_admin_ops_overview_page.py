from datetime import datetime, timedelta

from app.models.annuity_workflow_sync_queue import AnnuityWorkflowSyncQueue
from app.models.error_report import ErrorReport
from app.models.job_run import JobRun
from app.models.notification import NotificationLog
from app.models.ip_records import EmailIngestionLog
from app.models.user_access_log import UserAccessLog
from app.ops.models import DiskSample, DurableJob


def test_admin_ops_overview_page_renders_monitoring_hub(
    admin_client, db_session, admin_user, monkeypatch
):
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService

    now = datetime.utcnow()
    user_id = getattr(admin_user, "_test_id", None) or admin_user.id
    monkeypatch.setenv("ADMIN_CIDR_ALLOWLIST", "127.0.0.1/32")
    monkeypatch.setenv("CIDR_GUARD_ENABLED", "1")
    SystemConfig.set_config("CIDR_GUARD_ENABLED", "false")
    db_session.commit()
    ConfigService.clear_cache()

    db_session.add_all(
        [
            UserAccessLog(
                user_id=user_id,
                method="GET",
                path="/admin",
                endpoint="admin.index",
                status_code=200,
                duration_ms=240,
                created_at=now - timedelta(minutes=5),
            ),
            ErrorReport(
                user_id=user_id,
                method="GET",
                path="/case/list",
                endpoint="case_work.list_cases",
                status_code=500,
                error_type="RuntimeError",
                message="boom",
                created_at=now - timedelta(minutes=3),
            ),
            EmailIngestionLog(
                provider="imap",
                mailbox="INBOX",
                fetched_count=2,
                ingested_count=1,
                duplicate_count=0,
                error_code="exception",
                created_at=now - timedelta(hours=2),
            ),
            NotificationLog(
                entity_type="docket_item",
                entity_id="d-1",
                channel="email",
                days_before=7,
                due_date=(now + timedelta(days=7)).date(),
                recipient="owner@example.com",
                status="failed",
                sent_at=now - timedelta(minutes=2),
            ),
            DurableJob(
                queue="email",
                task="email_ingestion",
                payload={},
                status="failed",
                run_at=now - timedelta(minutes=15),
                last_error="mailbox timeout",
                updated_at=now - timedelta(minutes=10),
            ),
            DurableJob(
                queue="calendar",
                task="calendar_sync",
                payload={},
                status="queued",
                run_at=now - timedelta(minutes=8),
                updated_at=now - timedelta(minutes=8),
            ),
            JobRun(
                job_name="scheduler_heartbeat",
                run_id="hb-1",
                status="success",
                started_at=now - timedelta(minutes=4),
                finished_at=now - timedelta(minutes=4),
            ),
            JobRun(
                job_name="daily_annuity_generation",
                run_id="annuity-1",
                status="failed",
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(hours=1),
                error="generation failure",
            ),
            JobRun(
                job_name="retention.cleanup",
                run_id="retention-1",
                status="success",
                started_at=now - timedelta(hours=6),
                finished_at=now - timedelta(hours=6),
                output_ref='{"total_removed": 3, "total_errors": 1, "uploads": {"removed": 2, "errors": 1, "error_samples": ["uploads failed"]}}',
            ),
            DiskSample(
                mount_label="uploads",
                path="/data/uploads",
                total_bytes=1000,
                used_bytes=650,
                free_bytes=350,
                used_pct=65.0,
                sampled_at=now - timedelta(hours=6),
            ),
            DiskSample(
                mount_label="backups",
                path="/data/backups",
                total_bytes=1000,
                used_bytes=420,
                free_bytes=580,
                used_pct=42.0,
                sampled_at=now - timedelta(hours=6),
            ),
            AnnuityWorkflowSyncQueue(
                matter_id="matter-health-1",
                next_run_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=20),
            ),
        ]
    )
    db_session.commit()

    res = admin_client.get("/admin/ops")

    assert res.status_code == 200, f"Response: {res.data}"
    html = res.get_data(as_text=True)

    assert "Operations Monitoring" in html
    assert "Service Health" in html
    assert "Durable Queue" in html
    assert "Recent Failed Queue Actions" in html
    assert "Retention" in html
    assert "Renewal Operations" in html
    assert "email ingestion 7d: 1/1 failed" in html
    assert "email_ingestion" in html
    assert "mailbox timeout" in html
    assert "daily_annuity_generation" in html or "Daily Annuity Auto-Generation" in html
