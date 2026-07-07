from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)


class QueueLockHeartbeat:
    """
    Best-effort lock renewal helper for DB-backed queues.

    Why:
    - Some queue workers lock a row with (locked_at, lock_token) and then run work
      that may take longer than the lock TTL.
    - If locked_at is not refreshed, other workers can treat it as stale and
      reclaim the same job -> duplicate execution.
    """

    def __init__(
        self,
        app,
        *,
        table: str,
        id_column: str | None,
        id_value: str | None,
        token_column: str,
        token_value: str,
        interval_seconds: int,
    ) -> None:
        self._app = app
        self._table = table
        self._id_column = id_column
        self._id_value = id_value
        self._token_column = token_column
        self._token_value = token_value
        self._interval_seconds = max(1, int(interval_seconds or 1))

        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return bool(self._lost.is_set())

    def start(self) -> "QueueLockHeartbeat":
        if self._thread is not None:
            return self

        def _run() -> None:
            try:
                with self._app.app_context():
                    from app.extensions import db

                    while not self._stop.wait(self._interval_seconds):
                        now = datetime.utcnow()
                        try:
                            where_parts = [f"{self._token_column} = :tokenv"]
                            params = {"now": now, "tokenv": self._token_value}
                            if self._id_column:
                                where_parts.insert(0, f"{self._id_column} = :idv")
                                params["idv"] = self._id_value
                            stmt = text(
                                f"""
                                UPDATE {self._table}
                                SET locked_at = :now,
                                    updated_at = :now
                                WHERE {' AND '.join(where_parts)}
                                """
                            )
                            res = db.session.execute(stmt, params)
                            if not res.rowcount:
                                try:
                                    db.session.rollback()
                                except Exception as rollback_exc:
                                    logger.debug(
                                        "Queue lock heartbeat rollback failed (%s.%s=%s): %s",
                                        self._table,
                                        self._id_column,
                                        self._id_value,
                                        rollback_exc,
                                        exc_info=True,
                                    )
                                self._lost.set()
                                return
                            db.session.commit()
                        except Exception:
                            try:
                                db.session.rollback()
                            except Exception as rollback_exc:
                                logger.debug(
                                    "Queue lock heartbeat rollback failed (%s.%s=%s): %s",
                                    self._table,
                                    self._id_column,
                                    self._id_value,
                                    rollback_exc,
                                    exc_info=True,
                                )
                            # If we can't heartbeat, we don't assume lock loss, but we do log it.
                            logger.exception(
                                "Queue lock heartbeat failed (%s.%s=%s)",
                                self._table,
                                self._id_column,
                                self._id_value,
                            )
            except Exception:
                # If app context cannot be created, heartbeat is disabled.
                logger.debug("QueueLockHeartbeat thread aborted", exc_info=True)

        self._thread = threading.Thread(
            target=_run,
            name=f"queue_lock_hb:{self._table}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is None:
            return
        try:
            t.join(timeout=5)
        except Exception:
            return

    def __enter__(self) -> "QueueLockHeartbeat":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
