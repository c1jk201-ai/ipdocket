from __future__ import annotations

import logging

from flask import current_app

from app.utils.error_logging import report_swallowed_exception
from app.utils.external_api import is_circuit_open

logger = logging.getLogger(__name__)


def _autopause_enabled() -> bool:
    try:
        return bool(current_app.config.get("EXTERNAL_API_AUTOPAUSE_ENABLED", True))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="automation_guard._autopause_enabled",
            log_key="automation_guard._autopause_enabled",
            log_window_seconds=300,
        )
        return True


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def _open_keys(keys: list[str]) -> list[str]:
    if not keys:
        return []
    return [key for key in keys if is_circuit_open(key, include_prefix=True)]


def should_pause_email_ingestion() -> tuple[bool, list[str]]:
    if not _autopause_enabled():
        return False, []
    raw = current_app.config.get("EXTERNAL_API_AUTOPAUSE_EMAIL_KEYS", "imap,graph")
    keys = _split_keys(raw)
    open_keys = _open_keys(keys)
    return bool(open_keys), open_keys
