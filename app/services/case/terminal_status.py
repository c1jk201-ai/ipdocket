from __future__ import annotations

import re
from datetime import date, datetime

_TERM_EXPIRY_STATUS_LABELS = frozenset(
    {
        "termexpired",
    }
)

_TERMINAL_STATUS_EXACT: set[str] = {"done", "complete", "completed"}
_TERMINAL_STATUS_KEYWORDS: tuple[str, ...] = (
    "Matter closed",
    "Term expired",
    "Abandoned",
    "Withdrawn",
    "Transferred",
    "abandon",
    "transfer",
    "withdraw",
    "closed",
    "expired",
    "giveup",
    "forfeit",
)


def _normalize_status_label(value: object | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def parse_status_related_date(value: object | None) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith(("AUTO_CANCELLED:", "AUTO_EXPIRED:")):
        raw = raw.split(":", 1)[1].strip()
    if "T" in raw:
        raw = raw.split("T", 1)[0].strip()
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def is_terminal_case_status(status: str | None) -> bool:
    text = (status or "").strip().lower()
    if not text:
        return False
    if text in _TERMINAL_STATUS_EXACT:
        return True
    return any(keyword.lower() in text for keyword in _TERMINAL_STATUS_KEYWORDS if keyword)


def is_future_term_expiry_status(
    status: object | None,
    related_date: object | None,
    *,
    today: date | None = None,
) -> bool:
    """
    Matter.status_red may legitimately point at a future term-expiry milestone.

    That future milestone should not be treated as "the case is already closed"
    for workflow/task cleanup.
    """
    if _normalize_status_label(status) not in _TERM_EXPIRY_STATUS_LABELS:
        return False
    resolved_date = parse_status_related_date(related_date)
    if resolved_date is None:
        return False
    return resolved_date > (today or date.today())
