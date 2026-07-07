from datetime import datetime, timedelta

from app.models.job_run import JobRun
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.ops.models import DiskSample, DurableJob


def test_housekeeping_deletes_high_volume_operational_tables(app, db_session):
    from app.services.ops.housekeeping import (
        cleanup_disk_samples,
        cleanup_durable_jobs,
        cleanup_success_job_runs,
        cleanup_user_access_logs,
    )

    now = datetime.utcnow()
    user = User(username="ops-user", email="ops@example.com", role="admin", is_active=True)
    db_session.add(user)
    db_session.flush()

    old = now - timedelta(days=120)
    very_old = now - timedelta(days=240)
    recent = now - timedelta(days=2)
    db_session.add_all(
        [
            DurableJob(
                queue="calendar",
                task="calendar.sync",
                payload={"entity_id": 1},
                status="succeeded",
                created_at=old,
                updated_at=old,
                finished_at=old,
            ),
            DurableJob(
                queue="calendar",
                task="calendar.sync",
                payload={"entity_id": 2},
                status="failed",
                created_at=very_old,
                updated_at=very_old,
                finished_at=very_old,
            ),
            DurableJob(
                queue="calendar",
                task="calendar.sync",
                payload={"entity_id": 3},
                status="queued",
                created_at=old,
                updated_at=old,
                run_at=old,
            ),
            JobRun(
                job_name="job-old-success",
                run_id="job-old-success",
                status="success",
                started_at=old,
                finished_at=old,
            ),
            JobRun(
                job_name="job-old-failed",
                run_id="job-old-failed",
                status="failed",
                started_at=old,
                finished_at=old,
            ),
            JobRun(
                job_name="job-recent-success",
                run_id="job-recent-success",
                status="success",
                started_at=recent,
                finished_at=recent,
            ),
            UserAccessLog(
                user_id=user.id,
                method="GET",
                path="/old",
                status_code=200,
                created_at=old,
            ),
            UserAccessLog(
                user_id=user.id,
                method="GET",
                path="/recent",
                status_code=200,
                created_at=recent,
            ),
            DiskSample(
                mount_label="uploads",
                path="/tmp",
                total_bytes=100,
                used_bytes=50,
                free_bytes=50,
                used_pct=50.0,
                sampled_at=old,
            ),
            DiskSample(
                mount_label="uploads",
                path="/tmp",
                total_bytes=100,
                used_bytes=60,
                free_bytes=40,
                used_pct=60.0,
                sampled_at=recent,
            ),
        ]
    )
    db_session.commit()

    assert cleanup_success_job_runs(retention_days=14) == 1
    assert cleanup_durable_jobs(retention_days=30, failed_retention_days=180) == {
        "finished_deleted": 1,
        "failed_deleted": 1,
    }
    assert cleanup_user_access_logs(retention_days=90) == 1
    assert cleanup_disk_samples(retention_days=90) == 1

    db_session.expire_all()
    assert JobRun.query.filter_by(status="success").count() == 1
    assert JobRun.query.filter_by(status="failed").count() == 1
    assert UserAccessLog.query.count() == 1
    assert DiskSample.query.count() == 1
    assert DurableJob.query.count() == 1
    assert DurableJob.query.first().status == "queued"


def test_run_housekeeping_reports_operational_retention_sections(app, db_session, monkeypatch):
    import app.services.ops.housekeeping as housekeeping

    monkeypatch.setattr(housekeeping, "cleanup_upload_sessions", lambda: 0)
    monkeypatch.setattr(housekeeping, "cleanup_error_reports", lambda *, retention_days: 0)
    monkeypatch.setattr(housekeeping, "cleanup_job_runs", lambda *, retention_days: 3)
    monkeypatch.setattr(housekeeping, "cleanup_success_job_runs", lambda *, retention_days: 7)
    monkeypatch.setattr(housekeeping, "cleanup_stale_job_runs", lambda *, stale_minutes: 0)
    monkeypatch.setattr(
        housekeeping,
        "cleanup_stale_worker_heartbeats",
        lambda *, retention_minutes: {"scanned": 2, "deleted": 1},
    )
    monkeypatch.setattr(
        housekeeping,
        "cleanup_durable_jobs",
        lambda *, retention_days, failed_retention_days: {
            "finished_deleted": 13,
            "failed_deleted": 3,
        },
    )
    monkeypatch.setattr(
        housekeeping,
        "cleanup_durable_jobs_by_task",
        lambda *, retention_days_by_task: {"calendar.sync": 2},
    )
    monkeypatch.setattr(housekeeping, "cleanup_user_access_logs", lambda *, retention_days: 5)
    monkeypatch.setattr(housekeeping, "cleanup_disk_samples", lambda *, retention_days: 4)
    monkeypatch.setattr(
        housekeeping,
        "cleanup_orphan_file_assets",
        lambda *, min_age_days, limit: {"scanned": 0, "deleted": 0, "bytes_freed": 0},
    )
    monkeypatch.setattr(
        housekeeping,
        "cleanup_file_delete_queue",
        lambda *, limit: {"picked": 0, "deleted": 0, "retried": 0, "failed": 0},
    )
    monkeypatch.setattr(
        housekeeping,
        "cleanup_staging_file_assets",
        lambda *, retention_hours: {"scanned": 0, "deleted": 0, "bytes_freed": 0},
    )
    monkeypatch.setattr(
        housekeeping,
        "cleanup_pdf_text_cache",
        lambda *, cache_dir, retention_days: {"scanned": 0, "deleted": 0, "bytes_freed": 0},
    )
    monkeypatch.setattr(housekeeping, "cleanup_old_backups", lambda: True)

    result = housekeeping.run_housekeeping()

    assert result["job_runs_deleted"] == 3
    assert result["job_runs_success_deleted"] == 7
    assert result["worker_heartbeats_gc"] == {"scanned": 2, "deleted": 1}
    assert result["durable_jobs_deleted"]["finished_deleted"] == 13
    assert result["durable_jobs_deleted"]["failed_deleted"] == 3
    assert result["durable_jobs_task_deleted"] == {"calendar.sync": 2}
    assert result["user_access_logs_deleted"] == 5
    assert result["disk_samples_deleted"] == 4
    assert "high_volume_table_snapshot" in result


def test_scheduled_housekeeping_logs_summary(app, monkeypatch):
    from app.services.ops import scheduler as scheduler_mod
    from app.services.ops import housekeeping

    infos = []

    monkeypatch.setattr(housekeeping, "run_housekeeping", lambda: {"job_runs_deleted": 3})

    with app.app_context():
        monkeypatch.setattr(app.logger, "info", lambda *args, **kwargs: infos.append(args))
        result = scheduler_mod._run_housekeeping_job(app)

    assert result == {"job_runs_deleted": 3}
    assert infos == [("Scheduled housekeeping completed: %s", {"job_runs_deleted": 3})]
