from __future__ import annotations

import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import bindparam

from app.extensions import db
from app.services.core.config_service import ConfigService
from app.services.ops.queue_lock_heartbeat import QueueLockHeartbeat
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)

# ---- Config defaults ----
_DEFAULT_MAX_ATTEMPTS = 8
_DEFAULT_BACKOFF_BASE_SECONDS = 30
_DEFAULT_BACKOFF_MAX_SECONDS = 3600
_DEFAULT_LOCK_TIMEOUT_SECONDS = 300


def _get_int_config(key: str, default: int) -> int:
    value = ConfigService.get_int(key, default)
    return default if value is None else value


def enqueue_annuity_matter_rebuild(matter_id: str, *, reason: str | None = None) -> None:
    """
    Upsert queue row (dedupe by matter_id). Does NOT commit.
    - If already scheduled in the future (backoff), keep it.
    - If next_run_at is due/past/null, schedule now.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    payload = json.dumps({"reason": reason}, ensure_ascii=False) if reason else None
    now = datetime.utcnow()

    try:
        stmt = text(
            """
            INSERT INTO annuity_workflow_sync_queue (
                matter_id, payload, attempts,
                next_run_at, locked_at, lock_token, last_error,
                created_at, updated_at
            ) VALUES (
                :mid, :payload, 0,
                :now, NULL, NULL, NULL,
                :now, :now
            )
            ON CONFLICT (matter_id) DO UPDATE
            SET payload = COALESCE(EXCLUDED.payload, annuity_workflow_sync_queue.payload),
                updated_at = EXCLUDED.updated_at,
                next_run_at = CASE
                    WHEN annuity_workflow_sync_queue.next_run_at IS NULL THEN EXCLUDED.updated_at
                    WHEN annuity_workflow_sync_queue.next_run_at <= EXCLUDED.updated_at THEN EXCLUDED.updated_at
                    ELSE annuity_workflow_sync_queue.next_run_at
                END
            """
        ).execution_options(policy_bypass=True)
        db.session.execute(stmt, {"mid": mid, "payload": payload, "now": now})
    except Exception as exc:
        # table missing or DB not migrated yet: fail-safe (but do not fail silently)
        msg = (str(exc) or "").lower()
        log_key = "annuity_sync_queue.enqueue"
        if "no such table" in msg or "does not exist" in msg or "undefined table" in msg:
            log_key = "annuity_sync_queue.enqueue.missing_table"
        report_swallowed_exception(
            exc,
            context="annuity_sync_queue.enqueue_annuity_matter_rebuild",
            log_key=log_key,
            log_window_seconds=300,
        )


def run_annuity_workflow_sync_task(
    payload: dict[str, Any] | None = None, **kwargs
) -> dict[str, int]:
    """
    Durable Queue adapter for annuity -> workflow sync.

    The dedicated annuity_workflow_sync_queue remains the matter-level dedupe
    and retry table during migration. Durable Queue owns process delivery; this
    adapter ensures matter rows exist, then drains only those requested matters.
    """
    data: dict[str, Any] = {}
    if isinstance(payload, dict):
        data.update(payload)
    if kwargs:
        data.update(kwargs)

    annuity_ids = {
        str(value).strip() for value in (data.get("annuity_ids") or []) if str(value or "").strip()
    }
    matter_ids = {
        str(value).strip() for value in (data.get("matter_ids") or []) if str(value or "").strip()
    }
    ensure_rows = bool(data.get("ensure") or data.get("ensure_rows"))
    refresh_registration_date = bool(data.get("refresh_registration_date"))

    if annuity_ids:
        from app.models.ip_records import AnnuityItem

        rows = (
            db.session.query(AnnuityItem.matter_id)
            .filter(AnnuityItem.annuity_id.in_(sorted(annuity_ids)))
            .distinct()
            .all()
        )
        for row in rows:
            if row and row[0]:
                matter_ids.add(str(row[0]))

    ensured = 0
    if ensure_rows and matter_ids:
        from app.services.annuity.annuity_service import ensure_annuities_for_matter

        try:
            for mid in sorted(matter_ids):
                ensured += int(
                    ensure_annuities_for_matter(
                        mid,
                        refresh_registration_date=refresh_registration_date,
                        commit=False,
                    )
                    or 0
                )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    for mid in sorted(matter_ids):
        enqueue_annuity_matter_rebuild(mid, reason="durable_queue")

    if matter_ids:
        processed = drain_annuity_sync_queue(matter_ids=sorted(matter_ids))
    else:
        processed = drain_annuity_sync_queue(limit=200)

    return {
        "processed": int(processed or 0),
        "annuity_ids": len(annuity_ids),
        "matter_ids": len(matter_ids),
        "ensured": int(ensured or 0),
    }


def drain_annuity_sync_queue(
    *,
    limit: int = 200,
    matter_ids: Optional[Iterable[str]] = None,
) -> int:
    """
    Drain durable queue. Applies:
    - attempts++ on lock
    - exponential backoff on failure
    - dead-letter when max attempts exceeded

    Returns: number of matters processed successfully.
    """
    max_attempts = _get_int_config("ANNUITY_SYNC_QUEUE_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
    base = _get_int_config("ANNUITY_SYNC_QUEUE_BACKOFF_BASE_SECONDS", _DEFAULT_BACKOFF_BASE_SECONDS)
    cap = _get_int_config("ANNUITY_SYNC_QUEUE_BACKOFF_MAX_SECONDS", _DEFAULT_BACKOFF_MAX_SECONDS)
    lock_timeout = _get_int_config(
        "ANNUITY_SYNC_QUEUE_LOCK_TIMEOUT_SECONDS", _DEFAULT_LOCK_TIMEOUT_SECONDS
    )

    now = datetime.utcnow()
    token = uuid.uuid4().hex

    processed = 0

    try:
        from flask import current_app

        app = current_app._get_current_object()
    except Exception:
        app = None

    heartbeat_interval = max(5, min(60, int(lock_timeout) // 2 if lock_timeout else 30))

    def _drain_one(mid: str) -> None:
        nonlocal processed

        try:
            # read attempts/payload (attempts already incremented on lock)
            row = db.session.execute(
                text(
                    "SELECT attempts, payload FROM annuity_workflow_sync_queue WHERE matter_id = :mid"
                ).execution_options(policy_bypass=True),
                {"mid": mid},
            ).first()
            attempts = int(row[0]) if row and row[0] is not None else 0
            payload = row[1] if row else None

            from app.services.workflow import task_sync

            hb_ctx = (
                QueueLockHeartbeat(
                    app,
                    table="annuity_workflow_sync_queue",
                    id_column="matter_id",
                    id_value=mid,
                    token_column="lock_token",
                    token_value=token,
                    interval_seconds=heartbeat_interval,
                )
                if app is not None
                else None
            )
            with hb_ctx or nullcontext():
                task_sync.sync_annuity_workflows_for_matter(mid)
                if hb_ctx is not None and hb_ctx.lost:
                    raise RuntimeError("annuity sync lock lost during processing")

            # success: remove from queue
            result = db.session.execute(
                text(
                    """
                    DELETE FROM annuity_workflow_sync_queue
                    WHERE matter_id = :mid
                      AND lock_token = :token
                    """
                ).execution_options(policy_bypass=True),
                {"mid": mid, "token": token},
            )
            if not result.rowcount:
                db.session.rollback()
                raise RuntimeError("annuity sync lock lost before finalize")
            db.session.commit()
            processed += 1

        except Exception as e:
            # failure: backoff or dead-letter
            err = (str(e) or "unknown error")[:4000]

            try:
                # refresh attempts (in case SELECT failed)
                row2 = db.session.execute(
                    text(
                        "SELECT attempts, payload FROM annuity_workflow_sync_queue WHERE matter_id = :mid"
                    ).execution_options(policy_bypass=True),
                    {"mid": mid},
                ).first()
                attempts = int(row2[0]) if row2 and row2[0] is not None else 0
                payload = row2[1] if row2 else None
            except Exception:
                attempts = 0
                payload = None

            try:
                if attempts >= max_attempts and attempts > 0:
                    _dead_letter(
                        matter_id=mid,
                        payload=payload,
                        attempts=attempts,
                        last_error=err,
                        reason="max_attempts_exceeded",
                    )
                    db.session.execute(
                        text(
                            """
                            DELETE FROM annuity_workflow_sync_queue
                            WHERE matter_id = :mid
                              AND lock_token = :token
                            """
                        ).execution_options(policy_bypass=True),
                        {"mid": mid, "token": token},
                    )
                else:
                    delay = _compute_backoff_seconds(
                        attempts=max(1, attempts),
                        base=base,
                        cap=cap,
                    )
                    next_run = datetime.utcnow() + timedelta(seconds=delay)
                    result = db.session.execute(
                        text(
                            """
                            UPDATE annuity_workflow_sync_queue
                            SET last_error = :err,
                                next_run_at = :next_run,
                                locked_at = NULL,
                                lock_token = NULL,
                                updated_at = :now
                            WHERE matter_id = :mid
                              AND lock_token = :token
                            """
                        ).execution_options(policy_bypass=True),
                        {
                            "mid": mid,
                            "token": token,
                            "err": err,
                            "next_run": next_run,
                            "now": datetime.utcnow(),
                        },
                    )
                    if not result.rowcount:
                        # Another worker may have reclaimed the lock; do not clobber.
                        db.session.rollback()
                        return

                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception as rollback_exc:
                    report_swallowed_exception(
                        rollback_exc,
                        context="annuity_sync_queue.record_failure.rollback",
                        log_key="annuity_sync_queue.record_failure.rollback",
                        log_window_seconds=300,
                    )
                logger.exception("Failed to record queue failure/backoff (mid=%s)", mid)

            logger.exception("Annuity sync drain failed (mid=%s)", mid)

    if matter_ids is not None:
        requested = [str(m).strip() for m in matter_ids if (m or "").strip()]
        for mid in requested:
            locked = _lock_specific_items(
                [mid],
                now=datetime.utcnow(),
                token=token,
                max_attempts=max_attempts,
                lock_timeout=lock_timeout,
            )
            if not locked:
                continue
            _drain_one(str(mid))
        return processed

    drained = 0
    while drained < int(limit):
        mids = _pick_and_lock_ready_items(
            limit=1,
            now=datetime.utcnow(),
            token=token,
            max_attempts=max_attempts,
            lock_timeout=lock_timeout,
        )
        if not mids:
            break
        _drain_one(str(mids[0]))
        drained += 1

    return processed


def _compute_backoff_seconds(*, attempts: int, base: int, cap: int) -> int:
    # attempts: 1-based
    try:
        exp = base * (2 ** max(0, attempts - 1))
        return int(min(cap, exp))
    except Exception:
        return int(min(cap, base))


def _pick_and_lock_ready_items(
    *,
    limit: int,
    now: datetime,
    token: str,
    max_attempts: int,
    lock_timeout: int,
) -> list[str]:
    mids: list[str] = []
    expired_before = now - timedelta(seconds=int(lock_timeout))

    try:
        dialect = getattr(db.engine.dialect, "name", "")
        if dialect == "postgresql":
            rows = db.session.execute(
                text(
                    """
                    SELECT matter_id
                    FROM annuity_workflow_sync_queue
                    WHERE (locked_at IS NULL OR locked_at < :expired_before)
                      AND (next_run_at IS NULL OR next_run_at <= :now)
                      AND COALESCE(attempts, 0) < :max_attempts
                    ORDER BY COALESCE(next_run_at, created_at) ASC, updated_at ASC
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ).execution_options(policy_bypass=True),
                {
                    "expired_before": expired_before,
                    "now": now,
                    "limit": int(limit),
                    "max_attempts": int(max_attempts),
                },
            ).all()
        else:
            rows = db.session.execute(
                text(
                    """
                    SELECT matter_id
                    FROM annuity_workflow_sync_queue
                    WHERE (locked_at IS NULL OR locked_at < :expired_before)
                      AND (next_run_at IS NULL OR next_run_at <= :now)
                      AND COALESCE(attempts, 0) < :max_attempts
                    ORDER BY COALESCE(next_run_at, created_at) ASC, updated_at ASC
                    LIMIT :limit
                    """
                ).execution_options(policy_bypass=True),
                {
                    "expired_before": expired_before,
                    "now": now,
                    "limit": int(limit),
                    "max_attempts": int(max_attempts),
                },
            ).all()

        mids = [str(r[0]) for r in rows if r and r[0]]
        if not mids:
            db.session.rollback()
            return []

        stmt = (
            text(
                """
            UPDATE annuity_workflow_sync_queue
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
            .bindparams(bindparam("ids", expanding=True))
        )
        db.session.execute(
            stmt,
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
                SELECT matter_id
                FROM annuity_workflow_sync_queue
                WHERE matter_id IN :ids
                  AND lock_token = :token
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": mids, "token": token},
        ).all()
        locked_ids = [str(r[0]) for r in locked_rows if r and r[0]]
        if not locked_ids:
            db.session.rollback()
            return []

        db.session.commit()
        return locked_ids

    except Exception as e:
        report_swallowed_exception(
            e,
            context="annuity_sync_queue.pick_and_lock",
            log_key="annuity_sync_queue.pick_and_lock",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="annuity_sync_queue.pick_and_lock.rollback",
                log_key="annuity_sync_queue.pick_and_lock.rollback",
                log_window_seconds=300,
            )
        return []


def _lock_specific_items(
    ids: list[str],
    *,
    now: datetime,
    token: str,
    max_attempts: int,
    lock_timeout: int,
) -> list[str]:
    if not ids:
        return []
    expired_before = now - timedelta(seconds=int(lock_timeout))
    try:
        # lock only those ready now (respect backoff + lock timeout)
        rows = db.session.execute(
            text(
                """
                SELECT matter_id
                FROM annuity_workflow_sync_queue
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

        mids = [str(r[0]) for r in rows if r and r[0]]
        if not mids:
            db.session.rollback()
            return []

        stmt = (
            text(
                """
            UPDATE annuity_workflow_sync_queue
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
            .bindparams(bindparam("mids", expanding=True))
        )
        db.session.execute(
            stmt,
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
                SELECT matter_id
                FROM annuity_workflow_sync_queue
                WHERE matter_id IN :mids
                  AND lock_token = :token
                """
            )
            .execution_options(policy_bypass=True)
            .bindparams(bindparam("mids", expanding=True)),
            {"mids": mids, "token": token},
        ).all()
        locked_ids = [str(r[0]) for r in locked_rows if r and r[0]]
        if not locked_ids:
            db.session.rollback()
            return []

        db.session.commit()
        return locked_ids
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_sync_queue.lock_specific",
            log_key="annuity_sync_queue.lock_specific",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="annuity_sync_queue.lock_specific.rollback",
                log_key="annuity_sync_queue.lock_specific.rollback",
                log_window_seconds=300,
            )
        return []


def _dead_letter(
    *,
    matter_id: str,
    payload: str | None,
    attempts: int,
    last_error: str,
    reason: str,
) -> None:
    try:
        db.session.execute(
            text(
                """
                INSERT INTO annuity_workflow_sync_dead_letter
                    (matter_id, payload, attempts, last_error, dead_letter_reason, dead_lettered_at, created_at)
                VALUES
                    (:mid, :payload, :attempts, :err, :reason, :now, :now)
                """
            ).execution_options(policy_bypass=True),
            {
                "mid": matter_id,
                "payload": payload,
                "attempts": int(attempts),
                "err": last_error,
                "reason": reason,
                "now": datetime.utcnow(),
            },
        )
    except Exception:
        # dead-letter itself should never break the worker
        logger.exception("Failed to write dead-letter (mid=%s)", matter_id)
