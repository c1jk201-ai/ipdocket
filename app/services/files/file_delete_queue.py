from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import bindparam
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 10
_DEFAULT_BACKOFF_BASE_SECONDS = 60
_DEFAULT_BACKOFF_MAX_SECONDS = 3600
_DEFAULT_LOCK_TIMEOUT_SECONDS = 600


def _get_int_config(key: str, default: int) -> int:
    value = ConfigService.get_int(key, default)
    return default if value is None else value


def _compute_backoff_seconds(*, attempts: int, base: int, cap: int) -> int:
    try:
        exp = base * (2 ** max(0, attempts - 1))
        return int(min(cap, exp))
    except Exception:
        return int(min(cap, base))


def _safe_rollback(session: SASession, *, context: str) -> None:
    try:
        session.rollback()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"{context}.rollback",
            log_key=f"{context}.rollback",
            log_window_seconds=300,
        )


def enqueue_file_delete_retry(
    *,
    file_path: str,
    file_asset_id: str | None = None,
    error: Exception | str | None = None,
    session: SASession | None = None,
    commit: bool | None = None,
) -> bool:
    """
    Record a failed file delete for later retry.

    Best-effort: returns False if the queue insert/update fails.
    """
    file_path = (file_path or "").strip()
    if not file_path:
        return False

    err_text = ""
    if isinstance(error, Exception):
        err_text = f"{type(error).__name__}: {error}"
    elif error is not None:
        err_text = str(error)
    err_text = (err_text or "")[:4000] or None

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
    now = datetime.utcnow()

    def _finalize_ok() -> None:
        if commit:
            sess.commit()
        else:
            sess.flush()

    def _exec(stmt, params) -> None:
        # If we're participating in a caller transaction (commit=False), isolate failures
        # using a SAVEPOINT so we don't force a full rollback of the outer transaction.
        if commit:
            sess.execute(stmt, params)
            return
        with sess.begin_nested():
            sess.execute(stmt, params)

    try:
        _exec(
            text(
                """
                INSERT INTO file_delete_queue (
                    delete_id, file_path, file_asset_id,
                    attempts, next_run_at, locked_at, lock_token,
                    last_error, created_at, updated_at
                )
                VALUES (
                    :did, :path, :fid,
                    0, :now, NULL, NULL,
                    :err, :now, :now
                )
                """
            ),
            {
                "did": uuid.uuid4().hex,
                "path": file_path,
                "fid": (file_asset_id or None),
                "err": err_text,
                "now": now,
            },
        )
        _finalize_ok()
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.enqueue.insert",
            log_key="file_delete_queue.enqueue.insert",
            log_window_seconds=300,
        )
        if commit:
            _safe_rollback(sess, context="file_delete_queue.enqueue.insert")

    try:
        _exec(
            text(
                """
                UPDATE file_delete_queue
                SET file_asset_id = COALESCE(file_asset_id, :fid),
                last_error = :err,
                next_run_at = :now,
                updated_at = :now
                WHERE file_path = :path
                """
            ),
            {
                "path": file_path,
                "fid": (file_asset_id or None),
                "err": err_text,
                "now": now,
            },
        )
        _finalize_ok()
        return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.enqueue",
            log_key="file_delete_queue.enqueue",
            log_window_seconds=300,
        )
        if commit:
            _safe_rollback(sess, context="file_delete_queue.enqueue.update")
        return False
    finally:
        if owns_session:
            try:
                sess.close()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="file_delete_queue.session_close",
                    log_key="file_delete_queue.session_close",
                    log_window_seconds=300,
                )


def drain_file_delete_queue(*, limit: int = 200) -> dict[str, int]:
    """
    Drain and retry failed file deletes.

    Intended to be called from housekeeping.
    """
    max_attempts = _get_int_config("FILE_DELETE_QUEUE_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
    base = _get_int_config("FILE_DELETE_QUEUE_BACKOFF_BASE_SECONDS", _DEFAULT_BACKOFF_BASE_SECONDS)
    cap = _get_int_config("FILE_DELETE_QUEUE_BACKOFF_MAX_SECONDS", _DEFAULT_BACKOFF_MAX_SECONDS)
    lock_timeout = _get_int_config(
        "FILE_DELETE_QUEUE_LOCK_TIMEOUT_SECONDS", _DEFAULT_LOCK_TIMEOUT_SECONDS
    )

    now = datetime.utcnow()
    token = uuid.uuid4().hex
    jobs = _pick_and_lock_ready_jobs(
        limit=limit,
        now=now,
        token=token,
        max_attempts=max_attempts,
        lock_timeout=lock_timeout,
    )
    if not jobs:
        return {"picked": 0, "deleted": 0, "retried": 0, "failed": 0}

    deleted = 0
    retried = 0
    failed = 0

    from app.services.storage.file_asset_service import get_file_asset_service

    file_service = get_file_asset_service()

    for job in jobs:
        delete_id = str(job.get("delete_id") or "")
        file_path = str(job.get("file_path") or "")
        attempts = int(job.get("attempts") or 0)
        if not delete_id or not file_path:
            continue

        try:
            abs_path = file_service.abs_path(file_path)
        except Exception as exc:
            _mark_terminal_failure(
                delete_id,
                token=token,
                error=f"unsafe_path:{type(exc).__name__}: {exc}",
                max_attempts=max_attempts,
            )
            failed += 1
            continue

        if not abs_path.exists():
            if _delete_row(delete_id, token=token):
                deleted += 1
            continue

        try:
            ok = file_service.delete_physical_file(file_path, prune_empty=True)
        except Exception as exc:
            ok = False
            err_text = f"{type(exc).__name__}: {exc}"
        else:
            err_text = "" if ok else "delete_physical_file returned False"

        if ok:
            if _delete_row(delete_id, token=token):
                deleted += 1
            continue

        if attempts >= max_attempts:
            _mark_terminal_failure(
                delete_id, token=token, error=err_text, max_attempts=max_attempts
            )
            failed += 1
            continue

        delay = _compute_backoff_seconds(attempts=max(1, attempts), base=base, cap=cap)
        next_run = datetime.utcnow() + timedelta(seconds=delay)
        if _schedule_retry(delete_id, token=token, error=err_text, next_run_at=next_run):
            retried += 1
        else:
            failed += 1

    return {"picked": int(len(jobs)), "deleted": deleted, "retried": retried, "failed": failed}


def _pick_and_lock_ready_jobs(
    *,
    limit: int,
    now: datetime,
    token: str,
    max_attempts: int,
    lock_timeout: int,
) -> list[dict[str, object]]:
    expired_before = now - timedelta(seconds=int(lock_timeout))
    try:
        dialect = getattr(db.engine.dialect, "name", "")
        if dialect == "postgresql":
            rows = db.session.execute(
                text(
                    """
                    SELECT delete_id, file_path, attempts
                    FROM file_delete_queue
                    WHERE (locked_at IS NULL OR locked_at < :expired_before)
                      AND (next_run_at IS NULL OR next_run_at <= :now)
                      AND COALESCE(attempts, 0) < :max_attempts
                    ORDER BY COALESCE(next_run_at, created_at) ASC, updated_at ASC
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
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
                    SELECT delete_id, file_path, attempts
                    FROM file_delete_queue
                    WHERE (locked_at IS NULL OR locked_at < :expired_before)
                      AND (next_run_at IS NULL OR next_run_at <= :now)
                      AND COALESCE(attempts, 0) < :max_attempts
                    ORDER BY COALESCE(next_run_at, created_at) ASC, updated_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "expired_before": expired_before,
                    "now": now,
                    "limit": int(limit),
                    "max_attempts": int(max_attempts),
                },
            ).all()

        ids = [str(r[0]) for r in rows if r and r[0]]
        if not ids:
            db.session.rollback()
            return []

        stmt = text(
            """
            UPDATE file_delete_queue
            SET locked_at = :now,
                lock_token = :token,
                attempts = COALESCE(attempts, 0) + 1,
                updated_at = :now
            WHERE delete_id IN :ids
              AND (locked_at IS NULL OR locked_at < :expired_before)
              AND COALESCE(attempts, 0) < :max_attempts
            """
        ).bindparams(bindparam("ids", expanding=True))
        db.session.execute(
            stmt,
            {
                "now": now,
                "token": token,
                "ids": ids,
                "expired_before": expired_before,
                "max_attempts": int(max_attempts),
            },
        )

        locked_rows = db.session.execute(
            text(
                """
                SELECT delete_id, file_path, attempts
                FROM file_delete_queue
                WHERE delete_id IN :ids
                  AND lock_token = :token
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "token": token},
        ).all()
        out = []
        for r in locked_rows:
            if not r or not r[0] or not r[1]:
                continue
            out.append({"delete_id": str(r[0]), "file_path": str(r[1]), "attempts": int(r[2] or 0)})
        if not out:
            db.session.rollback()
            return []

        db.session.commit()
        return out
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.pick_and_lock",
            log_key="file_delete_queue.pick_and_lock",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="file_delete_queue.pick.rollback",
                log_key="file_delete_queue.pick.rollback",
                log_window_seconds=300,
            )
        return []


def _delete_row(delete_id: str, *, token: str) -> bool:
    try:
        res = db.session.execute(
            text(
                """
                DELETE FROM file_delete_queue
                WHERE delete_id = :did
                  AND lock_token = :token
                """
            ),
            {"did": delete_id, "token": token},
        )
        db.session.commit()
        return bool(res.rowcount)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.delete_row",
            log_key="file_delete_queue.delete_row",
            log_window_seconds=300,
        )
        _safe_rollback(db.session, context="file_delete_queue.delete_row")
        return False


def _schedule_retry(delete_id: str, *, token: str, error: str, next_run_at: datetime) -> bool:
    err = (error or "unknown error")[:4000]
    try:
        res = db.session.execute(
            text(
                """
                UPDATE file_delete_queue
                SET last_error = :err,
                    next_run_at = :next_run,
                    locked_at = NULL,
                    lock_token = NULL,
                    updated_at = :now
                WHERE delete_id = :did
                  AND lock_token = :token
                """
            ),
            {
                "did": delete_id,
                "token": token,
                "err": err,
                "next_run": next_run_at,
                "now": datetime.utcnow(),
            },
        )
        db.session.commit()
        return bool(res.rowcount)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.schedule_retry",
            log_key="file_delete_queue.schedule_retry",
            log_window_seconds=300,
        )
        _safe_rollback(db.session, context="file_delete_queue.schedule_retry")
        return False


def _mark_terminal_failure(
    delete_id: str,
    *,
    token: str,
    error: str,
    max_attempts: int,
) -> bool:
    err = (error or "unknown error")[:4000]
    try:
        res = db.session.execute(
            text(
                """
                UPDATE file_delete_queue
                SET last_error = :err,
                    next_run_at = NULL,
                    attempts = :max_attempts,
                    locked_at = NULL,
                    lock_token = NULL,
                    updated_at = :now
                WHERE delete_id = :did
                  AND lock_token = :token
                """
            ),
            {
                "did": delete_id,
                "token": token,
                "err": err,
                "max_attempts": int(max_attempts),
                "now": datetime.utcnow(),
            },
        )
        db.session.commit()
        return bool(res.rowcount)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="file_delete_queue.terminal_failure",
            log_key="file_delete_queue.terminal_failure",
            log_window_seconds=300,
        )
        _safe_rollback(db.session, context="file_delete_queue.terminal_failure")
        return False


def count_file_delete_queue(*, include_failed: bool = True) -> int:
    try:
        q = "SELECT COUNT(*) FROM file_delete_queue"
        if not include_failed:
            q += " WHERE COALESCE(attempts, 0) < :max_attempts"
            max_attempts = _get_int_config("FILE_DELETE_QUEUE_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
            return int(
                db.session.execute(text(q), {"max_attempts": int(max_attempts)}).scalar() or 0
            )
        return int(db.session.execute(text(q)).scalar() or 0)
    except Exception:
        return 0


def enqueue_file_delete_retries(
    *,
    paths: Iterable[str],
    file_asset_id: str | None = None,
    error: Exception | str | None = None,
    session: SASession | None = None,
) -> int:
    """Bulk helper for enqueue_file_delete_retry."""
    count = 0
    for p in paths:
        if enqueue_file_delete_retry(
            file_path=p,
            file_asset_id=file_asset_id,
            error=error,
            session=session,
        ):
            count += 1
    return count
