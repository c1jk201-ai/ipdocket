from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from app.utils.error_logging import report_swallowed_exception

DEFAULT_TIMEZONE = "America/New_York"


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def utcnow_iso() -> str:
    return utcnow_naive().isoformat()


def get_timezone_name(default: str = DEFAULT_TIMEZONE) -> str:
    if has_app_context():
        try:
            tzname = (current_app.config.get("TIMEZONE") or "").strip()
            if tzname:
                return tzname
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="timezone.get_timezone_name.app_config",
                log_key="timezone.get_timezone_name.app_config",
            )

    for key in ("TIMEZONE", "TZ"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return default


def normalize_timezone_name(tzname: str | None, default: str = DEFAULT_TIMEZONE) -> str:
    candidate = (tzname or "").strip() or default
    try:
        ZoneInfo(candidate)
        return candidate
    except Exception:
        return default


def apply_process_timezone(tzname: str | None = None, *, default: str = DEFAULT_TIMEZONE) -> str:
    normalized = normalize_timezone_name(tzname or get_timezone_name(default), default=default)
    os.environ["TZ"] = normalized
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="timezone.apply_process_timezone.tzset",
                log_key="timezone.apply_process_timezone.tzset",
            )
    return normalized


def now_local(tzname: str | None = None, *, default: str = DEFAULT_TIMEZONE) -> datetime:
    normalized = normalize_timezone_name(tzname or get_timezone_name(default), default=default)
    try:
        return datetime.now(ZoneInfo(normalized))
    except Exception:
        return datetime.now()


def today_local(tzname: str | None = None, *, default: str = DEFAULT_TIMEZONE) -> date:
    return now_local(tzname=tzname, default=default).date()
