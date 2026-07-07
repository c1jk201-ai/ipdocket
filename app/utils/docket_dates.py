from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.utils.error_logging import report_swallowed_exception

AUTO_CANCELLED_PREFIX = "AUTO_CANCELLED"
AUTO_EXPIRED_PREFIX = "AUTO_EXPIRED"
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_NULLISH_DATE_TOKENS = {"null", "none", "nil", "nan"}


def normalize_date_str(value: Any) -> str | None:
    """Normalize date-like inputs to YYYY-MM-DD, or None if empty/invalid."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if not s:
        return None
    s = s.strip("[](){}<>")
    if not s:
        return None
    if s.lower() in _NULLISH_DATE_TOKENS:
        return None
    s = s.replace(".", "-").replace("/", "-")
    token = s.split("T")[0].split(" ")[0].strip()
    match = _DATE_RE.search(token) or _DATE_RE.search(s)
    if not match:
        # Do not silently ignore parse failures: accumulate as a record (best-effort).
        try:
            from app.services.automation.parse_failure import record_parse_failure

            record_parse_failure(
                kind="date",
                raw_value=s,
                error="no_match",
                source="docket_dates.normalize_date_str",
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="docket_dates.normalize_date_str.record_parse_failure",
                log_key="docket_dates.record_parse_failure",
                log_window_seconds=300,
            )
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError as exc:
        try:
            from app.services.automation.parse_failure import record_parse_failure

            record_parse_failure(
                kind="date",
                raw_value=s,
                error=str(exc),
                source="docket_dates.normalize_date_str",
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="docket_dates.normalize_date_str.record_parse_failure",
                log_key="docket_dates.record_parse_failure",
                log_window_seconds=300,
            )
        return None


def normalize_done_date(value: Any, *, today: date | None = None) -> str | None:
    """Normalize done_date, enforcing AUTO_*:YYYY-MM-DD when needed."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return normalize_date_str(value)
    s = str(value).strip()
    if not s:
        return None
    upper = s.upper()
    if upper.startswith(AUTO_CANCELLED_PREFIX):
        suffix = s[len(AUTO_CANCELLED_PREFIX) :].lstrip(":").strip()
        date_str = normalize_date_str(suffix)
        if not date_str:
            date_str = (today or date.today()).isoformat()
        return f"{AUTO_CANCELLED_PREFIX}:{date_str}"
    if upper.startswith(AUTO_EXPIRED_PREFIX):
        suffix = s[len(AUTO_EXPIRED_PREFIX) :].lstrip(":").strip()
        date_str = normalize_date_str(suffix)
        if not date_str:
            date_str = (today or date.today()).isoformat()
        return f"{AUTO_EXPIRED_PREFIX}:{date_str}"
    return normalize_date_str(s)


def parse_date(value: Any) -> date | None:
    s = normalize_date_str(value)
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_date_fallback(primary: Any, secondary: Any) -> date | None:
    """Parse primary date-like value; fallback to secondary if primary invalid."""
    parsed = parse_date(primary)
    if parsed:
        return parsed
    return parse_date(secondary)


def effective_due_for_work(due_date: Any, extended_due_date: Any) -> date | None:
    """Work-planning due: prefer internal/extended due when valid, else legal due."""
    return parse_date_fallback(extended_due_date, due_date)


def effective_due_for_legal(due_date: Any, extended_due_date: Any) -> date | None:
    """Legal due: choose the later of due vs extended when both valid."""
    due = parse_date(due_date)
    ext = parse_date(extended_due_date)
    if due and ext:
        return ext if ext >= due else due
    return ext or due


def adjusted_legal_due_for_docket(due_date: Any, extended_due_date: Any) -> date | None:
    """Final legal due for a docket row.

    `extended_due_date` is historically overloaded in docket_item:
    - earlier than `due_date`: internal/work-planning due
    - later than `due_date`: paid/approved legal extension
    - present without `due_date`: internal-only user deadline

    This helper intentionally does not treat an ext-only value as legal due.
    """
    due = parse_date(due_date)
    ext = parse_date(extended_due_date)
    if due and ext and ext > due:
        return ext
    return due


def internal_due_for_docket(due_date: Any, extended_due_date: Any) -> date | None:
    """Internal/work-planning due for a docket row, excluding legal extensions."""
    ext = parse_date(extended_due_date)
    if not ext:
        return None
    due = parse_date(due_date)
    if due and ext >= due:
        return None
    return ext


def parse_done_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return parse_date(value)
    s = str(value).strip()
    if not s:
        return None
    upper = s.upper()
    if upper.startswith(AUTO_CANCELLED_PREFIX):
        s = s[len(AUTO_CANCELLED_PREFIX) :].lstrip(":").strip()
    elif upper.startswith(AUTO_EXPIRED_PREFIX):
        s = s[len(AUTO_EXPIRED_PREFIX) :].lstrip(":").strip()
    return parse_date(s)


def done_state(value: Any) -> tuple[str, str | None]:
    """Return ("pending" | "done" | "cancelled" | "expired", date_str | None)."""
    if value is None:
        return ("pending", None)
    if isinstance(value, (date, datetime)):
        return ("done", normalize_date_str(value))
    s = str(value).strip()
    if not s:
        return ("pending", None)
    upper = s.upper()
    if upper.startswith(AUTO_CANCELLED_PREFIX):
        suffix = s[len(AUTO_CANCELLED_PREFIX) :].lstrip(":").strip()
        return ("cancelled", normalize_date_str(suffix))
    if upper.startswith(AUTO_EXPIRED_PREFIX):
        suffix = s[len(AUTO_EXPIRED_PREFIX) :].lstrip(":").strip()
        return ("expired", normalize_date_str(suffix))
    date_str = normalize_date_str(s)
    if date_str:
        return ("done", date_str)
    return ("pending", None)


def is_done(value: Any) -> bool:
    return done_state(value)[0] in ("done", "cancelled", "expired")


def is_cancelled(value: Any) -> bool:
    return done_state(value)[0] == "cancelled"


def is_expired(value: Any) -> bool:
    return done_state(value)[0] == "expired"


def effective_due_text_expr(model, *, dialect_name: str | None = None):
    """SQL expression for effective due text (YYYY-MM-DD), preferring valid extended due."""
    try:
        from sqlalchemy import case, func
    except Exception:
        return None

    ext = func.nullif(func.trim(model.extended_due_date), "")
    due = func.nullif(func.trim(model.due_date), "")
    ext_token = func.substr(ext, 1, 10)
    due_token = func.substr(due, 1, 10)

    if dialect_name == "postgresql":
        ext_is_date = ext_token.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        due_is_date = due_token.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    else:
        ext_is_date = ext_token.like("____-__-__")
        due_is_date = due_token.like("____-__-__")

    return case((ext_is_date, ext_token), (due_is_date, due_token), else_=None)
