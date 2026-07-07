from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Iterable, Optional

from flask import current_app, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.services.ops.background import BackgroundService
from app.services.workflow.deferred_task_executor import (
    DeferredSyncRunError,
    _critical_deferred_failures,
    _run_deferred_sync,
    run_deferred_sync_task,
)
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)

_QUEUE_KEY = "_deferred_docket_sync_queue"
_WF_TASK_QUEUE_KEY = "_deferred_workflow_task_sync_queue"
# Annuity keys (?)
_KEY_ANNUITY_IDS = "annuity_sync_ids"
_KEY_ANNUITY_MATTER_IDS = "annuity_sync_matter_ids"

# Old annuity key (kept for compatibility with old handler, though unused by new logic)
_ANN_QUEUE_KEY = "_deferred_annuity_sync_queue"

_IN_HANDLER_KEY = "_in_deferred_docket_sync_handler"
_DEDUPE_KEYS_KEY = "_deferred_dedupe_keys"
_META_KEY = "_deferred_sync_meta"
_MODULE_INITIALIZED = False
def _run_operational_sync_immediately(
    d_queue: dict[str, int | None],
    a_queue: dict[str, int | None],
    t_queue: dict[int, int | None],
) -> None:
    """Run DocketItem/WorkflowTask sync immediately after commit.

    These queues create/update user-visible Task rows.
    """
    if not d_queue and not a_queue and not t_queue:
        return
    try:
        app = current_app._get_current_object()
    except Exception:
        return
    try:
        if bool(getattr(app, "config", {}).get("TESTING")):
            return
    except Exception:
        return
    BackgroundService.run_async(
        _run_deferred_sync,
        dict(d_queue),
        dict(a_queue),
        {},
        dict(t_queue),
        _critical=True,
        _context="after_commit.operational_docket_workflow_sync",
    )


def _in_transaction() -> bool:
    try:
        in_tx_fn = getattr(db.session, "in_transaction", None)
        if callable(in_tx_fn):
            return bool(in_tx_fn())
        get_tx = getattr(db.session, "get_transaction", None)
        if callable(get_tx):
            return bool(get_tx())
        return True
    except Exception:
        # fail-safe: assume yes
        return True


def _mark_dedupe_key(session: SASession, key: str) -> bool:
    keys = session.info.get(_DEDUPE_KEYS_KEY)
    if keys is None:
        keys = set()
        session.info[_DEDUPE_KEYS_KEY] = keys
    if key in keys:
        return False
    keys.add(key)
    return True


def set_deferred_sync_meta(meta: dict[str, Any] | None) -> None:
    """Attach best-effort correlation metadata to the next deferred sync job.

    Stored on the current Flask-SQLAlchemy session.info and consumed by the after_commit handler.
    """
    if not meta or not isinstance(meta, dict):
        return
    sess = db.session
    existing = sess.info.get(_META_KEY)
    if not isinstance(existing, dict):
        existing = {}
    merged: dict[str, Any] = dict(existing)
    for k, v in meta.items():
        if v is None:
            continue
        merged[str(k)] = v
    sess.info[_META_KEY] = merged


def enqueue_docket_sync(*, docket_item_id: str, actor_id: int | None = None) -> None:
    """Defer DocketItem -> Workflow/WorkLog sync until after the current transaction commits."""
    sess = db.session

    _mark_dedupe_key(sess, f"docket:{docket_item_id}")
    queue: dict[str, int | None] = sess.info.get(_QUEUE_KEY) or {}
    key = str(docket_item_id)
    if actor_id is not None:
        queue[key] = actor_id
    elif key not in queue:
        queue[key] = actor_id
    sess.info[_QUEUE_KEY] = queue


def enqueue_workflow_sync(*, workflow_id: int, commit: bool = True) -> None:
    """Compatibility no-op for removed external sync."""
    _ = workflow_id, commit
    return


def enqueue_workflow_task_sync(*, workflow_id: int, actor_id: int | None = None) -> None:
    """Defer Workflow -> Docket/WorkLog full sync until after commit."""
    sess = db.session
    queue: dict[int, int | None] = sess.info.get(_WF_TASK_QUEUE_KEY) or {}
    _mark_dedupe_key(sess, f"workflow_task:{workflow_id}")
    if workflow_id in queue:
        if queue[workflow_id] is None and actor_id is not None:
            queue[workflow_id] = actor_id
    else:
        queue[workflow_id] = actor_id
    sess.info[_WF_TASK_QUEUE_KEY] = queue


def enqueue_annuity_sync(annuity_id: str | int | None) -> None:
    """Defer AnnuityItem -> Workflow sync until after the current transaction commits."""
    if not annuity_id:
        return
    q: set[str] = db.session.info.setdefault(_KEY_ANNUITY_IDS, set())
    q.add(str(annuity_id))
    # Durable queue (best-effort): resolve matter_id and enqueue matter rebuild
    try:
        from app.utils.policy_sql import policy_text as text

        row = db.session.execute(
            text(
                "SELECT matter_id FROM annuity_item WHERE annuity_id = :aid LIMIT 1"
            ).execution_options(policy_bypass=True),
            {"aid": str(annuity_id)},
        ).scalar()
        if row:
            enqueue_annuity_sync_for_matter(str(row))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deferred_task_sync.enqueue_annuity_sync.lookup",
            log_key="deferred_task_sync.enqueue_annuity_sync.lookup",
            log_window_seconds=300,
        )
    # commit   :  if none Immediate  row
    if not _in_transaction():
        _drain_annuity_sync_now()


def enqueue_annuity_sync_for_item(annuity_item: Any) -> None:
    # item.id flush    matter   ()
    if getattr(annuity_item, "annuity_id", None):
        enqueue_annuity_sync(annuity_item.annuity_id)
    matter_id = getattr(annuity_item, "matter_id", None)
    if matter_id:
        enqueue_annuity_sync_for_matter(matter_id)


def enqueue_annuity_sync_for_matter(matter_id: str | None) -> None:
    if not matter_id:
        return
    q: set[str] = db.session.info.setdefault(_KEY_ANNUITY_MATTER_IDS, set())
    q.add(str(matter_id))
    # Durable queue (DB) - so it won't be lost if thread/process dies
    try:
        from app.services.annuity.annuity_sync_queue import enqueue_annuity_matter_rebuild

        enqueue_annuity_matter_rebuild(str(matter_id), reason="annuity_changed")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deferred_task_sync.enqueue_annuity_sync_for_matter.enqueue",
            log_key="deferred_task_sync.enqueue_annuity_sync_for_matter.enqueue",
            log_window_seconds=300,
        )
    if not _in_transaction():
        _drain_annuity_sync_now()


def enqueue_docket_sync_for_item(*, docket_item: Any, actor_id: int | None = None) -> None:
    """Convenience wrapper that accepts a DocketItem ORM instance."""
    docket_item_id = getattr(docket_item, "docket_id", None)
    if not docket_item_id:
        try:
            db.session.flush()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deferred_task_sync.enqueue_docket_sync_for_item.flush",
                log_key="deferred_task_sync.enqueue_docket_sync_for_item.flush",
                log_window_seconds=300,
            )
            return
        docket_item_id = getattr(docket_item, "docket_id", None)
    if docket_item_id:
        enqueue_docket_sync(docket_item_id=str(docket_item_id), actor_id=actor_id)


def _run_annuity_sync(
    app,
    annuity_ids: set[str],
    matter_ids: set[str],
    *,
    ensure_rows: bool = False,
    refresh_registration_date: bool = False,
) -> None:
    with app.app_context():
        try:
            from app.services.annuity.annuity_sync_queue import run_annuity_workflow_sync_task

            run_annuity_workflow_sync_task(
                {
                    "annuity_ids": sorted(str(aid) for aid in annuity_ids if aid),
                    "matter_ids": sorted(str(mid) for mid in matter_ids if mid),
                    "ensure": bool(ensure_rows),
                    "refresh_registration_date": bool(refresh_registration_date),
                }
            )
        except Exception:
            logger.exception("Deferred annuity queue drain failed")
        finally:
            try:
                db.session.rollback()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="deferred_task_sync.run_annuity_sync.rollback",
                    log_key="deferred_task_sync.run_annuity_sync.rollback",
                    log_window_seconds=300,
                )
            try:
                db.session.remove()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="deferred_task_sync.run_annuity_sync.remove",
                    log_key="deferred_task_sync.run_annuity_sync.remove",
                    log_window_seconds=300,
                )


def _annuity_sync_payload(
    *,
    annuity_ids: Iterable[str | None],
    matter_ids: Iterable[str | None],
    ensure_rows: bool = False,
    refresh_registration_date: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "annuity_ids": sorted({str(aid).strip() for aid in annuity_ids if str(aid or "").strip()}),
        "matter_ids": sorted({str(mid).strip() for mid in matter_ids if str(mid or "").strip()}),
    }
    if ensure_rows:
        payload["ensure"] = True
    if refresh_registration_date:
        payload["refresh_registration_date"] = True
    return payload


def _enqueue_annuity_sync_durable(
    app,
    *,
    annuity_ids: Iterable[str | None],
    matter_ids: Iterable[str | None],
    ensure_rows: bool = False,
    refresh_registration_date: bool = False,
    allow_testing: bool = False,
) -> bool:
    payload = _annuity_sync_payload(
        annuity_ids=annuity_ids,
        matter_ids=matter_ids,
        ensure_rows=ensure_rows,
        refresh_registration_date=refresh_registration_date,
    )
    if not payload["annuity_ids"] and not payload["matter_ids"]:
        return True

    try:
        config = getattr(app, "config", {}) or {}
        if bool(config.get("TESTING")) and not allow_testing:
            return True
        enabled = bool(
            config.get(
                "ANNUITY_SYNC_DURABLE_QUEUE_ENABLED",
                config.get("DEFERRED_TASKS_DURABLE_QUEUE_ENABLED", True),
            )
        )
    except Exception:
        enabled = True
    if not enabled:
        return False

    try:
        from app.ops.durable_queue import build_queue_from_app

        dedupe_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        dedupe_key = (
            "annuity.workflow_sync:" + hashlib.sha256(dedupe_raw.encode("utf-8")).hexdigest()
        )
        source_ids = payload["matter_ids"] or payload["annuity_ids"]
        source_event_id = source_ids[0] if len(source_ids) == 1 else None

        queue = build_queue_from_app(app)
        queue.enqueue(
            task="annuity.workflow_sync",
            payload=payload,
            queue="annuity",
            dedupe_key=dedupe_key,
            source_event_id=source_event_id,
            idempotency_scope="annuity.workflow_sync",
        )
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deferred_task_sync.enqueue_annuity_sync_durable",
            log_key="deferred_task_sync.enqueue_annuity_sync_durable",
            log_window_seconds=300,
        )
        return False


def enqueue_annuity_sync_for_matter_ids_after_commit(
    matter_ids: Iterable[str | None],
    *,
    allow_testing_durable: bool = True,
) -> None:
    """Schedule annuity sync worker after commit without touching the current DB session.

    This helper is safe to call from SQLAlchemy ``after_commit`` handlers where issuing SQL
    on the same committed session would fail.
    """
    mids = {str(mid).strip() for mid in matter_ids if (mid or "").strip()}
    if not mids:
        return
    try:
        app = current_app._get_current_object()
    except Exception:
        logger.warning("No current_app; cannot schedule annuity sync after commit.")
        return
    if _enqueue_annuity_sync_durable(
        app,
        annuity_ids=set(),
        matter_ids=mids,
        allow_testing=allow_testing_durable,
    ):
        return
    BackgroundService.run_async(
        _run_annuity_sync,
        app,
        set(),
        mids,
        _critical=True,
        _context="after_commit.annuity_sync",
    )


def enqueue_annuity_ensure_for_matter_ids_after_commit(
    matter_ids: Iterable[str | None],
    *,
    allow_testing_durable: bool = True,
) -> None:
    """Schedule annuity row generation and workflow sync after commit."""
    mids = {str(mid).strip() for mid in matter_ids if (mid or "").strip()}
    if not mids:
        return
    try:
        app = current_app._get_current_object()
    except Exception:
        logger.warning("No current_app; cannot schedule annuity ensure after commit.")
        return
    if _enqueue_annuity_sync_durable(
        app,
        annuity_ids=set(),
        matter_ids=mids,
        ensure_rows=True,
        refresh_registration_date=True,
        allow_testing=allow_testing_durable,
    ):
        return
    BackgroundService.run_async(
        _run_annuity_sync,
        app,
        set(),
        mids,
        ensure_rows=True,
        refresh_registration_date=True,
        _critical=True,
        _context="after_commit.annuity_ensure",
    )


def _drain_annuity_sync_now() -> None:
    try:
        app = current_app._get_current_object()
    except Exception:
        logger.warning("No current_app; cannot run annuity sync now.")
        return
    annuity_ids: set[str] = set(db.session.info.pop(_KEY_ANNUITY_IDS, set()) or set())
    matter_ids: set[str] = set(db.session.info.pop(_KEY_ANNUITY_MATTER_IDS, set()) or set())
    if not annuity_ids and not matter_ids:
        return
    if _enqueue_annuity_sync_durable(app, annuity_ids=annuity_ids, matter_ids=matter_ids):
        return
    BackgroundService.run_async(
        _run_annuity_sync,
        app,
        annuity_ids,
        matter_ids,
        _critical=True,
        _context="after_commit.annuity_sync",
    )


def _after_flush_collect_workflow_sync(session, flush_context) -> None:
    """Compatibility hook for older listener registration.

    Workflow/task sync is now queued explicitly through enqueue_* helpers. Keep
    the after_flush listener as a no-op so bootstrap does not fail.
    """
    _ = session, flush_context




def _enqueue_deferred_sync_durable(
    dq: dict[str, int | None],
    aq: dict[str, int | None],
    wq: dict[int, bool],
    tq: dict[int, int | None],
    *,
    meta: dict[str, Any] | None = None,
) -> bool:
    try:
        app = current_app._get_current_object()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deferred_task_sync._enqueue_deferred_sync_durable.current_app",
            log_key="deferred_task_sync._enqueue_deferred_sync_durable.current_app",
            log_window_seconds=300,
        )
        return False
    try:
        from app.ops.durable_queue import build_queue_from_app

        testing = False
        try:
            testing = bool(getattr(app, "config", {}).get("TESTING"))
        except Exception:
            testing = False
        if testing:
            return True

        if not dq and not aq and not tq:
            return True

        queue = build_queue_from_app(app)
        payload = {
            "docket_queue": {str(k): v for k, v in dq.items()},
            "annuity_queue": {str(k): v for k, v in aq.items()},
            "workflow_queue": {},
            "workflow_task_queue": {str(k): v for k, v in tq.items()},
        }
        if meta and isinstance(meta, dict):
            payload["_meta"] = dict(meta)
        dedupe_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        dedupe_key = "deferred.sync:" + hashlib.sha256(dedupe_raw.encode("utf-8")).hexdigest()
        queue.enqueue(
            task="deferred.sync",
            payload=payload,
            queue="deferred",
            dedupe_key=dedupe_key,
            idempotency_scope="deferred.sync",
        )
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="deferred_task_sync.enqueue_durable",
            log_key="deferred_task_sync.enqueue_durable",
            log_window_seconds=300,
        )
        # Tests often run on SQLite in-memory with StaticPool (single shared connection).
        # Falling back to background threads can race with test DB setup/teardown and
        # cause flaky DDL errors. Prefer a no-op success in TESTING when durable
        # enqueue is unavailable.
        testing = False
        try:
            testing = bool(getattr(app, "config", {}).get("TESTING"))
        except Exception:
            testing = False
        if testing:
            return True
        return False


@event.listens_for(SASession, "after_commit")
def _after_commit(session):
    #      row
    # NOTE: begin_nested() (SAVEPOINT) commits can also trigger Session after_commit.
    # Only drain when the outermost transaction has finished.
    try:
        if session.in_transaction():
            return
    except Exception:
        return
    try:
        app = current_app._get_current_object()
    except Exception:
        return
    annuity_ids = set(session.info.pop(_KEY_ANNUITY_IDS, set()) or set())
    matter_ids = set(session.info.pop(_KEY_ANNUITY_MATTER_IDS, set()) or set())
    if not annuity_ids and not matter_ids:
        return
    if _enqueue_annuity_sync_durable(app, annuity_ids=annuity_ids, matter_ids=matter_ids):
        return
    BackgroundService.run_async(
        _run_annuity_sync,
        app,
        annuity_ids,
        matter_ids,
        _critical=True,
        _context="after_commit.annuity_sync",
    )


def _after_commit_docket(session) -> None:
    # NOTE: begin_nested() (SAVEPOINT) commits can also trigger Session after_commit.
    # Skip draining during nested SAVEPOINT commits; otherwise background work can
    # interfere with the still-open outer transaction (notably on SQLite + StaticPool).
    in_nested = False
    try:
        in_nested = bool(session.in_nested_transaction())
    except Exception:
        in_nested = False
    if in_nested:
        return
    if session.info.get(_IN_HANDLER_KEY):
        return

    meta = session.info.pop(_META_KEY, None) or {}
    d_queue: dict[str, int | None] = session.info.pop(_QUEUE_KEY, None) or {}
    a_queue: dict[str, int | None] = session.info.pop(_ANN_QUEUE_KEY, None) or {}
    w_queue: dict[int, bool] = {}
    t_queue: dict[int, int | None] = session.info.pop(_WF_TASK_QUEUE_KEY, None) or {}
    session.info.pop(_DEDUPE_KEYS_KEY, None)

    if not d_queue and not a_queue and not t_queue:
        return

    try:
        session.info[_IN_HANDLER_KEY] = True
        if not has_app_context():
            logger.error("Deferred docket sync skipped: no app context")
            return
        _run_operational_sync_immediately(d_queue, a_queue, t_queue)
        use_durable = bool(current_app.config.get("DEFERRED_TASKS_DURABLE_QUEUE_ENABLED", True))
        if use_durable and _enqueue_deferred_sync_durable(
            d_queue, a_queue, w_queue, t_queue, meta=meta
        ):
            return

        BackgroundService.run_async(
            _run_deferred_sync,
            d_queue,
            a_queue,
            w_queue,
            t_queue,
            _critical=True,
            _context="after_commit.docket_workflow_sync",
        )
    except Exception as exc:
        logger.error("Deferred docket sync failed: %s", exc)
    finally:
        session.info.pop(_IN_HANDLER_KEY, None)


def _after_rollback_deferred(session) -> None:
    session.info.pop(_QUEUE_KEY, None)
    session.info.pop(_ANN_QUEUE_KEY, None)
    session.info.pop(_WF_TASK_QUEUE_KEY, None)
    session.info.pop(_IN_HANDLER_KEY, None)
    session.info.pop(_DEDUPE_KEYS_KEY, None)
    session.info.pop(_META_KEY, None)
    session.info.pop(_KEY_ANNUITY_IDS, None)
    session.info.pop(_KEY_ANNUITY_MATTER_IDS, None)


def init_deferred_docket_sync() -> None:
    """Register SQLAlchemy session events once to run deferred sync after commit."""
    global _MODULE_INITIALIZED
    if _MODULE_INITIALIZED:
        return

    listeners = (
        ("after_flush", _after_flush_collect_workflow_sync),
        ("after_commit", _after_commit_docket),
        ("after_rollback", _after_rollback_deferred),
    )
    for event_name, listener in listeners:
        if event.contains(SASession, event_name, listener):
            continue
        event.listen(SASession, event_name, listener)

    _MODULE_INITIALIZED = True
