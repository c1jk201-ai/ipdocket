from __future__ import annotations

from datetime import date, datetime


def parse_date(s: str | None):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_only(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_date_text(value: str | None, field: str, errors: list[str]) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    errors.append(field)
    return None
