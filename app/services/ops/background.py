from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import current_app, has_request_context

from app.services.ops.job_types import JobType
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)


class TaskRunner:
    def submit(self, func, *args, **kwargs):
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class ThreadPoolRunner(TaskRunner):
    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )

    def submit(self, func, *args, **kwargs):
        return self._executor.submit(func, *args, **kwargs)

    def shutdown(self) -> None:
        ex = self._executor
        self._executor = None
        if not ex:
            return
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
        except Exception:
            logger.exception("BackgroundService shutdown failed")


class BackgroundService:
    """
    Simple background task manager using ThreadPoolExecutor.
    Ensures that tasks are run within the Flask application context.
    """

    _runner = None
    _init_lock = threading.Lock()

    @classmethod
    def init_app(cls, app):
        """Initialize the executor with the app."""
        if cls._runner:
            return
        with cls._init_lock:
            if cls._runner:
                return
            try:
                max_workers = int(app.config.get("BACKGROUND_WORKERS", 2) or 2)
            except Exception:
                max_workers = 2
            max_workers = max(1, min(32, max_workers))
            cls._runner = ThreadPoolRunner(max_workers=max_workers, thread_name_prefix="bg_worker")
            app.logger.info(f"BackgroundService initialized with {max_workers} workers.")
            atexit.register(cls.shutdown)

    @classmethod
    def set_runner(cls, runner: TaskRunner | None) -> None:
        cls._runner = runner

    @classmethod
    def shutdown(cls):
        runner = cls._runner
        cls._runner = None
        if not runner:
            return
        runner.shutdown()

    @classmethod
    def run_async(cls, func, *args, **kwargs):
        """
        Submit a task to the background thread pool.
        The task will be executed within an application context.
        """
        # Capture output of current_app._get_current_object() to pass the real app object
        # accessing current_app inside the thread might fail if context is lost,
        # so we rely on the closure to capture the app instance.
        app = current_app._get_current_object()
        if bool(app.config.get("TESTING")) and not bool(
            app.config.get("BACKGROUND_RUN_ASYNC_IN_TESTS", False)
        ):
            # Avoid background threads touching SQLite in-memory StaticPool during tests.
            # Tests can opt-in via BACKGROUND_RUN_ASYNC_IN_TESTS=1.
            return None
        # Reserved kwargs (not forwarded to the task function).
        # - _critical=True: never drop the task (even in prod request context). If the pool
        #   is unavailable, run synchronously as a last resort.
        # - _context="...": tag for logs/error reports.
        critical = bool(kwargs.pop("_critical", False))
        ctx_tag = (str(kwargs.pop("_context", "")) or "").strip()
        task_kwargs = dict(kwargs)

        def task_wrapper(app_instance, f, *a, _raise_on_error: bool = False, **k):
            with app_instance.app_context():
                from app.extensions import db

                try:
                    f(*a, **k)
                except Exception:
                    logger.exception("Background task failed")
                    if _raise_on_error:
                        raise
                finally:
                    try:
                        db.session.rollback()
                    except Exception as exc:
                        # Best-effort cleanup should not mask rollback failures.
                        report_swallowed_exception(
                            exc,
                            context="background.BackgroundService.task_wrapper.rollback",
                            log_key="background.BackgroundService.task_wrapper.rollback",
                            log_window_seconds=300,
                        )
                    try:
                        db.session.remove()
                    except Exception as exc:
                        # Best-effort cleanup should not mask session removal failures.
                        report_swallowed_exception(
                            exc,
                            context="background.BackgroundService.task_wrapper.remove_session",
                            log_key="background.BackgroundService.task_wrapper.remove_session",
                            log_window_seconds=300,
                        )

        def _allow_sync_fallback_in_request() -> bool:
            try:
                return bool(app.config.get("BACKGROUND_ALLOW_SYNC_FALLBACK_IN_REQUEST", False))
            except Exception:
                return False

        def _run_detached_thread_fallback(reason: str) -> bool:
            # Never run critical request-scoped jobs inline on the current request/session.
            # after_commit hooks can call run_async() while the Session is in "committed"
            # transition state; inline SQL then raises InvalidRequestError cascades.
            try:
                worker = threading.Thread(
                    target=task_wrapper,
                    args=(app, func, *args),
                    kwargs={"_raise_on_error": False, **dict(task_kwargs)},
                    daemon=True,
                    name="bg_fallback",
                )
                worker.start()
                logger.warning(
                    "BackgroundService detached-thread fallback (%s). critical=%s ctx=%s",
                    reason,
                    bool(critical),
                    ctx_tag or "-",
                )
                return True
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="background.BackgroundService.detached_fallback",
                    log_key="background.BackgroundService.detached_fallback",
                    log_window_seconds=300,
                )
                return False

        def _should_run_sync_fallback() -> bool:
            in_request = has_request_context()
            # For request-scoped critical tasks, only allow explicit sync fallback.
            # Default is False to avoid reusing the request Session while it's committing.
            if critical and in_request:
                return _allow_sync_fallback_in_request()
            # Non-request critical tasks (CLI/worker contexts) may run inline as last resort.
            if critical:
                return True
            # In production, never run background tasks synchronously on request threads.
            if in_request and (not app.debug) and (not bool(app.config.get("TESTING"))):
                return _allow_sync_fallback_in_request()
            return True

        if not cls._runner:
            try:
                cls.init_app(app)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="background.BackgroundService.auto_init",
                    log_key="background.BackgroundService.auto_init",
                    log_window_seconds=300,
                )

        if not cls._runner:
            if critical and has_request_context():
                if _run_detached_thread_fallback("not_initialized"):
                    return None
            if _should_run_sync_fallback():
                logger.warning(
                    "BackgroundService not initialized. Running synchronously (%s). critical=%s ctx=%s",
                    JobType.INLINE,
                    bool(critical),
                    ctx_tag or "-",
                )
                task_wrapper(app, func, *args, _raise_on_error=False, **dict(task_kwargs))
                return None
            logger.error(
                "BackgroundService not initialized; dropping async task in request context. critical=%s ctx=%s",
                bool(critical),
                ctx_tag or "-",
            )
            report_swallowed_exception(
                RuntimeError("BackgroundService not initialized in request context"),
                context="background.BackgroundService.not_initialized_request",
                log_key="background.BackgroundService.not_initialized_request",
                log_window_seconds=300,
            )
            return None

        try:
            return cls._runner.submit(
                task_wrapper, app, func, *args, _raise_on_error=True, **dict(task_kwargs)
            )
        except Exception as e:
            if critical and has_request_context():
                if _run_detached_thread_fallback("submit_failed"):
                    return None
            if _should_run_sync_fallback():
                logger.warning(
                    "BackgroundService submit failed (%s). Running synchronously. critical=%s ctx=%s",
                    e,
                    bool(critical),
                    ctx_tag or "-",
                )
                task_wrapper(app, func, *args, **dict(task_kwargs))
                return None
            logger.error(
                "BackgroundService submit failed; dropping async task in request context: %s (critical=%s ctx=%s)",
                e,
                bool(critical),
                ctx_tag or "-",
            )
            report_swallowed_exception(
                e if isinstance(e, Exception) else RuntimeError("BackgroundService submit failed"),
                context="background.BackgroundService.submit_failed_request",
                log_key="background.BackgroundService.submit_failed_request",
                log_window_seconds=300,
            )
            return None
