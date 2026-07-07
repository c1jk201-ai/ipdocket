from __future__ import annotations

import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import bindparam

from app.extensions import db
from app.models.matter import Matter
from app.models.matter_status_recalc_queue import MatterStatusRecalcQueue
from app.services.core.config_service import ConfigService
from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter
from app.services.ops.queue_lock_heartbeat import QueueLockHeartbeat
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_BACKOFF_BASE_SECONDS = 30
_DEFAULT_BACKOFF_MAX_SECONDS = 3600
_DEFAULT_LOCK_TIMEOUT_SECONDS = 300
_DEFAULT_PICK_BATCH_SIZE = 100


def _get_int_config(key: str, default: int) -> int:
    value = ConfigService.get_int(key, default)
    return default if value is None else value


def _ready_pick_order_sql(*, dialect: str) -> str:
    if dialect == "postgresql":
        return "next_run_at ASC NULLS FIRST, updated_at ASC, matter_id ASC"
    return "next_run_at ASC, updated_at ASC, matter_id ASC"


def enqueue_matter_status_recalc(
    matter_id: str | None,
    *,
    reason: str | None = None,
    session=None,
) -> bool:
    """
    Upsert a matter status recalculation request.

    Queue rows are deduplicated by matter_id and revived on fresh changes even if a
    previous attempt exhausted retries.

    Returns:
        True when the queue row was recorded inside the caller transaction.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return False

    now = datetime.utcnow()
    payload = json.dumps(
        {
            "reason": reason,
            "enqueue_id": uuid.uuid4().hex,
            "enqueued_at": now.isoformat(timespec="microseconds"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    sess = session or db.session
    savepoint = None
    lock_timeout = _get_int_config(
        "MATTER_STATUS_RECALC_QUEUE_LOCK_TIMEOUT_SECONDS",
        _DEFAULT_LOCK_TIMEOUT_SECONDS,
    )
    expired_before = now - timedelta(seconds=int(lock_timeout))

    try:
        savepoint = sess.begin_nested()
        sess.execute(
            text(
                """
                INSERT INTO matter_status_recalc_queue (
                    matter_id, payload, attempts,
                    next_run_at, locked_at, lock_token, last_error,
                    created_at, updated_at
                ) VALUES (
                    :mid, :payload, 0,
                    :now, NULL, NULL, NULL,
                    :now, :now
                )
                ON CONFLICT (matter_id) DO UPDATE
                SET payload = EXCLUDED.payload,
                    attempts = CASE
                        WHEN matter_status_recalc_queue.lock_token IS NOT NULL
                         AND matter_status_recalc_queue.locked_at IS NOT NULL
                         AND matter_status_recalc_queue.locked_at >= :expired_before
                        THEN matter_status_recalc_queue.attempts
                        ELSE 0
                    END,
                    next_run_at = EXCLUDED.updated_at,
                    locked_at = CASE
                        WHEN matter_status_recalc_queue.lock_token IS NOT NULL
                         AND matter_status_recalc_queue.locked_at IS NOT NULL
                         AND matter_status_recalc_queue.locked_at >= :expired_before
                        THEN matter_status_recalc_queue.locked_at
                        ELSE NULL
                    END,
                    lock_token = CASE
                        WHEN matter_status_recalc_queue.lock_token IS NOT NULL
                         AND matter_status_recalc_queue.locked_at IS NOT NULL
                         AND matter_status_recalc_queue.locked_at >= :expired_before
                        THEN matter_status_recalc_queue.lock_token
                        ELSE NULL
                    END,
                    last_error = NULL,
                    updated_at = EXCLUDED.updated_at
                """
            ).execution_options(policy_bypass=True),
            {
                "mid": mid,
                "payload": payload,
                "now": now,
                "expired_before": expired_before,
            },
        )
        savepoint.commit()
        return True
    except Exception as exc:
        if savepoint is not None:
            try:
                savepoint.rollback()
            except Exception:
                logger.debug(
                    "Failed to rollback matter status recalc enqueue savepoint", exc_info=True
                )
        msg = (str(exc) or "").lower()
        log_key = "matter_status_recalc_queue.enqueue"
        if "no such table" in msg or "does not exist" in msg or "undefined table" in msg:
            log_key = "matter_status_recalc_queue.enqueue.missing_table"
        report_swallowed_exception(
            exc,
            context="matter_status_recalc_queue.enqueue_matter_status_recalc",
            log_key=log_key,
            log_window_seconds=300,
        )
        return False


def drain_matter_status_recalc_queue(
    *,
    limit: int = 200,
    matter_ids: Optional[Iterable[str]] = None,
) -> dict[str, int]:
    """
    Drain matter status recalculation queue.

    Returns a lightweight summary: processed / updated / failed.
    """
    max_attempts = _get_int_config(
        "MATTER_STATUS_RECALC_QUEUE_MAX_ATTEMPTS",
        _DEFAULT_MAX_ATTEMPTS,
    )
    base = _get_int_config(
        "MATTER_STATUS_RECALC_QUEUE_BACKOFF_BASE_SECONDS",
        _DEFAULT_BACKOFF_BASE_SECONDS,
    )
    cap = _get_int_config(
        "MATTER_STATUS_RECALC_QUEUE_BACKOFF_MAX_SECONDS",
        _DEFAULT_BACKOFF_MAX_SECONDS,
    )
    lock_timeout = _get_int_config(
        "MATTER_STATUS_RECALC_QUEUE_LOCK_TIMEOUT_SECONDS",
        _DEFAULT_LOCK_TIMEOUT_SECONDS,
    )

    token = uuid.uuid4().hex
    processed = 0
    updated = 0
    failed = 0

    try:
        from flask import current_app

        app = current_app._get_current_object()
    except Exception:
        app = None

    heartbeat_interval = max(5, min(60, int(lock_timeout) // 2 if lock_timeout else 30))

    def _build_heartbeat(*, mid: str | None = None, token_scope: bool = False):
        if app is None:
            return None
        return QueueLockHeartbeat(
            app,
            table="matter_status_recalc_queue",
            id_column=None if token_scope else "matter_id",
            id_value=None if token_scope else mid,
            token_column="lock_token",
            token_value=token,
            interval_seconds=heartbeat_interval,
        )

    def _finalize_success(
        *,
        mid: str,
        matter_changed: bool,
        locked_payload: str | None,
    ) -> None:
        nonlocal processed, updated
        row = db.session.execute(
            text(
                """
                SELECT payload
                FROM matter_status_recalc_queue
                WHERE matter_id = :mid
                  AND lock_token = :token
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid, "token": token},
        ).first()
        if not row:
            db.session.rollback()
            raise RuntimeError("matter status recalc lock lost before finalize")

        current_payload = row[0]
        if (current_payload or "") != (locked_payload or ""):
            db.session.execute(
                text(
                    """
                    UPDATE matter_status_recalc_queue
                    SET attempts = 0,
                        next_run_at = :now,
                        locked_at = NULL,
                        lock_token = NULL,
                        last_error = NULL,
                        updated_at = :now
                    WHERE matter_id = :mid
                      AND lock_token = :token
                    """
                ).execution_options(policy_bypass=True),
                {"mid": mid, "token": token, "now": datetime.utcnow()},
            )
            db.session.commit()
            processed += 1
            if matter_changed:
                updated += 1
            return

        deleted = (
            db.session.query(MatterStatusRecalcQueue)
            .filter(MatterStatusRecalcQueue.matter_id == mid)
            .filter(MatterStatusRecalcQueue.lock_token == token)
            .delete(synchronize_session="fetch")
        )
        if not deleted:
            db.session.rollback()
            raise RuntimeError("matter status recalc lock lost before finalize")
        db.session.commit()
        processed += 1
        if matter_changed:
            updated += 1

    def _record_failure(*, mid: str, err: str) -> None:
        nonlocal failed
        failed += 1
        try:
            row = db.session.execute(
                text(
                    """
                    SELECT attempts
                    FROM matter_status_recalc_queue
                    WHERE matter_id = :mid
                    """
                ).execution_options(policy_bypass=True),
                {"mid": mid},
            ).first()
            attempts = int(row[0]) if row and row[0] is not None else 0
        except Exception:
            attempts = 0

        try:
            params = {
                "mid": mid,
                "token": token,
                "err": err[:4000],
                "now": datetime.utcnow(),
                "next_run_at": None,
            }
            if 0 < attempts < int(max_attempts):
                delay = _compute_backoff_seconds(attempts=attempts, base=base, cap=cap)
                params["next_run_at"] = datetime.utcnow() + timedelta(seconds=delay)

            result = db.session.execute(
                text(
                    """
                    UPDATE matter_status_recalc_queue
                    SET last_error = :err,
                        next_run_at = :next_run_at,
                        locked_at = NULL,
                        lock_token = NULL,
                        updated_at = :now
                    WHERE matter_id = :mid
                      AND lock_token = :token
                    """
                ).execution_options(policy_bypass=True),
                params,
            )
            if result.rowcount:
                db.session.commit()
            else:
                db.session.rollback()
        except Exception:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="matter_status_recalc_queue.record_failure.rollback",
                    log_key="matter_status_recalc_queue.record_failure.rollback",
                    log_window_seconds=300,
                )
            logger.exception("Failed to record matter status recalc queue failure (mid=%s)", mid)

    def _drain_one(mid: str, *, use_row_heartbeat: bool = True) -> None:
        try:
            hb_ctx = _build_heartbeat(mid=mid) if use_row_heartbeat else None

            with hb_ctx or nullcontext():
                matter = db.session.get(Matter, mid)
                if matter is None or bool(getattr(matter, "is_deleted", False)):
                    _finalize_success(
                        mid=mid,
                        matter_changed=False,
                        locked_payload=locked_payloads.get(mid),
                    )
                    return

                result = apply_auto_status_cache_to_matter(matter=matter)
                if result.changed:
                    db.session.add(matter)

                if hb_ctx is not None and hb_ctx.lost:
                    raise RuntimeError("matter status recalc lock lost during processing")

                _finalize_success(
                    mid=mid,
                    matter_changed=bool(result.changed),
                    locked_payload=locked_payloads.get(mid),
                )
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="matter_status_recalc_queue.drain.rollback",
                    log_key="matter_status_recalc_queue.drain.rollback",
                    log_window_seconds=300,
                )
            _record_failure(mid=mid, err=str(exc) or "unknown error")
            logger.exception("Matter status recalc queue drain failed (mid=%s)", mid)

    if matter_ids is not None:
        requested = [str(mid).strip() for mid in matter_ids if (mid or "").strip()]
        locked_payloads = _lock_specific_items(
            requested,
            now=datetime.utcnow(),
            token=token,
            max_attempts=max_attempts,
            lock_timeout=lock_timeout,
        )
        locked = list(locked_payloads.keys())
        batch_hb_ctx = _build_heartbeat(token_scope=True) if len(locked) > 1 else None
        with batch_hb_ctx or nullcontext():
            for mid in locked:
                _drain_one(mid, use_row_heartbeat=batch_hb_ctx is None)
        return {"processed": processed, "updated": updated, "failed": failed}

    drained = 0
    target = int(limit or 0)
    pick_batch_size = max(1, min(_DEFAULT_PICK_BATCH_SIZE, target or _DEFAULT_PICK_BATCH_SIZE))
    while drained < target:
        batch_limit = min(pick_batch_size, target - drained)
        locked_payloads = _pick_and_lock_ready_items(
            limit=batch_limit,
            now=datetime.utcnow(),
            token=token,
            max_attempts=max_attempts,
            lock_timeout=lock_timeout,
        )
        mids = list(locked_payloads.keys())
        if not mids:
            break
        batch_hb_ctx = _build_heartbeat(token_scope=True) if len(mids) > 1 else None
        with batch_hb_ctx or nullcontext():
            for mid in mids:
                _drain_one(str(mid), use_row_heartbeat=batch_hb_ctx is None)
                drained += 1

    return {"processed": processed, "updated": updated, "failed": failed}


def run_matter_status_recalc_task(
    matter_ids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    clean_ids = [str(mid).strip() for mid in (matter_ids or []) if str(mid).strip()]
    task_limit = int(limit or len(clean_ids) or 200)
    return drain_matter_status_recalc_queue(
        limit=max(1, task_limit),
        matter_ids=clean_ids or None,
    )


def _compute_backoff_seconds(*, attempts: int, base: int, cap: int) -> int:
    try:
        exp = base * (2 ** max(0, attempts - 1))
        return int(min(cap, exp))
    except Exception:
        return int(min(cap, base))


def _select_ready_candidate_rows(
    *,
    limit: int,
    now: datetime,
    expired_before: datetime,
    max_attempts: int,
    dialect: str,
    include_expired_locked: bool,
) -> list[tuple[str, datetime | None, datetime | None]]:
    if limit <= 0:
        return []

    order_sql = _ready_pick_order_sql(dialect=dialect)
    lock_predicate = (
        "locked_at < :expired_before" if include_expired_locked else "locked_at IS NULL"
    )
    skip_locked_sql = "FOR UPDATE SKIP LOCKED" if dialect == "postgresql" else ""

    rows = db.session.execute(
        text(
            f"""
            SELECT matter_id, next_run_at, updated_at
            FROM matter_status_recalc_queue
            WHERE {lock_predicate}
              AND (next_run_at IS NULL OR next_run_at <= :now)
              AND COALESCE(attempts, 0) < :max_attempts
            ORDER BY {order_sql}
            LIMIT :limit
            {skip_locked_sql}
            """
        ).execution_options(policy_bypass=True),
        {
            "expired_before": expired_before,
            "now": now,
            "limit": int(limit),
            "max_attempts": int(max_attempts),
        },
    ).all()

    out: list[tuple[str, datetime | None, datetime | None]] = []
    for row in rows:
        if not row or not row[0]:
            continue
        out.append((str(row[0]), row[1], row[2]))
    return out


def _pick_and_lock_ready_items(
    *,
    limit: int,
    now: datetime,
    token: str,
    max_attempts: int,
    lock_timeout: int,
) -> dict[str, str | None]:
    mids: list[str] = []
    expired_before = now - timedelta(seconds=int(lock_timeout))

    try:
        dialect = getattr(db.engine.dialect, "name", "")
        unlocked_rows = _select_ready_candidate_rows(
            limit=int(limit),
            now=now,
            expired_before=expired_before,
            max_attempts=max_attempts,
            dialect=dialect,
            include_expired_locked=False,
        )
        expired_rows = _select_ready_candidate_rows(
            limit=max(0, int(limit) - len(unlocked_rows)),
            now=now,
            expired_before=expired_before,
            max_attempts=max_attempts,
            dialect=dialect,
            include_expired_locked=True,
        )

        candidates = unlocked_rows + expired_rows
        candidates.sort(
            key=lambda row: (
                row[1] is not None,
                row[1] or datetime.min,
                row[2] or datetime.min,
                row[0],
            )
        )
        mids = [mid for mid, _next_run_at, _updated_at in candidates[: int(limit)]]
        if not mids:
            db.session.rollback()
            return {}

        db.session.execute(
            text(
                """
                UPDATE matter_status_recalc_queue
                SET locked_at = :now,
                    lock_token = :token,
                    attempts = COALESCE(attempts, 0) + 1,
                    last_error = NULL,
                    updated_at = :now
                WHERE matter_id IN :ids
                  AND (locked_at IS NULL OR locked_at < :expired_before)
                  AND (next_run_at IS NULL OR next_run_at <= :now)
                  AND COALESCE(attempts, 0) < :max_attempts
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("ids", expanding=True)),
            {
                "now": now,
                "token": token,
                "ids": mids,
                "expired_before": expired_before,
                "max_attempts": int(max_attempts),
            },
        )

        locked_rows = db.session.execute(
            text(
                """
                SELECT matter_id, payload
                FROM matter_status_recalc_queue
                WHERE matter_id IN :ids
                  AND lock_token = :token
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": mids, "token": token},
        ).all()
        locked_payloads = {str(row[0]): row[1] for row in locked_rows if row and row[0]}
        if not locked_payloads:
            db.session.rollback()
            return {}

        db.session.commit()
        return locked_payloads
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_status_recalc_queue.pick_and_lock",
            log_key="matter_status_recalc_queue.pick_and_lock",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="matter_status_recalc_queue.pick_and_lock.rollback",
                log_key="matter_status_recalc_queue.pick_and_lock.rollback",
                log_window_seconds=300,
            )
        return {}


def _lock_specific_items(
    ids: list[str],
    *,
    now: datetime,
    token: str,
    max_attempts: int,
    lock_timeout: int,
) -> dict[str, str | None]:
    if not ids:
        return {}

    expired_before = now - timedelta(seconds=int(lock_timeout))
    try:
        rows = db.session.execute(
            text(
                """
                SELECT matter_id
                FROM matter_status_recalc_queue
                WHERE matter_id IN :ids
                  AND (locked_at IS NULL OR locked_at < :expired_before)
                  AND (next_run_at IS NULL OR next_run_at <= :now)
                  AND COALESCE(attempts, 0) < :max_attempts
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("ids", expanding=True)),
            {
                "ids": ids,
                "expired_before": expired_before,
                "now": now,
                "max_attempts": int(max_attempts),
            },
        ).all()

        mids = [str(row[0]) for row in rows if row and row[0]]
        if not mids:
            db.session.rollback()
            return {}

        db.session.execute(
            text(
                """
                UPDATE matter_status_recalc_queue
                SET locked_at = :now,
                    lock_token = :token,
                    attempts = COALESCE(attempts, 0) + 1,
                    last_error = NULL,
                    updated_at = :now
                WHERE matter_id IN :mids
                  AND (locked_at IS NULL OR locked_at < :expired_before)
                  AND (next_run_at IS NULL OR next_run_at <= :now)
                  AND COALESCE(attempts, 0) < :max_attempts
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("mids", expanding=True)),
            {
                "now": now,
                "token": token,
                "mids": mids,
                "expired_before": expired_before,
                "max_attempts": int(max_attempts),
            },
        )

        locked_rows = db.session.execute(
            text(
                """
                SELECT matter_id, payload
                FROM matter_status_recalc_queue
                WHERE matter_id IN :mids
                  AND lock_token = :token
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("mids", expanding=True)),
            {"mids": mids, "token": token},
        ).all()
        locked_payloads = {str(row[0]): row[1] for row in locked_rows if row and row[0]}
        if not locked_payloads:
            db.session.rollback()
            return {}

        db.session.commit()
        return locked_payloads
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_status_recalc_queue.lock_specific",
            log_key="matter_status_recalc_queue.lock_specific",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="matter_status_recalc_queue.lock_specific.rollback",
                log_key="matter_status_recalc_queue.lock_specific.rollback",
                log_window_seconds=300,
            )
        return {}
