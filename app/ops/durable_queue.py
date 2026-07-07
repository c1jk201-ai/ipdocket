from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import traceback
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional, cast

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.ops.models import DurableJob
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

Handler = Callable[[Dict[str, Any]], None]
logger = logging.getLogger(__name__)
_AFTER_COMMIT_ENQUEUE_KEY = "_durable_queue_after_commit_specs"
_ACTIVE_DEDUPE_STATUSES = ("queued", "running")


@dataclass(frozen=True)
class QueueSettings:
    poll_seconds: float = 2.0
    lock_ttl_seconds: int = 600
    max_backoff_seconds: int = 3600
    worker_heartbeat_interval_seconds: int = 30


@dataclass(frozen=True)
class DurableJobClaim:
    id: int
    queue: str
    task: str
    payload: Dict[str, Any]
    attempts: int
    max_attempts: int


@dataclass(frozen=True)
class DurableEnqueueSpec:
    task: str
    payload: Dict[str, Any]
    queue: str
    run_at: datetime | None
    max_attempts: int
    dedupe_key: str | None
    payload_version: int
    source_event_id: str | None
    provider_request_id: str | None
    idempotency_scope: str | None
    settings: QueueSettings


def _utcnow() -> datetime:
    return datetime.utcnow()


def _backoff_seconds(attempts: int, max_backoff_seconds: int) -> int:
    # 1, 2, 4, 8, ... (cap)
    sec = 2 ** max(0, attempts - 1)
    backoff = sec * 60  # minute-based backoff
    if backoff > max_backoff_seconds:
        return int(max_backoff_seconds)
    return int(backoff)


def _exception_summary(err: BaseException) -> str:
    err_type = type(err).__name__
    message = str(err or "").strip()
    if message:
        return f"{err_type}: {message}"
    return err_type


def _visible_retry_cause(last_error: Any) -> str:
    raw = str(last_error or "").strip()
    if not raw:
        return ""

    marker_labels: dict[str, str] = {
        "[manual retry]": "manual retry",
        "[recovered stale lock]": "stale lock recovered",
        "[recovered worker startup lock]": "worker startup lock recovered",
    }
    seen_markers: list[str] = []
    for line in raw.replace("\r", "\n").split("\n"):
        text_value = line.strip()
        if not text_value:
            continue
        marker = marker_labels.get(text_value)
        if marker:
            seen_markers.append(marker)
            continue
        if len(text_value) > 300:
            return text_value[:299].rstrip() + "..."
        return text_value
    return ", ".join(seen_markers)


def durable_job_retry_diagnostics(job: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Return display-safe retry diagnostics for a DurableJob-like object."""
    current = now or _utcnow()
    status = str(getattr(job, "status", "") or "").strip().lower()
    attempts = 0
    max_attempts = 0
    try:
        attempts = int(getattr(job, "attempts", 0) or 0)
    except Exception:
        attempts = 0
    try:
        max_attempts = int(getattr(job, "max_attempts", 0) or 0)
    except Exception:
        max_attempts = 0

    run_at = getattr(job, "run_at", None)
    retry_cause = _visible_retry_cause(getattr(job, "last_error", None))
    next_retry_at = run_at if status == "queued" and attempts > 0 else None
    retry_due_in_seconds = None
    if next_retry_at is not None:
        try:
            retry_due_in_seconds = int((next_retry_at - current).total_seconds())
        except Exception:
            retry_due_in_seconds = None

    retry_state = ""
    if status == "queued" and attempts > 0:
        if retry_due_in_seconds is not None and retry_due_in_seconds > 0:
            retry_state = "retry_waiting"
        else:
            retry_state = "retry_ready"
    elif status == "running" and attempts > 1:
        retry_state = "retry_running"
    elif status == "failed":
        if max_attempts and attempts >= max_attempts:
            retry_state = "retry_exhausted"
        else:
            retry_state = "failed"

    labels = {
        "retry_waiting": "Retry Waiting",
        "retry_ready": "Retry ",
        "retry_running": "Retry row In Progress",
        "retry_exhausted": "Retry ",
        "failed": "Failed",
    }
    if not retry_cause and retry_state:
        retry_cause = "previous attempt failed"

    return {
        "retry_state": retry_state,
        "retry_state_label": labels.get(retry_state, ""),
        "retry_cause": retry_cause,
        "next_retry_at": next_retry_at,
        "retry_due_in_seconds": retry_due_in_seconds,
        "retries_remaining": max(0, max_attempts - attempts) if max_attempts else None,
    }


def _clean_optional(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value or None


def _find_existing_deduped_job(
    session: SASession,
    *,
    queue: str,
    task: str,
    dedupe_key: str | None,
) -> DurableJob | None:
    if not dedupe_key:
        return None
    return cast(
        DurableJob | None,
        session.query(DurableJob)
        .filter(
            DurableJob.queue == str(queue),
            DurableJob.task == str(task),
            DurableJob.dedupe_key == str(dedupe_key),
            DurableJob.status.in_(_ACTIVE_DEDUPE_STATUSES),
        )
        .order_by(DurableJob.id.asc())
        .first(),
    )


class DurableQueue:
    """
    DB-backed Durable Queue.

    - Producer: enqueue()
    - Consumer: worker_loop()
    - Concurrency: SELECT ... FOR UPDATE SKIP LOCKED (Postgres)
    - Resilience: stale lock recovery
    """

    def __init__(self, settings: QueueSettings):
        self.settings = settings
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def enqueue(
        self,
        task: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        queue: str = "default",
        run_at: Optional[datetime] = None,
        max_attempts: int = 5,
        dedupe_key: str | None = None,
        payload_version: int = 1,
        source_event_id: str | None = None,
        provider_request_id: str | None = None,
        idempotency_scope: str | None = None,
        session: SASession | None = None,
        commit: bool | None = None,
    ) -> DurableJob:
        """
        Enqueue a durable job.

        Safety:
        - If `session` is provided, this function will NOT commit by default (commit=False),
          to avoid "hidden commit" that can accidentally commit unrelated pending changes.
        - If `session` is not provided, a dedicated SQLAlchemy Session is used and committed
          by default (commit=True) so callers can enqueue safely from request code without
          committing their ambient Flask-SQLAlchemy scoped session.
        """
        owns_session = False
        sess: SASession
        if session is None:
            sess = SASession(db.engine)
            owns_session = True
            if commit is None:
                commit = True
        else:
            sess = session
            if commit is None:
                commit = False

        clean_dedupe_key = _clean_optional(dedupe_key)
        clean_source_event_id = _clean_optional(source_event_id)
        clean_provider_request_id = _clean_optional(provider_request_id)
        clean_idempotency_scope = _clean_optional(idempotency_scope)
        try:
            clean_payload_version = max(1, int(payload_version or 1))
        except Exception:
            clean_payload_version = 1

        existing = _find_existing_deduped_job(
            sess,
            queue=queue,
            task=task,
            dedupe_key=clean_dedupe_key,
        )
        if existing is not None:
            if owns_session:
                try:
                    sess.close()
                except Exception as close_exc:
                    report_swallowed_exception(
                        close_exc,
                        context="durable_queue.enqueue.close_existing",
                        log_key="durable_queue.enqueue.close_existing",
                        log_window_seconds=300,
                    )
            return existing

        job = DurableJob(
            queue=queue,
            task=task,
            payload=payload or {},
            payload_version=clean_payload_version,
            dedupe_key=clean_dedupe_key,
            source_event_id=clean_source_event_id,
            provider_request_id=clean_provider_request_id,
            idempotency_scope=clean_idempotency_scope,
            status="queued",
            attempts=0,
            max_attempts=max_attempts,
            run_at=run_at or _utcnow(),
        )
        try:
            sess.add(job)
            if commit:
                sess.commit()
                if owns_session:
                    # The dedicated producer session is closed below. Load the row once so
                    # callers can still read basic job attributes (especially id) afterwards.
                    sess.refresh(job)
            else:
                sess.flush()
            return job
        except IntegrityError:
            if not clean_dedupe_key:
                try:
                    sess.rollback()
                except Exception as rollback_exc:
                    report_swallowed_exception(
                        rollback_exc,
                        context="durable_queue.enqueue.integrity.rollback",
                        log_key="durable_queue.enqueue.integrity.rollback",
                        log_window_seconds=300,
                    )
                raise
            try:
                sess.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="durable_queue.enqueue.dedupe.rollback",
                    log_key="durable_queue.enqueue.dedupe.rollback",
                    log_window_seconds=300,
                )
            existing = _find_existing_deduped_job(
                sess,
                queue=queue,
                task=task,
                dedupe_key=clean_dedupe_key,
            )
            if existing is not None:
                return existing
            raise
        except Exception:
            try:
                sess.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="durable_queue.enqueue.rollback",
                    log_key="durable_queue.enqueue.rollback",
                    log_window_seconds=300,
                )
            raise
        finally:
            if owns_session:
                try:
                    sess.close()
                except Exception as close_exc:
                    report_swallowed_exception(
                        close_exc,
                        context="durable_queue.enqueue.close",
                        log_key="durable_queue.enqueue.close",
                        log_window_seconds=300,
                    )

    def enqueue_after_commit(
        self,
        task: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        queue: str = "default",
        run_at: Optional[datetime] = None,
        max_attempts: int = 5,
        dedupe_key: str | None = None,
        payload_version: int = 1,
        source_event_id: str | None = None,
        provider_request_id: str | None = None,
        idempotency_scope: str | None = None,
        session: SASession | None = None,
    ) -> None:
        """Stage a durable enqueue to run only after the surrounding commit succeeds."""
        sess = session or db.session
        try:
            clean_payload_version = max(1, int(payload_version or 1))
        except Exception:
            clean_payload_version = 1
        spec = DurableEnqueueSpec(
            task=str(task),
            payload=deepcopy(payload or {}),
            queue=str(queue or "default"),
            run_at=run_at,
            max_attempts=int(max_attempts or 5),
            dedupe_key=_clean_optional(dedupe_key),
            payload_version=clean_payload_version,
            source_event_id=_clean_optional(source_event_id),
            provider_request_id=_clean_optional(provider_request_id),
            idempotency_scope=_clean_optional(idempotency_scope),
            settings=self.settings,
        )
        pending = sess.info.get(_AFTER_COMMIT_ENQUEUE_KEY)
        if pending is None:
            pending = []
            sess.info[_AFTER_COMMIT_ENQUEUE_KEY] = pending
        pending.append(spec)

    def _recover_stale_locks(self) -> int:
        """Re-queue jobs whose locks have exceeded the TTL.

        C-5 fix: Uses an independent SQLAlchemy session so this method is
        safe to call even after the Flask scoped session has been reset or
        removed by the worker loop.
        """
        from sqlalchemy import text as sa_text
        from sqlalchemy.orm import sessionmaker

        ttl = timedelta(seconds=self.settings.lock_ttl_seconds)
        cutoff = _utcnow() - ttl
        count = 0
        try:
            # Build a short-lived independent session that is isolated from
            # the Flask request-scoped session managed by flask-sqlalchemy.
            engine = db.engine
            IndepSession = sessionmaker(bind=engine)
            with IndepSession() as sess:
                rows = sess.execute(
                    sa_text(
                        """
                        UPDATE durable_jobs
                           SET status    = 'queued',
                               locked_at = NULL,
                               locked_by = NULL,
                               run_at    = NOW() AT TIME ZONE 'UTC',
                               last_error = COALESCE(last_error, '') || E'\\n[recovered stale lock]'
                         WHERE status = 'running'
                           AND locked_at IS NOT NULL
                           AND locked_at < :cutoff
                        """
                    ),
                    {"cutoff": cutoff},
                )
                count = rows.rowcount
                sess.commit()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue._recover_stale_locks",
                log_key="durable_queue._recover_stale_locks",
                log_window_seconds=300,
            )
        return count

    def _recover_startup_locks_for_worker(self, *, queues: Iterable[str]) -> int:
        """Re-queue jobs left running by a previous worker process in this container."""
        from sqlalchemy.orm import sessionmaker

        queue_names = [str(q) for q in queues if q]
        if not queue_names:
            return 0
        locked_by_prefix = f"{self._worker_hostname()}:%"
        count = 0
        try:
            IndepSession = sessionmaker(bind=db.engine)
            with IndepSession() as sess:
                jobs = (
                    sess.query(DurableJob)
                    .filter(
                        DurableJob.status == "running",
                        DurableJob.queue.in_(queue_names),
                        DurableJob.locked_by.like(locked_by_prefix),
                    )
                    .all()
                )
                for job in jobs:
                    job.status = "queued"
                    job.locked_at = None
                    job.locked_by = None
                    job.run_at = _utcnow()
                    job.last_error = (job.last_error or "") + "\n[recovered worker startup lock]"
                count = len(jobs)
                sess.commit()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue._recover_startup_locks_for_worker",
                log_key="durable_queue._recover_startup_locks_for_worker",
                log_window_seconds=300,
            )
        return int(count or 0)

    def _snapshot_claimed_job(self, job: DurableJob) -> DurableJobClaim:
        raw_payload = job.payload if isinstance(job.payload, dict) else {}
        return DurableJobClaim(
            id=int(job.id),
            queue=str(job.queue or ""),
            task=str(job.task or ""),
            payload=deepcopy(raw_payload),
            attempts=int(job.attempts or 0),
            max_attempts=int(job.max_attempts or 0),
        )

    def claim_one(self, *, queues: Iterable[str]) -> Optional[DurableJobClaim]:
        now = _utcnow()
        queue_names = [q for q in queues if q]

        # Periodically recover stale locks
        self._recover_stale_locks()

        job: DurableJob | None = None
        for queue_name in queue_names:
            # SKIP LOCKED is effective on Postgres. Iterate queue names in the
            # caller-provided order so operational queues can outrank slow
            # external integrations even when integration jobs are older.
            base = DurableJob.query.filter(
                DurableJob.status == "queued",
                DurableJob.queue == queue_name,
                DurableJob.run_at <= now,
            ).order_by(DurableJob.run_at.asc(), DurableJob.id.asc())

            try:
                job = cast(DurableJob | None, base.with_for_update(skip_locked=True).first())
            except TypeError:
                # Fallback for SQLite etc.
                job = cast(DurableJob | None, base.with_for_update().first())

            if job:
                break

        if not job:
            db.session.rollback()
            return None

        job.status = "running"
        job.locked_at = now
        job.locked_by = self.worker_id
        job.attempts = (job.attempts or 0) + 1
        claimed = self._snapshot_claimed_job(job)
        db.session.commit()
        return claimed

    def _resolve_job_id(self, job_or_id: DurableJob | DurableJobClaim | int | None) -> int | None:
        if isinstance(job_or_id, int):
            return job_or_id
        if isinstance(job_or_id, DurableJobClaim):
            return int(job_or_id.id)
        if not isinstance(job_or_id, DurableJob):
            return None
        try:
            job_id = getattr(job_or_id, "id", None)
            if job_id is not None:
                return int(job_id)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue._resolve_job_id.from_attr",
                log_key="durable_queue._resolve_job_id.from_attr",
                log_window_seconds=300,
            )
        # Detached instances may still carry identity in SQLAlchemy state.
        try:
            state = getattr(job_or_id, "_sa_instance_state", None)
            ident = getattr(state, "identity", None) if state is not None else None
            if ident and ident[0] is not None:
                return int(ident[0])
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue._resolve_job_id.from_state_identity",
                log_key="durable_queue._resolve_job_id.from_state_identity",
                log_window_seconds=300,
            )
        return None

    def _reset_scoped_session(self, context: str) -> None:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context=f"{context}.rollback",
                log_key=f"{context}.rollback",
                log_window_seconds=300,
            )
        try:
            db.session.remove()
        except Exception as remove_exc:
            report_swallowed_exception(
                remove_exc,
                context=f"{context}.remove",
                log_key=f"{context}.remove",
                log_window_seconds=300,
            )

    def _worker_hostname(self) -> str:
        return self.worker_id.rsplit(":", 1)[0] if ":" in self.worker_id else socket.gethostname()

    def _worker_heartbeat_key(self) -> str:
        return f"ops.worker_heartbeat.{self._worker_hostname()}"

    def _record_worker_heartbeat(self, *, queues: Iterable[str]) -> None:
        now = _utcnow()
        queue_names = [str(q) for q in queues if q]
        payload = json.dumps(
            {
                "ok": True,
                "service": "worker",
                "worker_id": self.worker_id,
                "hostname": self._worker_hostname(),
                "pid": os.getpid(),
                "queues": queue_names,
                "updated_at": now.isoformat(),
            },
            sort_keys=True,
        )
        params = {"key": self._worker_heartbeat_key(), "value": payload}
        try:
            dialect = getattr(db.engine.dialect, "name", "")
            with db.engine.begin() as conn:
                if dialect in {"postgresql", "sqlite"}:
                    conn.execute(
                        text(
                            """
                            INSERT INTO system_config (key, value)
                            VALUES (:key, :value)
                            ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value
                            """
                        ),
                        params,
                    )
                    return

                updated = conn.execute(
                    text("UPDATE system_config SET value = :value WHERE key = :key"),
                    params,
                )
                if not int(updated.rowcount or 0):
                    conn.execute(
                        text("INSERT INTO system_config (key, value) VALUES (:key, :value)"),
                        params,
                    )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue.worker_heartbeat",
                log_key="durable_queue.worker_heartbeat",
                log_window_seconds=300,
            )

    def _refresh_job_lock(self, job_id: int) -> bool:
        """Extend the current worker's lease on a running job."""
        from sqlalchemy.orm import sessionmaker

        if not job_id:
            return False
        try:
            IndepSession = sessionmaker(bind=db.engine)
            with IndepSession() as sess:
                rows = (
                    sess.query(DurableJob)
                    .filter(
                        DurableJob.id == job_id,
                        DurableJob.status == "running",
                        DurableJob.locked_by == self.worker_id,
                    )
                    .update(
                        {DurableJob.locked_at: _utcnow()},
                        synchronize_session=False,
                    )
                )
                sess.commit()
                return bool(rows)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue.job_lock_heartbeat.refresh",
                log_key="durable_queue.job_lock_heartbeat.refresh",
                log_window_seconds=300,
            )
            return False

    def _start_job_lock_heartbeat(
        self, job_id: int
    ) -> tuple[threading.Event, threading.Thread | None]:
        """Keep a claimed job from being recovered while its handler is still running."""
        try:
            from flask import current_app, has_app_context

            app = current_app._get_current_object() if has_app_context() else None
        except Exception:
            app = None

        ttl = max(1, int(self.settings.lock_ttl_seconds or 600))
        worker_interval = max(5, int(self.settings.worker_heartbeat_interval_seconds or 30))
        interval = max(5, min(worker_interval, max(5, ttl // 3)))
        stop = threading.Event()

        def _refresh_once() -> bool:
            if app is None:
                return self._refresh_job_lock(job_id)
            with app.app_context():
                return self._refresh_job_lock(job_id)

        def _run() -> None:
            while not stop.wait(interval):
                try:
                    if not _refresh_once():
                        return
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="durable_queue.job_lock_heartbeat.thread",
                        log_key="durable_queue.job_lock_heartbeat.thread",
                        log_window_seconds=300,
                    )

        try:
            thread = threading.Thread(
                target=_run,
                name=f"durable_queue_job_lock:{job_id}",
                daemon=True,
            )
            thread.start()
            return stop, thread
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue.job_lock_heartbeat.start",
                log_key="durable_queue.job_lock_heartbeat.start",
                log_window_seconds=300,
            )
            return stop, None

    def _start_worker_heartbeat(
        self, *, queues: Iterable[str]
    ) -> tuple[threading.Event, threading.Thread | None]:
        queue_names = [str(q) for q in queues if q]
        interval = max(5, int(self.settings.worker_heartbeat_interval_seconds or 30))

        try:
            from flask import current_app, has_app_context

            app = current_app._get_current_object() if has_app_context() else None
        except Exception:
            app = None

        def _beat_once() -> None:
            if app is None:
                self._record_worker_heartbeat(queues=queue_names)
                return
            with app.app_context():
                self._record_worker_heartbeat(queues=queue_names)

        try:
            _beat_once()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue.worker_heartbeat.start",
                log_key="durable_queue.worker_heartbeat.start",
                log_window_seconds=300,
            )

        stop = threading.Event()

        def _run() -> None:
            while not stop.wait(interval):
                try:
                    _beat_once()
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="durable_queue.worker_heartbeat.thread",
                        log_key="durable_queue.worker_heartbeat.thread",
                        log_window_seconds=300,
                    )

        thread = threading.Thread(
            target=_run,
            name=f"durable_queue_heartbeat:{self._worker_hostname()}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def mark_succeeded(self, job_or_id: DurableJob | DurableJobClaim | int | None) -> None:
        job_id = self._resolve_job_id(job_or_id)
        if job_id is None:
            return
        try:
            # Handler may clear the scoped session; re-load to ensure attachment.
            fresh = DurableJob.query.get(job_id)
            if not fresh:
                return
            if fresh.status != "running" or fresh.locked_by != self.worker_id:
                return
            fresh.status = "succeeded"
            fresh.finished_at = _utcnow()
            fresh.locked_at = None
            fresh.locked_by = None
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="durable_queue.mark_succeeded.rollback",
                    log_key="durable_queue.mark_succeeded.rollback",
                    log_window_seconds=300,
                )
            raise

    def mark_failed(
        self, job_or_id: DurableJob | DurableJobClaim | int | None, err: BaseException
    ) -> None:
        job_id = self._resolve_job_id(job_or_id)
        if job_id is None:
            return
        try:
            # Handler may clear the scoped session; re-load to ensure attachment.
            fresh = DurableJob.query.get(job_id)
            if not fresh:
                return
            if fresh.status != "running" or fresh.locked_by != self.worker_id:
                return
            error_summary = _exception_summary(err)
            fresh.last_error = error_summary
            fresh.last_traceback = traceback.format_exc()
            queue_name = str(fresh.queue or "")
            task_name = str(fresh.task or "")

            if fresh.attempts < fresh.max_attempts:
                backoff = _backoff_seconds(fresh.attempts, self.settings.max_backoff_seconds)
                next_run_at = _utcnow() + timedelta(seconds=backoff)
                fresh.status = "queued"
                fresh.run_at = next_run_at
                logger.warning(
                    "Durable job retry scheduled job_id=%s queue=%s task=%s attempts=%s/%s "
                    "backoff_seconds=%s next_run_at=%s retry_cause=%s",
                    fresh.id,
                    queue_name,
                    task_name,
                    fresh.attempts,
                    fresh.max_attempts,
                    backoff,
                    next_run_at.isoformat(),
                    error_summary,
                )
            else:
                fresh.status = "failed"
                fresh.finished_at = _utcnow()
                logger.error(
                    "Durable job retry exhausted job_id=%s queue=%s task=%s attempts=%s/%s "
                    "retry_cause=%s",
                    fresh.id,
                    queue_name,
                    task_name,
                    fresh.attempts,
                    fresh.max_attempts,
                    error_summary,
                )

            fresh.locked_at = None
            fresh.locked_by = None
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="durable_queue.mark_failed.rollback",
                    log_key="durable_queue.mark_failed.rollback",
                    log_window_seconds=300,
                )
            raise

    def retry(self, job_id: int) -> bool:
        job = DurableJob.query.get(job_id)
        if not job:
            return False
        job.status = "queued"
        job.run_at = _utcnow()
        job.locked_at = None
        job.locked_by = None
        # Attempts are kept (optional: reset to 0 in UI)
        job.last_error = (job.last_error or "") + "\\n[manual retry]"
        db.session.commit()
        return True

    def cancel(self, job_id: int) -> bool:
        job = DurableJob.query.get(job_id)
        if not job:
            return False
        job.status = "cancelled"
        job.finished_at = _utcnow()
        job.locked_at = None
        job.locked_by = None
        db.session.commit()
        return True

    def worker_loop(
        self,
        handlers: Dict[str, Handler],
        *,
        queues: Iterable[str],
        poll_seconds: Optional[float] = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        poll = poll_seconds if poll_seconds is not None else self.settings.poll_seconds
        queue_names = [q for q in queues if q]
        recovered_startup_locks = self._recover_startup_locks_for_worker(queues=queue_names)
        if recovered_startup_locks:
            try:
                from flask import current_app, has_app_context

                if has_app_context():
                    current_app.logger.warning(
                        "Recovered durable queue startup locks: %s",
                        recovered_startup_locks,
                    )
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="durable_queue.worker_loop.log_startup_lock_recovery",
                    log_key="durable_queue.worker_loop.log_startup_lock_recovery",
                    log_window_seconds=300,
                )
        heartbeat_stop, heartbeat_thread = self._start_worker_heartbeat(queues=queue_names)

        try:
            while not (stop_event is not None and stop_event.is_set()):
                job = self.claim_one(queues=queue_names)
                if not job:
                    if stop_event is not None:
                        stop_event.wait(poll)
                    else:
                        time.sleep(poll)
                    continue

                job_id = job.id
                task = job.task
                payload = deepcopy(job.payload or {})

                # Do not carry the queue-claim session into handlers that may perform
                # slow external I/O. Queue state is recorded later by job id.
                self._reset_scoped_session("durable_queue.worker_loop.before_handler")

                handler = handlers.get(task) if task else None
                if not handler:
                    self.mark_failed(
                        job_id or job,
                        RuntimeError(f"No handler registered for task={task}"),
                    )
                    continue

                started_at = time.monotonic()
                logger.info(
                    "Durable job started job_id=%s queue=%s task=%s attempts=%s max_attempts=%s worker_id=%s",
                    job_id,
                    job.queue,
                    task,
                    job.attempts,
                    job.max_attempts,
                    self.worker_id,
                )
                job_lock_stop, job_lock_thread = self._start_job_lock_heartbeat(job_id)
                try:
                    handler(payload)
                except BaseException as e:
                    duration_ms = (time.monotonic() - started_at) * 1000.0
                    logger.warning(
                        "Durable job failed job_id=%s queue=%s task=%s duration_ms=%.1f error_type=%s worker_id=%s",
                        job_id,
                        job.queue,
                        task,
                        duration_ms,
                        type(e).__name__,
                        self.worker_id,
                        exc_info=True,
                    )
                    # Handler failures can leave Flask-SQLAlchemy's scoped session in
                    # a failed transaction. Reset it before recording queue state so a
                    # transient domain error does not kill the worker process.
                    self._reset_scoped_session("durable_queue.worker_loop.handler_error")
                    try:
                        self.mark_failed(job_id or job, e)
                    except Exception as mark_exc:
                        report_swallowed_exception(
                            mark_exc,
                            context="durable_queue.worker_loop.mark_failed",
                            log_key="durable_queue.worker_loop.mark_failed",
                            log_window_seconds=300,
                        )
                        self._reset_scoped_session("durable_queue.worker_loop.mark_failed_error")
                        try:
                            self.mark_failed(job_id or job, e)
                        except Exception as retry_exc:
                            report_swallowed_exception(
                                retry_exc,
                                context="durable_queue.worker_loop.mark_failed_retry",
                                log_key="durable_queue.worker_loop.mark_failed_retry",
                                log_window_seconds=300,
                            )
                            self._reset_scoped_session(
                                "durable_queue.worker_loop.mark_failed_retry_error"
                            )
                            # C-4 fix: Both mark_failed attempts failed. Force the job
                            # to 'failed' directly via a raw SQL UPDATE so it does not
                            # remain locked in 'running' state indefinitely until the
                            # stale-lock recovery TTL fires.
                            if job_id is not None:
                                try:
                                    from sqlalchemy import text as sa_text

                                    db.session.execute(
                                        sa_text(
                                            "UPDATE durable_jobs SET status='failed',"
                                            " locked_at=NULL, locked_by=NULL,"
                                            " finished_at=NOW() AT TIME ZONE 'UTC'"
                                            " WHERE id=:jid AND status='running'"
                                            " AND locked_by=:worker_id"
                                        ),
                                        {"jid": job_id, "worker_id": self.worker_id},
                                    )
                                    db.session.commit()
                                except Exception as force_exc:
                                    report_swallowed_exception(
                                        force_exc,
                                        context="durable_queue.worker_loop.mark_failed_force",
                                        log_key="durable_queue.worker_loop.mark_failed_force",
                                        log_window_seconds=300,
                                    )
                                    self._reset_scoped_session(
                                        "durable_queue.worker_loop.mark_failed_force_error"
                                    )
                    continue
                finally:
                    job_lock_stop.set()
                    if job_lock_thread is not None:
                        try:
                            job_lock_thread.join(timeout=5)
                        except Exception as join_exc:
                            report_swallowed_exception(
                                join_exc,
                                context="durable_queue.job_lock_heartbeat.join",
                                log_key="durable_queue.job_lock_heartbeat.join",
                                log_window_seconds=300,
                            )

                duration_ms = (time.monotonic() - started_at) * 1000.0
                logger.info(
                    "Durable job completed job_id=%s queue=%s task=%s duration_ms=%.1f worker_id=%s",
                    job_id,
                    job.queue,
                    task,
                    duration_ms,
                    self.worker_id,
                )
                try:
                    self.mark_succeeded(job_id or job)
                except Exception as mark_exc:
                    report_swallowed_exception(
                        mark_exc,
                        context="durable_queue.worker_loop.mark_succeeded",
                        log_key="durable_queue.worker_loop.mark_succeeded",
                        log_window_seconds=300,
                    )
                    self._reset_scoped_session("durable_queue.worker_loop.mark_succeeded_error")
                    try:
                        self.mark_succeeded(job_id or job)
                    except Exception as retry_exc:
                        report_swallowed_exception(
                            retry_exc,
                            context="durable_queue.worker_loop.mark_succeeded_retry",
                            log_key="durable_queue.worker_loop.mark_succeeded_retry",
                            log_window_seconds=300,
                        )
                        self._reset_scoped_session(
                            "durable_queue.worker_loop.mark_succeeded_retry_error"
                        )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                try:
                    heartbeat_thread.join(timeout=5)
                except Exception as join_exc:
                    report_swallowed_exception(
                        join_exc,
                        context="durable_queue.worker_heartbeat.join",
                        log_key="durable_queue.worker_heartbeat.join",
                        log_window_seconds=300,
                    )


@event.listens_for(SASession, "after_commit")
def _drain_after_commit_enqueues(session: SASession) -> None:
    try:
        if session.in_nested_transaction():
            return
    except Exception:
        return

    specs = session.info.pop(_AFTER_COMMIT_ENQUEUE_KEY, None) or []
    if not specs:
        return

    try:
        from flask import current_app, has_app_context

        app = current_app._get_current_object() if has_app_context() else None
    except Exception:
        app = None

    def _enqueue_one(spec: DurableEnqueueSpec) -> None:
        queue = DurableQueue(settings=spec.settings)
        queue.enqueue(
            task=spec.task,
            payload=deepcopy(spec.payload),
            queue=spec.queue,
            run_at=spec.run_at,
            max_attempts=spec.max_attempts,
            dedupe_key=spec.dedupe_key,
            payload_version=spec.payload_version,
            source_event_id=spec.source_event_id,
            provider_request_id=spec.provider_request_id,
            idempotency_scope=spec.idempotency_scope,
        )

    for spec in specs:
        try:
            if app is not None:
                with app.app_context():
                    _enqueue_one(spec)
            else:
                _enqueue_one(spec)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="durable_queue.after_commit_enqueue",
                log_key="durable_queue.after_commit_enqueue",
                log_window_seconds=300,
            )


@event.listens_for(SASession, "after_rollback")
def _clear_after_commit_enqueues(session: SASession) -> None:
    session.info.pop(_AFTER_COMMIT_ENQUEUE_KEY, None)


def build_queue_from_app(app) -> DurableQueue:
    from config import Config

    app_config = getattr(app, "config", {}) or {}
    settings = QueueSettings(
        poll_seconds=app_config.get(
            "DURABLE_QUEUE_POLL_SECONDS", Config.DURABLE_QUEUE_POLL_SECONDS
        ),
        lock_ttl_seconds=app_config.get(
            "DURABLE_QUEUE_LOCK_TTL_SECONDS", Config.DURABLE_QUEUE_LOCK_TTL_SECONDS
        ),
        max_backoff_seconds=app_config.get(
            "DURABLE_QUEUE_MAX_BACKOFF_SECONDS",
            Config.DURABLE_QUEUE_MAX_BACKOFF_SECONDS,
        ),
        worker_heartbeat_interval_seconds=app_config.get(
            "WORKER_HEARTBEAT_INTERVAL_SECONDS",
            getattr(Config, "WORKER_HEARTBEAT_INTERVAL_SECONDS", 30),
        ),
    )
    return DurableQueue(settings=settings)
