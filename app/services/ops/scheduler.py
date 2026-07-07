import atexit
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Callable

from flask import g
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.models.job_run import JobRun
from app.services.ops.job_types import JobType
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)

_SINGLE_INSTANCE_JOB_KWARGS = {"max_instances": 1, "coalesce": True}


def _always_enabled(_app) -> bool:
    return True


@dataclass(frozen=True)
class _SchedulerImports:
    background_scheduler_cls: Any
    cron_trigger_cls: Any
    interval_trigger_cls: Any
    timezone: Any


@dataclass
class _SchedulerLockHandle:
    conn: Any = None
    file: Any = None
    key: int | None = None
    backend_pid: int | None = None

    def bind_extensions(self, app) -> None:
        if self.conn is not None:
            app.extensions["apscheduler_lock_conn"] = self.conn
            app.extensions["apscheduler_lock_backend_pid"] = self.backend_pid
            app.extensions["apscheduler_lock_key"] = self.key
        elif self.file is not None:
            app.extensions["apscheduler_lock_file"] = self.file

    def release(self, app) -> None:
        conn = self.conn
        file_handle = self.file
        self.conn = None
        self.file = None

        if conn is not None:
            from app.utils.policy_sql import policy_text as text

            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            except Exception as exc:
                app.logger.debug(
                    "Failed to release scheduler advisory lock: %s",
                    exc,
                    exc_info=True,
                )
            _close_scheduler_lock_conn(
                app,
                conn,
                "Failed to close scheduler advisory lock connection: %s",
            )

        if file_handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file_handle, fcntl.LOCK_UN)
            except Exception as exc:
                app.logger.debug(
                    "Failed to release scheduler file lock: %s",
                    exc,
                    exc_info=True,
                )
            _close_scheduler_lock_file(app, file_handle, "Failed to close scheduler lock file: %s")


@dataclass(frozen=True)
class _SchedulerJobSpec:
    id: str
    name: str
    runner: Callable[[Any], Any]
    trigger_factory: Callable[[Any, _SchedulerImports], Any]
    job_name: str | None = None
    enabled: Callable[[Any], bool] = _always_enabled
    scheduler_kwargs: dict[str, Any] = field(default_factory=dict)
    swallow_wrapper_exceptions: bool = False


def _acquire_file_lock(lock_path: str):
    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        try:
            os.makedirs(lock_dir, exist_ok=True)
        except Exception:
            return None
    try:
        lock_file = open(lock_path, "a+")
    except Exception:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            lock_file.close()
        except Exception as close_exc:
            logger.debug("Failed to close scheduler lock file: %s", close_exc, exc_info=True)
        return None
    return lock_file


def init_app(app):
    existing = app.extensions.get("apscheduler")
    if existing is not None:
        return existing

    if not _scheduler_enabled(app):
        return None

    if _should_skip_dev_reloader(app):
        return None

    scheduler_imports = _load_scheduler_imports(app)
    if scheduler_imports is None:
        return None

    lock_handle = _acquire_scheduler_lock(app)
    if lock_handle is None:
        return None

    scheduler = scheduler_imports.background_scheduler_cls(
        timezone=scheduler_imports.timezone,
        job_defaults={"max_instances": 1, "coalesce": True},
    )
    backoff = _JobBackoffController(app)

    try:
        _register_lock_health_job(app, scheduler, scheduler_imports, lock_handle)
        _register_scheduler_jobs(app, scheduler, scheduler_imports, backoff)
        _mark_interrupted_job_runs_on_startup(app)
        scheduler.start()
    except Exception:
        lock_handle.release(app)
        raise

    app.logger.info("APScheduler started (%s)", JobType.SCHEDULER)
    _record_scheduler_startup_heartbeat(app, backoff)
    atexit.register(lambda: lock_handle.release(app))
    atexit.register(lambda: scheduler.shutdown(wait=False))

    app.extensions["apscheduler"] = scheduler
    return scheduler


def _mark_interrupted_job_runs_on_startup(app) -> int:
    """Close job_runs left open by a previous scheduler process."""
    with app.app_context():
        now = datetime.utcnow()
        session = SASession(db.engine)
        context = "scheduler.startup.mark_interrupted_job_runs"
        try:
            rows = (
                session.query(JobRun)
                .filter(JobRun.status.in_(["queued", "running"]))
                .filter(JobRun.started_at.isnot(None))
                .filter(JobRun.started_at < now)
                .all()
            )
            for row in rows:
                row.status = "failed"
                row.finished_at = now
                row.error = row.error or "interrupted by scheduler startup"
            session.commit()
            if rows:
                app.logger.warning("Marked interrupted scheduler job_runs: %s", len(rows))
            return int(len(rows))
        except Exception as exc:
            _rollback_job_log_session(session, context=context)
            report_swallowed_exception(
                exc,
                context=context,
                log_key=context,
                log_window_seconds=300,
            )
            return 0
        finally:
            _close_job_log_session(session, context=context)


def _scheduler_enabled(app) -> bool:
    enabled = str(app.config.get("SCHEDULER_ENABLED", "1")).lower() in ("1", "true", "yes", "on")
    run_requested = bool(app.config.get("RUN_SCHEDULER")) or (
        os.environ.get("SCHEDULER_RUN_ANYWAY") == "1"
    )
    if enabled:
        return True

    if run_requested:
        msg = (
            "Scheduler run requested but SCHEDULER_ENABLED=0. "
            "Set SCHEDULER_ENABLED=1 (feature toggle) for the scheduler process."
        )
        app.logger.error(msg)
        if not app.debug:
            raise RuntimeError(msg)

    app.logger.info("Scheduler disabled by config (SCHEDULER_ENABLED=0)")
    return False


def _should_skip_dev_reloader(app) -> bool:
    return bool(
        app.debug
        and os.environ.get("FLASK_RUN_FROM_CLI") == "true"
        and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
    )


def _load_scheduler_imports(app) -> _SchedulerImports | None:
    try:
        import pytz
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:
        app.logger.warning("APScheduler not available, daily sync disabled: %s", exc)
        return None

    return _SchedulerImports(
        background_scheduler_cls=BackgroundScheduler,
        cron_trigger_cls=CronTrigger,
        interval_trigger_cls=IntervalTrigger,
        timezone=pytz.timezone("America/New_York"),
    )


def _config_int(
    app,
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    zero_uses_default: bool = True,
) -> int:
    raw_value = app.config.get(key, default)
    if zero_uses_default:
        raw_value = raw_value or default
    try:
        value = int(raw_value)
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _default_scheduler_lock_path(app) -> str:
    base_dir = app.config.get("BASE_DIR") or os.getcwd()
    return os.path.join(base_dir, "data", "scheduler.lock")


def _connect_scheduler_lock_conn():
    conn = db.engine.connect()
    try:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
    except Exception:
        _ = None
    return conn


def _read_scheduler_backend_pid(conn) -> int | None:
    from app.utils.policy_sql import policy_text as text

    try:
        return int(conn.execute(text("SELECT pg_backend_pid()")).scalar() or 0)
    except Exception:
        return None


def _close_scheduler_lock_conn(app, conn, message: str) -> None:
    try:
        conn.close()
    except Exception as exc:
        app.logger.debug(message, exc, exc_info=True)


def _close_scheduler_lock_file(app, lock_file, message: str) -> None:
    try:
        lock_file.close()
    except Exception as exc:
        app.logger.debug(message, exc, exc_info=True)


def _acquire_scheduler_lock(app) -> _SchedulerLockHandle | None:
    lock_key = int(app.config.get("SCHEDULER_ADVISORY_LOCK_KEY", 915000123))
    lock_handle = _SchedulerLockHandle(key=lock_key)
    lock_conn = None
    lock_file = None

    try:
        if getattr(db.engine.dialect, "name", "") == "postgresql":
            from app.utils.policy_sql import policy_text as text

            lock_conn = _connect_scheduler_lock_conn()
            got_lock = bool(
                lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar()
            )
            if not got_lock:
                _close_scheduler_lock_conn(
                    app,
                    lock_conn,
                    "Scheduler advisory lock close failed: %s",
                )
                app.logger.info("Scheduler not started (another instance holds advisory lock)")
                return None

            lock_handle.conn = lock_conn
            lock_handle.backend_pid = _read_scheduler_backend_pid(lock_conn)
        else:
            lock_path = app.config.get("SCHEDULER_FILE_LOCK_PATH") or _default_scheduler_lock_path(
                app
            )
            lock_file = _acquire_file_lock(lock_path)
            if not lock_file:
                app.logger.info("Scheduler not started (file lock already held)")
                return None
            lock_handle.file = lock_file

        lock_handle.bind_extensions(app)
        return lock_handle
    except Exception as exc:
        if lock_conn is not None:
            _close_scheduler_lock_conn(
                app,
                lock_conn,
                "Failed to close scheduler advisory lock connection: %s",
            )
        if lock_file is not None:
            _close_scheduler_lock_file(app, lock_file, "Failed to close scheduler lock file: %s")
        app.logger.error("Scheduler lock acquisition failed; scheduler disabled: %s", exc)
        return None


def _lock_healthcheck_interval(app) -> int:
    return _config_int(
        app,
        "SCHEDULER_LOCK_HEALTHCHECK_SECONDS",
        300,
        minimum=0,
    )


def _exit_on_lock_loss(app) -> bool:
    try:
        return bool(app.config.get("SCHEDULER_EXIT_ON_LOCK_LOSS", True))
    except Exception:
        return True


def _parse_hour_list(raw: object, *, default: str) -> list[int]:
    txt = str(raw if raw is not None else default).strip()
    if not txt:
        txt = default
    out: set[int] = set()
    for token in txt.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            hour = int(token)
        except Exception:
            continue
        if 0 <= hour <= 23:
            out.add(hour)
    return sorted(out)


def _recover_postgres_scheduler_lock(app, lock_handle: _SchedulerLockHandle) -> bool:
    from app.utils.policy_sql import policy_text as text

    recovery_conn = None
    try:
        recovery_conn = _connect_scheduler_lock_conn()
        recovered = bool(
            recovery_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_handle.key}
            ).scalar()
        )
        if not recovered:
            _close_scheduler_lock_conn(
                app,
                recovery_conn,
                "Failed to close unrecovered scheduler advisory lock connection: %s",
            )
            return False

        old_conn = lock_handle.conn
        lock_handle.conn = recovery_conn
        lock_handle.backend_pid = _read_scheduler_backend_pid(lock_handle.conn)
        lock_handle.bind_extensions(app)

        if old_conn is not None and old_conn is not lock_handle.conn:
            _close_scheduler_lock_conn(
                app,
                old_conn,
                "Failed to close stale scheduler advisory lock connection: %s",
            )

        app.logger.info(
            "Scheduler advisory lock connection recovered (backend_pid=%s)",
            lock_handle.backend_pid,
        )
        return True
    except Exception as recovery_exc:
        if recovery_conn is not None:
            _close_scheduler_lock_conn(
                app,
                recovery_conn,
                "Failed to close scheduler advisory lock recovery connection: %s",
            )
        app.logger.error(
            "Scheduler lock recovery failed; shutting down scheduler: %s",
            recovery_exc,
        )
        return False


def _run_scheduler_lock_health_check(app, scheduler, lock_handle: _SchedulerLockHandle) -> None:
    if lock_handle.conn is None:
        return

    failure_exc: Exception | None = None
    try:
        from app.utils.policy_sql import policy_text as text

        if lock_handle.backend_pid:
            current_pid = int(
                lock_handle.conn.execute(text("SELECT pg_backend_pid()")).scalar() or 0
            )
            if not current_pid or current_pid != lock_handle.backend_pid:
                raise RuntimeError(f"backend_pid_changed:{lock_handle.backend_pid}->{current_pid}")

        lock_handle.conn.execute(text("SELECT 1")).scalar()
        return
    except Exception as exc:
        failure_exc = exc
        app.logger.warning(
            "Scheduler lock health check failed (%s). Trying reconnect/reacquire.",
            exc,
        )

    if _recover_postgres_scheduler_lock(app, lock_handle):
        return

    app.logger.error("Scheduler lock lost; shutting down scheduler: %s", failure_exc)
    try:
        scheduler.shutdown(wait=False)
    except Exception as shutdown_exc:
        app.logger.debug(
            "Failed to shutdown scheduler after lock loss: %s",
            shutdown_exc,
            exc_info=True,
        )
    if _exit_on_lock_loss(app):
        os._exit(1)


def _register_lock_health_job(
    app, scheduler, scheduler_imports, lock_handle: _SchedulerLockHandle
) -> None:
    if lock_handle.conn is None:
        return

    interval_seconds = _lock_healthcheck_interval(app)
    if interval_seconds <= 0:
        return

    scheduler.add_job(
        partial(_run_scheduler_lock_health_check, app, scheduler, lock_handle),
        scheduler_imports.interval_trigger_cls(seconds=max(30, interval_seconds)),
        id="scheduler_lock_health",
        name="Scheduler Lock Health",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


class _JobBackoffController:
    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, object]] = {}

    def _enabled(self) -> bool:
        try:
            return bool(self._app.config.get("SCHEDULER_JOB_FAILURE_BACKOFF_ENABLED", True))
        except Exception:
            return True

    def _base_seconds(self) -> int:
        return _config_int(
            self._app,
            "SCHEDULER_JOB_FAILURE_BACKOFF_BASE_SECONDS",
            60,
            minimum=1,
        )

    def _max_seconds(self) -> int:
        return _config_int(
            self._app,
            "SCHEDULER_JOB_FAILURE_BACKOFF_MAX_SECONDS",
            3600,
            minimum=1,
        )

    def should_skip(self, job_name: str, *, now: datetime) -> tuple[bool, datetime | None, int]:
        if not self._enabled():
            return False, None, 0

        with self._lock:
            state = self._state.get(job_name) or {}
            next_allowed_at = state.get("next_allowed_at")
            failures = int(state.get("failures") or 0)

        if isinstance(next_allowed_at, datetime) and now < next_allowed_at:
            return True, next_allowed_at, failures
        return False, None, failures

    def record_success(self, job_name: str) -> None:
        if not self._enabled():
            return

        with self._lock:
            self._state.pop(job_name, None)

    def record_failure(self, job_name: str, *, now: datetime) -> tuple[int, int, datetime]:
        base = self._base_seconds()
        cap = max(base, self._max_seconds())

        with self._lock:
            state = self._state.get(job_name) or {}
            failures = int(state.get("failures") or 0) + 1
            delay = int(min(cap, base * (2 ** max(0, failures - 1))))
            next_allowed_at = now + timedelta(seconds=delay)
            self._state[job_name] = {
                "failures": failures,
                "next_allowed_at": next_allowed_at,
            }

        return failures, delay, next_allowed_at


def _cleanup_db_session(job_name: str) -> None:
    try:
        db.session.rollback()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"scheduler.{job_name}.rollback",
            log_key=f"scheduler.{job_name}.rollback",
            log_window_seconds=300,
        )
    try:
        db.session.remove()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"scheduler.{job_name}.remove_session",
            log_key=f"scheduler.{job_name}.remove_session",
            log_window_seconds=300,
        )


def _serialize_job_output(result) -> str | None:
    if result is None:
        return None
    try:
        return json.dumps(result)
    except Exception:
        return str(result)


def _close_job_log_session(session: SASession, *, context: str) -> None:
    try:
        session.close()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"{context}.close",
            log_key=f"{context}.close",
            log_window_seconds=300,
        )


def _rollback_job_log_session(session: SASession, *, context: str) -> None:
    try:
        session.rollback()
    except Exception as exc:
        try:
            session.invalidate()
        except Exception as invalidate_exc:
            report_swallowed_exception(
                invalidate_exc,
                context=f"{context}.invalidate",
                log_key=f"{context}.invalidate",
                log_window_seconds=300,
            )
        report_swallowed_exception(
            exc,
            context=f"{context}.rollback",
            log_key=f"{context}.rollback",
            log_window_seconds=300,
        )


def _insert_job_run(
    *,
    job_name: str,
    run_id: str,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    output_ref: str | None = None,
    error: str | None = None,
) -> None:
    session = SASession(db.engine)
    context = f"scheduler.{job_name}.job_run_insert"
    try:
        session.add(
            JobRun(
                job_name=job_name,
                run_id=run_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                request_id=run_id,
                output_ref=output_ref,
                error=error,
            )
        )
        session.commit()
    except Exception as exc:
        _rollback_job_log_session(session, context=context)
        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=300,
        )
    finally:
        _close_job_log_session(session, context=context)


def _finish_job_run(
    *,
    job_name: str,
    run_id: str,
    started_at: datetime,
    status: str,
    finished_at: datetime,
    output_ref: str | None = None,
    error: str | None = None,
) -> None:
    session = SASession(db.engine)
    context = f"scheduler.{job_name}.job_run_finish"
    try:
        job_run = session.query(JobRun).filter(JobRun.run_id == run_id).one_or_none()
        if job_run is None:
            job_run = JobRun(
                job_name=job_name,
                run_id=run_id,
                started_at=started_at,
                request_id=run_id,
            )
            session.add(job_run)
        job_run.status = status
        job_run.finished_at = finished_at
        job_run.output_ref = output_ref
        job_run.error = error
        session.commit()
    except Exception as exc:
        _rollback_job_log_session(session, context=context)
        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=300,
        )
    finally:
        _close_job_log_session(session, context=context)


def _run_with_job_log(app, backoff: _JobBackoffController, job_name: str, runner) -> None:
    run_id = uuid.uuid4().hex
    now = datetime.utcnow()

    skip, next_allowed_at, failures = backoff.should_skip(job_name, now=now)
    if skip:
        _insert_job_run(
            job_name=job_name,
            run_id=run_id,
            status="skipped",
            started_at=now,
            finished_at=now,
            output_ref=_serialize_job_output(
                {
                    "reason": "backoff",
                    "failures": failures,
                    "next_allowed_at": next_allowed_at.isoformat() if next_allowed_at else None,
                }
            ),
        )
        return

    try:
        g.request_id = run_id
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="scheduler._run_with_job_log.set_request_id",
            log_key="scheduler._run_with_job_log.set_request_id",
            log_window_seconds=300,
        )

    _insert_job_run(
        job_name=job_name,
        run_id=run_id,
        status="running",
        started_at=now,
    )

    status = "success"
    output_ref = None
    error = None
    try:
        result = runner()
        backoff.record_success(job_name)
        output_ref = _serialize_job_output(result)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        app.logger.exception("Scheduled job failed: %s", job_name)
        try:
            failures, delay, next_allowed = backoff.record_failure(job_name, now=datetime.utcnow())
            output_ref = _serialize_job_output(
                {
                    "failure_backoff": {
                        "failures": failures,
                        "delay_seconds": delay,
                        "next_allowed_at": next_allowed.isoformat(),
                    }
                }
            )
        except Exception as backoff_exc:
            report_swallowed_exception(
                backoff_exc,
                context="ops.scheduler.record_job_failure.backoff",
                log_key="ops.scheduler.record_job_failure.backoff",
                log_window_seconds=300,
            )
    finally:
        _finish_job_run(
            job_name=job_name,
            run_id=run_id,
            started_at=now,
            status=status,
            finished_at=datetime.utcnow(),
            output_ref=output_ref,
            error=error,
        )


def _execute_registered_job(app, backoff: _JobBackoffController, spec: _SchedulerJobSpec) -> None:
    job_name = spec.job_name or spec.id

    with app.app_context():
        try:
            _run_with_job_log(app, backoff, job_name, partial(spec.runner, app))
        except Exception as exc:
            if not spec.swallow_wrapper_exceptions:
                raise
            report_swallowed_exception(
                exc,
                context=f"scheduler.{job_name}.wrapper",
                log_key=f"scheduler.{job_name}.wrapper",
                log_window_seconds=300,
            )
        finally:
            _cleanup_db_session(job_name)


def _record_scheduler_startup_heartbeat(app, backoff: _JobBackoffController) -> None:
    job_name = "scheduler_heartbeat"

    with app.app_context():
        try:
            _run_with_job_log(
                app,
                backoff,
                job_name,
                partial(_run_scheduler_heartbeat_job, app),
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="scheduler.startup_heartbeat",
                log_key="scheduler.startup_heartbeat",
                log_window_seconds=300,
            )
        finally:
            _cleanup_db_session(job_name)


def register_job(
    app, scheduler, scheduler_imports, backoff: _JobBackoffController, spec: _SchedulerJobSpec
) -> bool:
    if not spec.enabled(app):
        return False

    trigger = spec.trigger_factory(app, scheduler_imports)
    if trigger is None:
        return False

    scheduler.add_job(
        partial(_execute_registered_job, app, backoff, spec),
        trigger,
        id=spec.id,
        name=spec.name,
        replace_existing=True,
        **dict(spec.scheduler_kwargs),
    )
    return True


def _register_scheduler_jobs(
    app, scheduler, scheduler_imports, backoff: _JobBackoffController
) -> None:
    for spec in _JOB_SPECS:
        register_job(app, scheduler, scheduler_imports, backoff, spec)


def _run_annuity_generation_job(app):
    from app.services.annuity.annuity_service import ensure_annuities_for_all_registered_matters

    processed, created = ensure_annuities_for_all_registered_matters(commit=True)
    app.logger.info(
        "Scheduled annuity generation completed: %s matters, %s items",
        processed,
        created,
    )
    return {"processed": processed, "created": created}


def _run_annuity_sync_queue_drain_job(app):
    from app.services.annuity.annuity_sync_queue import drain_annuity_sync_queue

    processed = drain_annuity_sync_queue(limit=200)
    if processed:
        app.logger.info("Annuity sync queue drained: %s matters", processed)
    return {"processed": processed or 0}


def _run_deadline_notifications_job(app):
    from app.services.deadlines.deadline_notifications import (
        EmailChannel,
        send_all_deadline_notifications,
    )

    email_sent, email_failed = send_all_deadline_notifications(channel=EmailChannel())

    sent = int(email_sent)
    failed = int(email_failed)
    app.logger.info(
        "Scheduled deadline notifications completed: %s sent, %s failed (email=%s/%s)",
        sent,
        failed,
        email_sent,
        email_failed,
    )
    return {
        "sent": sent,
        "failed": failed,
        "email": {"sent": email_sent, "failed": email_failed},
    }


def _run_deadline_notification_retry_job(app):
    from app.services.deadlines.deadline_notifications import (
        EmailChannel,
        retry_failed_deadline_notifications,
    )

    email_sent, email_failed = retry_failed_deadline_notifications(channel=EmailChannel())

    sent = int(email_sent)
    failed = int(email_failed)
    app.logger.info(
        "Deadline notification retry completed: %s sent, %s failed (email=%s/%s)",
        sent,
        failed,
        email_sent,
        email_failed,
    )
    return {
        "sent": sent,
        "failed": failed,
        "email": {"sent": email_sent, "failed": email_failed},
    }


def _run_deadline_auto_close_job(app):
    from app.services.deadlines.mgmt_deadlines import auto_close_post_due_deadlines

    result = auto_close_post_due_deadlines(commit=True)
    app.logger.info(
        "Scheduled deadline auto-close completed: %s closed, %s followups",
        result.get("closed"),
        result.get("followups"),
    )
    return result


def _run_office_action_auto_close_job(app):
    from app.services.matter.office_action_auto_close_usecase import run_office_action_auto_close

    limit = _config_int(
        app,
        "OFFICE_ACTION_AUTO_CLOSE_MATTER_LIMIT",
        200,
        minimum=1,
        maximum=2000,
    )
    return run_office_action_auto_close(limit_matters=limit)


def _run_worklog_docket_backfill_job(app):
    from app.services.workflow.docket_backfill import backfill_workflows_from_open_dockets

    lookahead_days = _config_int(
        app,
        "WORKLOG_AUTO_BACKFILL_LOOKAHEAD_DAYS",
        30,
        minimum=0,
        maximum=3650,
    )
    limit = _config_int(
        app,
        "WORKLOG_AUTO_BACKFILL_LIMIT",
        200,
        minimum=1,
        maximum=2000,
    )
    return backfill_workflows_from_open_dockets(
        today=date.today(),
        end_date=date.today() + timedelta(days=lookahead_days),
        bucket="",
        limit=limit,
        commit=True,
    )


def _run_housekeeping_job(app):
    from app.services.ops.housekeeping import run_housekeeping

    result = run_housekeeping()
    app.logger.info("Scheduled housekeeping completed: %s", result)
    return result


def _run_error_report_alerts_job(_app):
    from app.services.ops.error_report_monitor import send_error_report_alerts

    return send_error_report_alerts()


def _run_disk_monitor_job(_app):
    from app.services.ops.disk_monitor import check_disk_and_alert

    return check_disk_and_alert()



def _run_matter_status_recalc_queue_drain_job(app):
    from app.services.matter.matter_status_recalc_queue import drain_matter_status_recalc_queue

    limit = _config_int(
        app,
        "MATTER_STATUS_RECALC_QUEUE_DRAIN_LIMIT",
        200,
        minimum=1,
        maximum=5000,
    )
    result = drain_matter_status_recalc_queue(limit=limit)
    processed = int(result.get("processed", 0) or 0)
    if processed:
        app.logger.info(
            "Matter status recalc queue drained: processed=%s updated=%s failed=%s",
            processed,
            int(result.get("updated", 0) or 0),
            int(result.get("failed", 0) or 0),
        )
    return result


def _run_matter_status_cache_audit_job(app):
    from app.services.matter.matter_status_cache import audit_matter_status_cache_window

    limit = _config_int(
        app,
        "MATTER_STATUS_CACHE_AUDIT_LIMIT",
        5000,
        minimum=1,
    )
    commit_interval = _config_int(
        app,
        "MATTER_STATUS_CACHE_AUDIT_COMMIT_INTERVAL",
        100,
        minimum=1,
    )
    cursor_key = str(
        app.config.get(
            "MATTER_STATUS_CACHE_AUDIT_CURSOR_KEY",
            "MATTER_STATUS_CACHE_AUDIT_CURSOR",
        )
        or "MATTER_STATUS_CACHE_AUDIT_CURSOR"
    ).strip()

    result = audit_matter_status_cache_window(
        limit=limit,
        commit=True,
        commit_interval=commit_interval,
        cursor_key=cursor_key,
    )
    app.logger.info(
        "Matter status cache audit completed: processed=%s updated=%s errors=%s wrapped=%s cursor=%s->%s",
        result.get("processed", 0),
        result.get("updated", 0),
        result.get("errors", 0),
        bool(result.get("wrapped")),
        result.get("cursor_before", ""),
        result.get("cursor_after", ""),
    )
    return result


def _run_matter_status_cache_reconcile_job(app):
    from app.services.matter.matter_status_cache import reconcile_matter_status_cache_batch

    limit = _config_int(
        app,
        "MATTER_STATUS_CACHE_RECONCILE_LIMIT",
        0,
        minimum=0,
    )
    commit_interval = _config_int(
        app,
        "MATTER_STATUS_CACHE_RECONCILE_COMMIT_INTERVAL",
        100,
        minimum=1,
    )
    result = reconcile_matter_status_cache_batch(
        limit=limit or None,
        commit=True,
        commit_interval=commit_interval,
    )
    app.logger.info(
        "Matter status cache reconcile completed: processed=%s updated=%s errors=%s",
        result.get("processed", 0),
        result.get("updated", 0),
        result.get("errors", 0),
    )
    return result


def _run_scheduler_heartbeat_job(_app):
    return {"ok": True, "pid": os.getpid()}


def _fixed_cron_trigger(hour: int, minute: int):
    def _factory(_app, scheduler_imports):
        return scheduler_imports.cron_trigger_cls(hour=hour, minute=minute)

    return _factory


def _fixed_interval_trigger(*, seconds: int | None = None, minutes: int | None = None):
    def _factory(_app, scheduler_imports):
        trigger_kwargs: dict[str, int] = {}
        if seconds is not None:
            trigger_kwargs["seconds"] = seconds
        if minutes is not None:
            trigger_kwargs["minutes"] = minutes
        return scheduler_imports.interval_trigger_cls(**trigger_kwargs)

    return _factory


def _deadline_auto_close_enabled(app) -> bool:
    return bool(app.config.get("DEADLINE_AUTO_CLOSE_ENABLED", True))


def _deadline_auto_close_trigger(app, scheduler_imports):
    hour = _config_int(app, "DEADLINE_AUTO_CLOSE_HOUR", 0)
    minute = _config_int(app, "DEADLINE_AUTO_CLOSE_MINUTE", 35)
    return scheduler_imports.cron_trigger_cls(hour=hour, minute=minute)


def _office_action_auto_close_enabled(app) -> bool:
    return bool(app.config.get("OFFICE_ACTION_AUTO_CLOSE_ENABLED", True))


def _office_action_auto_close_trigger(app, scheduler_imports):
    interval_seconds = _config_int(
        app,
        "OFFICE_ACTION_AUTO_CLOSE_INTERVAL_SECONDS",
        300,
        minimum=60,
    )
    return scheduler_imports.interval_trigger_cls(seconds=interval_seconds)


def _worklog_docket_backfill_enabled(app) -> bool:
    return bool(app.config.get("WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED", False))


def _worklog_docket_backfill_trigger(app, scheduler_imports):
    interval_seconds = _config_int(
        app,
        "WORKLOG_AUTO_BACKFILL_INTERVAL_SECONDS",
        3600,
        minimum=300,
    )
    return scheduler_imports.interval_trigger_cls(seconds=interval_seconds)


def _housekeeping_enabled(app) -> bool:
    return bool(app.config.get("HOUSEKEEPING_ENABLED", True))


def _error_report_alerts_enabled(app) -> bool:
    return bool(app.config.get("ERROR_REPORT_ALERTS_ENABLED", False))


def _error_report_alerts_trigger(app, scheduler_imports):
    interval_minutes = _config_int(
        app,
        "ERROR_REPORT_ALERT_INTERVAL_MINUTES",
        60,
        minimum=5,
    )
    return scheduler_imports.interval_trigger_cls(minutes=interval_minutes)


def _disk_monitor_enabled(app) -> bool:
    return bool(app.config.get("DISK_MONITOR_ENABLED", True))



def _matter_status_recalc_queue_enabled(app) -> bool:
    return bool(app.config.get("MATTER_STATUS_RECALC_QUEUE_ENABLED", True))


def _matter_status_recalc_queue_trigger(app, scheduler_imports):
    interval_seconds = _config_int(
        app,
        "MATTER_STATUS_RECALC_QUEUE_INTERVAL_SECONDS",
        60,
        minimum=5,
        maximum=3600,
    )
    return scheduler_imports.interval_trigger_cls(seconds=interval_seconds)


def _matter_status_cache_audit_enabled(app) -> bool:
    return bool(app.config.get("MATTER_STATUS_CACHE_AUDIT_ENABLED", True))


def _matter_status_cache_audit_trigger(app, scheduler_imports):
    interval_seconds = _config_int(
        app,
        "MATTER_STATUS_CACHE_AUDIT_INTERVAL_SECONDS",
        300,
        minimum=30,
        maximum=86400,
    )
    return scheduler_imports.interval_trigger_cls(seconds=interval_seconds)


def _matter_status_cache_reconcile_enabled(app) -> bool:
    return bool(app.config.get("MATTER_STATUS_CACHE_RECONCILE_ENABLED", False))


def _matter_status_cache_reconcile_trigger(app, scheduler_imports):
    hour = _config_int(app, "MATTER_STATUS_CACHE_RECONCILE_HOUR", 4)
    minute = _config_int(app, "MATTER_STATUS_CACHE_RECONCILE_MINUTE", 50)
    return scheduler_imports.cron_trigger_cls(hour=hour, minute=minute)


def _scheduler_heartbeat_trigger(app, scheduler_imports):
    heartbeat_seconds = _config_int(
        app,
        "SCHEDULER_HEARTBEAT_INTERVAL_SECONDS",
        300,
        minimum=60,
        maximum=3600,
    )
    return scheduler_imports.interval_trigger_cls(seconds=heartbeat_seconds)


_JOB_SPECS = (
    _SchedulerJobSpec(
        id="daily_annuity_generation",
        name="Daily Annuity Auto-Generation",
        job_name="daily_annuity_generation",
        runner=_run_annuity_generation_job,
        trigger_factory=_fixed_cron_trigger(5, 30),
    ),
    _SchedulerJobSpec(
        id="annuity_sync_queue_drain",
        name="Annuity Sync Queue Drain",
        job_name="annuity_sync_queue_drain",
        runner=_run_annuity_sync_queue_drain_job,
        trigger_factory=_fixed_interval_trigger(seconds=60),
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="daily_deadline_notifications",
        name="Daily Deadline Notifications",
        job_name="daily_deadline_notifications",
        runner=_run_deadline_notifications_job,
        trigger_factory=_fixed_cron_trigger(9, 0),
    ),
    _SchedulerJobSpec(
        id="deadline_notification_retry",
        name="Deadline Notification Retry",
        job_name="deadline_notification_retry",
        runner=_run_deadline_notification_retry_job,
        trigger_factory=_fixed_interval_trigger(minutes=60),
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="daily_deadline_auto_close",
        name="Daily Deadline Auto-Close",
        job_name="daily_deadline_auto_close",
        runner=_run_deadline_auto_close_job,
        trigger_factory=_deadline_auto_close_trigger,
        enabled=_deadline_auto_close_enabled,
    ),
    _SchedulerJobSpec(
        id="office_action_auto_close",
        name="Office Action Auto Close",
        job_name="office_action_auto_close",
        runner=_run_office_action_auto_close_job,
        trigger_factory=_office_action_auto_close_trigger,
        enabled=_office_action_auto_close_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="worklog_docket_backfill",
        name="Task Log Deadline ",
        job_name="worklog_docket_backfill",
        runner=_run_worklog_docket_backfill_job,
        trigger_factory=_worklog_docket_backfill_trigger,
        enabled=_worklog_docket_backfill_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="daily_housekeeping",
        name="Daily Housekeeping",
        job_name="daily_housekeeping",
        runner=_run_housekeeping_job,
        trigger_factory=_fixed_cron_trigger(4, 10),
        enabled=_housekeeping_enabled,
    ),
    _SchedulerJobSpec(
        id="error_report_alerts",
        name="Error Report Alerts",
        job_name="error_report_alerts",
        runner=_run_error_report_alerts_job,
        trigger_factory=_error_report_alerts_trigger,
        enabled=_error_report_alerts_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="disk_monitor",
        name="Disk Monitor",
        job_name="disk_monitor",
        runner=_run_disk_monitor_job,
        trigger_factory=_fixed_interval_trigger(minutes=15),
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
        enabled=_disk_monitor_enabled,
        swallow_wrapper_exceptions=True,
    ),
    _SchedulerJobSpec(
        id="matter_status_recalc_queue_drain",
        name="Matter Status Recalc Queue Drain",
        job_name="matter_status_recalc_queue_drain",
        runner=_run_matter_status_recalc_queue_drain_job,
        trigger_factory=_matter_status_recalc_queue_trigger,
        enabled=_matter_status_recalc_queue_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="matter_status_cache_audit",
        name="Matter Status Cache Audit",
        job_name="matter_status_cache_audit",
        runner=_run_matter_status_cache_audit_job,
        trigger_factory=_matter_status_cache_audit_trigger,
        enabled=_matter_status_cache_audit_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="matter_status_cache_reconcile",
        name="Matter Status Cache Reconcile",
        job_name="matter_status_cache_reconcile",
        runner=_run_matter_status_cache_reconcile_job,
        trigger_factory=_matter_status_cache_reconcile_trigger,
        enabled=_matter_status_cache_reconcile_enabled,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
    _SchedulerJobSpec(
        id="scheduler_heartbeat",
        name="Scheduler Heartbeat",
        job_name="scheduler_heartbeat",
        runner=_run_scheduler_heartbeat_job,
        trigger_factory=_scheduler_heartbeat_trigger,
        scheduler_kwargs=_SINGLE_INSTANCE_JOB_KWARGS.copy(),
    ),
)
