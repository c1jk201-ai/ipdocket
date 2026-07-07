from __future__ import annotations

import logging
from typing import Any

from flask import current_app, has_app_context
from sqlalchemy.exc import DBAPIError, PendingRollbackError

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)

_IN_HANDLER_KEY = "_in_deferred_docket_sync_handler"
_WF_QUEUE_KEY = "_deferred_workflow_sync_queue"


class DeferredSyncRunError(RuntimeError):
    """Raised when one or more deferred sync sub-operations fail."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(self.failures))


def _critical_deferred_failures(failures: list[str]) -> list[str]:
    """Return failures that should make the durable job retry.

    Workflow-only compatibility queue entries are intentionally non-critical.
    """
    return [failure for failure in failures if not failure.startswith("workflow:")]


def _is_connection_invalidated_error(exc: Exception) -> bool:
    if isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False)):
        return True
    try:
        msg = str(exc).lower()
    except Exception:
        msg = ""
    return any(
        marker in msg
        for marker in (
            "server closed the connection unexpectedly",
            "can't reconnect until invalid transaction is rolled back",
            "connection reset by peer",
            "connection refused",
            "could not connect to server",
            "terminating connection",
        )
    )


def _ignore_removed_workflow_sync_jobs(wq: dict[int, bool]) -> list[str]:
    _ = wq
    return []


def _run_deferred_sync(
    dq: dict[str, int | None],
    aq: dict[str, int | None],
    wq: dict[int, bool],
    tq: dict[int, int | None],
    *,
    raise_on_error: bool = False,
) -> None:
    failures: list[str] = []
    try:
        db.session.info[_IN_HANDLER_KEY] = True

        def _safe_rollback(context: str) -> None:
            try:
                db.session.rollback()
            except Exception as exc:
                if not _is_connection_invalidated_error(exc):
                    report_swallowed_exception(
                        exc,
                        context=f"deferred_task_sync.{context}.rollback",
                        log_key=f"deferred_task_sync.{context}.rollback",
                        log_window_seconds=300,
                    )
            try:
                db.session.remove()
            except Exception as exc:
                if not _is_connection_invalidated_error(exc):
                    report_swallowed_exception(
                        exc,
                        context=f"deferred_task_sync.{context}.remove",
                        log_key=f"deferred_task_sync.{context}.remove",
                        log_window_seconds=300,
                    )
            # after_rollback clears handler flags; keep guard enabled in this worker.
            db.session.info[_IN_HANDLER_KEY] = True

        def _heal_session_if_inactive(context: str) -> None:
            try:
                if bool(getattr(db.session, "is_active", True)):
                    return
            except Exception:
                return
            _safe_rollback(context)

        def _reset_session(context: str) -> None:
            # Start each iteration with a fresh scoped session to avoid reusing expired/detached ORM
            # instances across commit/rollback boundaries in long-running workers.
            try:
                db.session.remove()
            except Exception as exc:
                if not _is_connection_invalidated_error(exc):
                    report_swallowed_exception(
                        exc,
                        context=f"deferred_task_sync.{context}.remove",
                        log_key=f"deferred_task_sync.{context}.remove",
                        log_window_seconds=300,
                    )
            db.session.info[_IN_HANDLER_KEY] = True

        def _merge_generated_workflow_queue(target: dict[int, bool]) -> None:
            generated = db.session.info.pop(_WF_QUEUE_KEY, None) or {}
            for raw_wf_id, raw_commit in generated.items():
                try:
                    wf_id = int(raw_wf_id)
                except Exception:
                    continue
                target[wf_id] = bool(target.get(wf_id)) or bool(raw_commit)

        # Defensive cleanup: background workers can inherit a poisoned SQLAlchemy session.
        _heal_session_if_inactive("run_deferred_sync.bootstrap")

        from app.models.ip_records import DocketItem
        from app.models.workflow import Workflow
        from app.services.workflow.task_sync import (
            sync_from_annuity_item,
            sync_from_docket_item,
            sync_from_workflow,
        )

        generated_wq: dict[int, bool] = {}

        if dq:
            for docket_id, actor_id in dq.items():
                _heal_session_if_inactive("run_deferred_sync.docket_load")
                di = db.session.get(DocketItem, str(docket_id))
                if not di:
                    continue
                try:
                    sync_from_docket_item(docket_item=di, actor_id=actor_id)
                    db.session.commit()
                    _merge_generated_workflow_queue(generated_wq)
                except Exception as exc:
                    logger.error("Deferred docket sync failed for %s: %s", docket_id, exc)
                    failures.append(f"docket:{docket_id}:{type(exc).__name__}")
                    _safe_rollback("run_deferred_sync.docket")
                finally:
                    _reset_session("run_deferred_sync.docket_post")

        # Old annuity handler (aq keys are empty in new logic, but kept for safety)
        if aq:
            for annuity_id, _actor_id in aq.items():
                try:
                    sync_from_annuity_item(annuity_id=str(annuity_id))
                    db.session.commit()
                    _merge_generated_workflow_queue(generated_wq)
                except Exception as exc:
                    logger.error("Deferred annuity sync failed for %s: %s", annuity_id, exc)
                    failures.append(f"annuity:{annuity_id}:{type(exc).__name__}")
                    _safe_rollback("run_deferred_sync.annuity")
                finally:
                    _reset_session("run_deferred_sync.annuity_post")

        if tq:
            for wf_id, actor_id in tq.items():
                _heal_session_if_inactive("run_deferred_sync.workflow_task_load")
                wf = db.session.get(Workflow, int(wf_id))
                if not wf:
                    continue
                try:
                    sync_from_workflow(workflow=wf, actor_id=actor_id)
                    db.session.commit()
                    _merge_generated_workflow_queue(generated_wq)
                except Exception as exc:
                    logger.error("Deferred workflow task sync failed for %s: %s", wf_id, exc)
                    failures.append(f"workflow_task:{wf_id}:{type(exc).__name__}")
                    _safe_rollback("run_deferred_sync.workflow_task")
                finally:
                    _reset_session("run_deferred_sync.workflow_task_post")

        if generated_wq:
            wq.update(generated_wq)

        if wq:
            failures.extend(_ignore_removed_workflow_sync_jobs(wq))

        try:
            db.session.commit()
        except PendingRollbackError as exc:
            logger.warning("[deferred] Commit skipped due to pending rollback state: %s", exc)
            _safe_rollback("run_deferred_sync.commit_pending_rollback")
        except Exception as exc:
            logger.error("[deferred] Commit failed: %s", exc)
            report_swallowed_exception(
                exc,
                context="deferred_task_sync.run_deferred_sync.commit",
                log_key="deferred_task_sync.run_deferred_sync.commit",
                log_window_seconds=300,
            )
            _safe_rollback("run_deferred_sync.commit")
        if failures and raise_on_error:
            raise DeferredSyncRunError(failures)
    except Exception as exc:
        logger.error("Deferred docket sync failed: %s", exc)
        if raise_on_error:
            raise
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="deferred_task_sync.run_deferred_sync.rollback",
                log_key="deferred_task_sync.run_deferred_sync.rollback",
                log_window_seconds=300,
            )
            try:
                db.session.remove()
            except Exception as remove_exc:
                report_swallowed_exception(
                    remove_exc,
                    context="deferred_task_sync.run_deferred_sync.rollback.remove",
                    log_key="deferred_task_sync.run_deferred_sync.rollback.remove",
                    log_window_seconds=300,
                )
    finally:
        try:
            db.session.info.pop(_IN_HANDLER_KEY, None)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deferred_task_sync.run_deferred_sync.clear_handler_flag",
                log_key="deferred_task_sync.run_deferred_sync.clear_handler_flag",
                log_window_seconds=300,
            )
        try:
            db.session.remove()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deferred_task_sync.run_deferred_sync.remove",
                log_key="deferred_task_sync.run_deferred_sync.remove",
                log_window_seconds=300,
            )


def run_deferred_sync_task(payload: dict[str, Any] | None = None, **kwargs) -> None:
    """Durable queue handler to process deferred docket/workflow sync payload.

    Supports both call styles:
    - run_deferred_sync_task(payload_dict)
    - run_deferred_sync_task(**payload_dict)  (legacy handler behavior)
    """
    if payload is None:
        payload = dict(kwargs or {})
    elif kwargs:
        merged = dict(payload)
        merged.update(kwargs)
        payload = merged

    if isinstance(payload, dict) and "docket_queue" not in payload and "payload" in payload:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            payload = inner

    docket_queue = (payload or {}).get("docket_queue") or {}
    annuity_queue = (payload or {}).get("annuity_queue") or {}
    workflow_queue = (payload or {}).get("workflow_queue") or {}
    workflow_task_queue = (payload or {}).get("workflow_task_queue") or {}

    dq: dict[str, int | None] = {str(k): v for k, v in docket_queue.items()}
    aq: dict[str, int | None] = {str(k): v for k, v in annuity_queue.items()}
    wq: dict[int, bool] = {}
    for key, val in workflow_queue.items():
        try:
            wq[int(key)] = bool(val)
        except Exception:
            continue

    tq: dict[int, int | None] = {}
    for key, val in workflow_task_queue.items():
        try:
            wf_id = int(key)
        except Exception:
            continue
        actor_id = None
        if val is not None:
            try:
                actor_id = int(val)
            except Exception:
                actor_id = None
        tq[wf_id] = actor_id

    try:
        _run_deferred_sync(dq, aq, wq, tq, raise_on_error=True)
    except DeferredSyncRunError as exc:
        critical_failures = _critical_deferred_failures(exc.failures)
        if critical_failures:
            raise DeferredSyncRunError(critical_failures) from exc
        logger.warning(
            "Deferred sync completed with non-critical workflow failures suppressed: %s",
            "; ".join(exc.failures),
        )
