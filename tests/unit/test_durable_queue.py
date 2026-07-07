import json
import threading
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.ops.durable_queue import (
    DurableJobClaim,
    DurableQueue,
    QueueSettings,
    durable_job_retry_diagnostics,
)
from app.ops.models import DurableJob
from app.utils.policy_sql import policy_text as text


def test_claim_one_respects_queue_priority_over_run_at(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        now = datetime.utcnow()
        old_calendar = queue.enqueue(
            task="calendar.sync",
            payload={},
            queue="calendar",
            run_at=now - timedelta(minutes=10),
            session=db_session,
            commit=False,
        )
        deferred = queue.enqueue(
            task="deferred.sync",
            payload={},
            queue="deferred",
            run_at=now,
            session=db_session,
            commit=False,
        )
        db_session.commit()

        claimed = queue.claim_one(queues=["deferred", "calendar"])

        assert claimed is not None
        assert claimed.id == deferred.id
        assert old_calendar.status == "queued"


def test_enqueue_returns_job_with_loaded_id_after_owned_session_close(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        job = queue.enqueue(
            task="mail.foreign_automation",
            payload={"email_id": "email-1"},
            queue="email",
        )

        assert isinstance(job.id, int)
        assert job.task == "mail.foreign_automation"
        assert job.payload == {"email_id": "email-1"}


def test_enqueue_dedupe_key_returns_existing_active_job(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        first = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 10},
            queue="calendar",
            dedupe_key="calendar:deadline:10:sync",
            payload_version=2,
            source_event_id="deadline:10",
            idempotency_scope="sync",
            session=db_session,
            commit=False,
        )
        second = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 10, "changed": True},
            queue="calendar",
            dedupe_key="calendar:deadline:10:sync",
            session=db_session,
            commit=False,
        )
        db_session.commit()

        assert second.id == first.id
        db.session.remove()
        jobs = DurableJob.query.order_by(DurableJob.id.asc()).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"entity_id": 10}
        assert jobs[0].payload_version == 2
        assert jobs[0].source_event_id == "deadline:10"
        assert jobs[0].idempotency_scope == "sync"


def test_enqueue_dedupe_key_allows_new_job_after_failed(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        first = queue.enqueue(
            task="mail.foreign_automation",
            payload={"email_id": "email-1"},
            queue="email",
            dedupe_key="mail:email-1",
            session=db_session,
            commit=False,
        )
        db_session.flush()
        first.status = "failed"
        db_session.commit()

        second = queue.enqueue(
            task="mail.foreign_automation",
            payload={"email_id": "email-1"},
            queue="email",
            dedupe_key="mail:email-1",
            session=db_session,
            commit=False,
        )
        db_session.commit()

        assert second.id != first.id
        assert DurableJob.query.count() == 2


def test_enqueue_dedupe_key_allows_new_job_after_succeeded(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        first = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 11},
            queue="calendar",
            dedupe_key="calendar:deadline:11:sync",
            session=db_session,
            commit=False,
        )
        db_session.flush()
        first.status = "succeeded"
        db_session.commit()

        second = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 11, "repeat": True},
            queue="calendar",
            dedupe_key="calendar:deadline:11:sync",
            session=db_session,
            commit=False,
        )
        db_session.commit()

        assert second.id != first.id
        assert DurableJob.query.count() == 2


def test_enqueue_after_commit_runs_only_after_successful_commit(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        queue.enqueue_after_commit(
            task="deferred.sync",
            payload={"matter_ids": ["M1"]},
            queue="deferred",
            dedupe_key="deferred:M1",
            session=db_session,
        )
        assert DurableJob.query.count() == 0

        db_session.commit()

        job = DurableJob.query.one()
        assert job.task == "deferred.sync"
        assert job.dedupe_key == "deferred:M1"


def test_enqueue_after_commit_discards_on_rollback(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        queue.enqueue_after_commit(
            task="deferred.sync",
            payload={"matter_ids": ["M2"]},
            queue="deferred",
            dedupe_key="deferred:M2",
            session=db_session,
        )
        db_session.rollback()

        assert DurableJob.query.count() == 0


def test_claim_one_returns_detached_claim_snapshot(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        job = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 10, "nested": {"value": 1}},
            queue="calendar",
            session=db_session,
            commit=False,
        )
        db_session.commit()
        job_id = job.id

        claimed = queue.claim_one(queues=["calendar"])

        assert isinstance(claimed, DurableJobClaim)
        assert not isinstance(claimed, DurableJob)
        assert claimed.id == job_id
        assert claimed.task == "calendar.sync"
        assert claimed.payload == {"entity_id": 10, "nested": {"value": 1}}

        claimed.payload["nested"]["value"] = 99
        db.session.remove()
        persisted = db.session.get(DurableJob, job_id)
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.payload == {"entity_id": 10, "nested": {"value": 1}}


def test_worker_heartbeat_records_system_config(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))

        queue._record_worker_heartbeat(queues=["deferred", "calendar"])

        raw_value = db.session.execute(
            text("SELECT value FROM system_config WHERE key = :key"),
            {"key": queue._worker_heartbeat_key()},
        ).scalar()
        assert raw_value
        payload = json.loads(raw_value)
        assert payload["service"] == "worker"
        assert payload["worker_id"] == queue.worker_id
        assert payload["queues"] == ["deferred", "calendar"]
        assert payload["updated_at"]


def test_worker_loop_releases_claim_session_before_handler(app, db_session, monkeypatch):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        job = queue.enqueue(
            task="calendar.sync",
            payload={"entity_id": 42},
            queue="calendar",
            max_attempts=1,
            session=db_session,
            commit=False,
        )
        db_session.commit()
        job_id = job.id

        db.session.remove()
        claim_session = db.session()
        original_expire_on_commit = claim_session.expire_on_commit
        claim_session.expire_on_commit = True

        class StopLoop(Exception):
            pass

        seen = {}

        def _handler(payload):
            seen["has_transaction"] = db.session().get_transaction() is not None
            seen["payload"] = dict(payload)

        def _stop_sleep(seconds):  # noqa: ARG001
            raise StopLoop()

        monkeypatch.setattr("app.ops.durable_queue.time.sleep", _stop_sleep)

        try:
            with pytest.raises(StopLoop):
                queue.worker_loop(
                    {"calendar.sync": _handler},
                    queues=["calendar"],
                    poll_seconds=0,
                )
        finally:
            claim_session.expire_on_commit = original_expire_on_commit

        assert seen == {"has_transaction": False, "payload": {"entity_id": 42}}
        db.session.remove()
        succeeded = db.session.get(DurableJob, job_id)
        assert succeeded is not None
        assert succeeded.status == "succeeded"
        assert succeeded.locked_at is None
        assert succeeded.locked_by is None


def test_worker_loop_respects_stop_event(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        stop_event = threading.Event()
        stop_event.set()

        queue.worker_loop({}, queues=["default"], poll_seconds=0, stop_event=stop_event)


def test_worker_loop_marks_failed_after_handler_db_error(app, db_session, monkeypatch):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        job = queue.enqueue(
            task="boom",
            payload={"id": 1},
            queue="default",
            max_attempts=1,
            session=db_session,
            commit=False,
        )
        db_session.commit()
        job_id = job.id

        class StopLoop(Exception):
            pass

        def _handler(payload):  # noqa: ARG001
            db.session.execute(text("select * from durable_queue_missing_table"))

        def _stop_sleep(seconds):  # noqa: ARG001
            raise StopLoop()

        monkeypatch.setattr("app.ops.durable_queue.time.sleep", _stop_sleep)

        with pytest.raises(StopLoop):
            queue.worker_loop({"boom": _handler}, queues=["default"], poll_seconds=0)

        db.session.remove()
        failed = db.session.get(DurableJob, job_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.locked_at is None
        assert failed.locked_by is None
        assert "durable_queue_missing_table" in (failed.last_error or "")


def test_mark_failed_exposes_retry_diagnostics(app, db_session):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        job = queue.enqueue(
            task="retry.visible",
            payload={},
            queue="default",
            max_attempts=3,
            session=db_session,
            commit=False,
        )
        db_session.commit()
        job_id = job.id

        claim = queue.claim_one(queues=["default"])
        assert claim is not None

        queue.mark_failed(claim, ValueError("bad payload"))

        db.session.remove()
        retried = db.session.get(DurableJob, job_id)
        assert retried is not None
        assert retried.status == "queued"
        assert retried.attempts == 1
        assert retried.last_error == "ValueError: bad payload"

        diagnostics = durable_job_retry_diagnostics(retried, now=datetime.utcnow())
        assert diagnostics["retry_state"] in {"retry_waiting", "retry_ready"}
        assert diagnostics["retry_state_label"]
        assert diagnostics["retry_cause"] == "ValueError: bad payload"
        assert diagnostics["next_retry_at"] == retried.run_at
        assert diagnostics["retries_remaining"] == 2


def test_worker_loop_retries_mark_succeeded_after_session_error(app, db_session, monkeypatch):
    with app.app_context():
        queue = DurableQueue(QueueSettings(poll_seconds=0.01, lock_ttl_seconds=60))
        job = queue.enqueue(
            task="ok",
            payload={},
            queue="default",
            max_attempts=1,
            session=db_session,
            commit=False,
        )
        db_session.commit()
        job_id = job.id

        class StopLoop(Exception):
            pass

        real_mark_succeeded = queue.mark_succeeded
        attempts = {"count": 0}

        def _flaky_mark_succeeded(job_or_id):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("session needs reset")
            real_mark_succeeded(job_or_id)

        def _stop_sleep(seconds):  # noqa: ARG001
            raise StopLoop()

        monkeypatch.setattr(queue, "mark_succeeded", _flaky_mark_succeeded)
        monkeypatch.setattr("app.ops.durable_queue.time.sleep", _stop_sleep)

        with pytest.raises(StopLoop):
            queue.worker_loop({"ok": lambda payload: None}, queues=["default"], poll_seconds=0)

        db.session.remove()
        succeeded = db.session.get(DurableJob, job_id)
        assert attempts["count"] == 2
        assert succeeded is not None
        assert succeeded.status == "succeeded"
        assert succeeded.locked_at is None
        assert succeeded.locked_by is None
