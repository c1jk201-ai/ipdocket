from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.models.job_run import JobRun
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.ops.models import DurableJob


def test_durable_queue_lag_ignores_finished_history(app, db_session):
    from app.services.ops.operational_metrics import _durable_queue_metrics

    with app.app_context():
        now = datetime.utcnow().replace(microsecond=0)
        old = now - timedelta(days=30)
        db_session.add_all(
            [
                DurableJob(
                    queue="default",
                    task="completed.task",
                    payload={},
                    status="succeeded",
                    attempts=1,
                    max_attempts=1,
                    run_at=old,
                    created_at=old,
                    updated_at=old,
                    finished_at=old + timedelta(seconds=5),
                ),
                DurableJob(
                    queue="default",
                    task="cancelled.task",
                    payload={},
                    status="cancelled",
                    attempts=0,
                    max_attempts=1,
                    run_at=old,
                    created_at=old,
                    updated_at=old,
                    finished_at=old + timedelta(seconds=5),
                ),
            ]
        )
        db_session.commit()

        metrics = _durable_queue_metrics(now=now)

        assert metrics["totals"]["queued"] == 0
        assert metrics["totals"]["running"] == 0
        assert metrics["totals"]["failed"] == 0
        assert metrics["totals"]["max_queue_lag_seconds"] == 0
        assert metrics["totals"]["oldest_queued_age_seconds"] == 0
        assert metrics["totals"]["oldest_active_age_seconds"] == 0
        assert all(item["queue_lag_seconds"] == 0 for item in metrics["by_queue_task"])


def test_durable_queue_lag_counts_queued_backlog(app, db_session):
    from app.services.ops.operational_metrics import _durable_queue_metrics

    with app.app_context():
        now = datetime.utcnow().replace(microsecond=0)
        old = now - timedelta(minutes=15)
        db_session.add(
            DurableJob(
                queue="default",
                task="queued.task",
                payload={},
                status="queued",
                attempts=0,
                max_attempts=1,
                run_at=old,
                created_at=old,
                updated_at=old,
            )
        )
        db_session.commit()

        metrics = _durable_queue_metrics(now=now)

        assert metrics["totals"]["queued"] == 1
        assert metrics["totals"]["max_queue_lag_seconds"] >= 900
        assert metrics["totals"]["oldest_queued_age_seconds"] >= 900
        assert metrics["totals"]["oldest_active_age_seconds"] >= 900


def test_worker_heartbeat_metrics_show_only_fresh_workers(app, db_session):
    from app.models.system_config import SystemConfig
    from app.services.ops.operational_metrics import _heartbeat_metrics

    with app.app_context():
        app.config["READY_WORKER_HEARTBEAT_MAX_AGE_SECONDS"] = 300
        now = datetime.utcnow().replace(microsecond=0)
        db_session.add_all(
            [
                SystemConfig(
                    key="ops.worker_heartbeat.current",
                    value=json.dumps(
                        {
                            "worker_id": "current:1",
                            "queues": ["deferred"],
                            "updated_at": (now - timedelta(seconds=60)).isoformat(),
                        }
                    ),
                ),
                SystemConfig(
                    key="ops.worker_heartbeat.old",
                    value=json.dumps(
                        {
                            "worker_id": "old:1",
                            "queues": ["deferred"],
                            "updated_at": (now - timedelta(hours=2)).isoformat(),
                        }
                    ),
                ),
                SystemConfig(key="ops.worker_heartbeat.malformed", value="{"),
            ]
        )
        db_session.commit()

        metrics = _heartbeat_metrics(now=now)

        assert metrics["worker_stale_after_seconds"] == 300
        assert metrics["newest_worker_age_seconds"] == 60
        assert metrics["stale_worker_count"] == 2
        assert metrics["malformed_worker_count"] == 1
        assert [worker["key"] for worker in metrics["workers"]] == ["ops.worker_heartbeat.current"]


def test_high_volume_table_metrics_emit_growth_and_retention_alerts(app, db_session):
    from app.services.ops.operational_metrics import collect_high_volume_table_metrics

    with app.app_context():
        app.config["OPERATIONAL_TABLE_JOB_RUNS_WARN_ROWS"] = 1
        app.config["OPERATIONAL_TABLE_JOB_RUNS_WARN_ROWS_24H"] = 1
        app.config["OPERATIONAL_TABLE_EXPIRED_WARN_ROWS"] = 1
        app.config["JOB_RUN_SUCCESS_RETENTION_DAYS"] = 14

        now = datetime.utcnow().replace(microsecond=0)
        old = now - timedelta(days=30)
        user = User(username="ops-metrics-user", email="ops-metrics@example.com", role="admin")
        db_session.add(user)
        db_session.flush()
        db_session.add_all(
            [
                JobRun(
                    job_name="old-success",
                    run_id="old-success",
                    status="success",
                    started_at=old,
                    finished_at=old,
                ),
                JobRun(
                    job_name="recent-success",
                    run_id="recent-success",
                    status="success",
                    started_at=now,
                    finished_at=now,
                ),
                UserAccessLog(
                    user_id=user.id,
                    method="GET",
                    path="/ops",
                    status_code=200,
                    created_at=now,
                ),
                DurableJob(queue="ops", task="noop", payload={}, status="queued", run_at=now),
            ]
        )
        db_session.commit()

        metrics = collect_high_volume_table_metrics(now=now)

        job_runs = metrics["by_table"]["job_runs"]
        assert job_runs["row_count"] >= 2
        assert job_runs["rows_24h"] >= 1
        assert job_runs["expired_rows"] >= 1
        assert {alert["code"] for alert in metrics["alerts"]} >= {
            "job_runs_row_count_high",
            "job_runs_rows_24h_high",
            "job_runs_retention_expired_rows",
        }
