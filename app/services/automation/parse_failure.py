from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from flask import g, has_app_context, has_request_context
from flask_login import current_user

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception

try:  # pragma: no cover
    from app.models.parse_failure import ParseFailure
except Exception:  # pragma: no cover
    ParseFailure = None  # type: ignore


_SEEN_KEY = "_parse_failure_seen"
logger = logging.getLogger(__name__)


def record_parse_failure(
    *,
    kind: str,
    raw_value: Any,
    error: str | None = None,
    source: str | None = None,
    field_name: str | None = None,
    entity_type: str | None = None,
    entity_id: Any | None = None,
    normalized_value: Any | None = None,
    request_id: str | None = None,
    actor_user_id: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """
    Record parsing failures as rows, without breaking caller flow.
    - No commit here (flush-only inside SAVEPOINT).
    - Best-effort: failures in logging are swallowed.
    """
    enabled = True
    log_to_logger = True
    try:
        if has_app_context():
            from flask import current_app

            enabled = bool(current_app.config.get("PARSE_FAILURE_LOGGING_ENABLED", True))
            log_to_logger = bool(current_app.config.get("PARSE_FAILURE_LOG_TO_LOGGER", True))
    except Exception:
        enabled = True
        log_to_logger = True
    if not enabled:
        return

    # Deduplicate per-session (prevents noisy duplicates in a single request/txn)
    try:
        seen = db.session.info.get(_SEEN_KEY)
        if seen is None:
            seen = set()
            db.session.info[_SEEN_KEY] = seen
        key = (str(kind or ""), str(source or ""), str(field_name or ""), str(raw_value))
        if key in seen:
            return
        seen.add(key)
    except Exception as exc:
        # Best-effort: dedupe is advisory; keep recording even if session info is unavailable.
        report_swallowed_exception(
            exc,
            context="parse_failure.record_parse_failure.dedupe",
            log_key="parse_failure.record_parse_failure.dedupe",
            log_window_seconds=300,
        )

    if log_to_logger:
        try:
            logger.warning(
                "Parse failure: kind=%s source=%s field=%s raw=%s error=%s",
                kind,
                source,
                field_name,
                raw_value,
                error,
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="parse_failure.record_parse_failure.logger_warning",
                log_key="parse_failure.record_parse_failure.logger_warning",
                log_window_seconds=300,
            )

    if ParseFailure is None:
        return

    rid = request_id
    if not rid:
        try:
            rid = getattr(g, "request_id", None)
        except Exception:
            rid = None

    uid = actor_user_id
    if uid is None:
        try:
            if has_request_context() and current_user and current_user.is_authenticated:
                uid = int(current_user.get_id())
        except Exception:
            uid = None

    try:
        row = ParseFailure(
            kind=(str(kind or "")[:20] or "unknown"),
            source=(str(source)[:255] if source else None),
            field_name=(str(field_name)[:255] if field_name else None),
            raw_value=None if raw_value is None else str(raw_value),
            normalized_value=None if normalized_value is None else str(normalized_value),
            error=None if error is None else str(error),
            entity_type=(str(entity_type)[:64] if entity_type else None),
            entity_id=None if entity_id is None else str(entity_id),
            request_id=None if rid is None else str(rid),
            actor_user_id=uid,
            extra=(json.dumps(extra, ensure_ascii=False) if extra else None),
        )

        # SAVEPOINT so we never poison the caller transaction if table is missing etc.
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except Exception:
        # swallow
        return
