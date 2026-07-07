from __future__ import annotations

from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, literal

from app.utils.docket_dates import normalize_date_str, parse_date

VISIBLE_FROM_BASELINE = "0001-01-01"


def normalize_visible_from(value: Any) -> str | None:
    return normalize_date_str(value)


def parse_visible_from(value: Any) -> date | None:
    return parse_date(value)


def compute_visible_from(
    due_date: date | None,
    *,
    days: int = 0,
    months: int = 0,
    years: int = 0,
) -> date | None:
    if due_date is None:
        return None
    return due_date + relativedelta(days=days, months=months, years=years)


def is_visible_by_date(item: Any, *, today: date | None = None) -> bool:
    visible_from = parse_visible_from(getattr(item, "visible_from_date", None))
    if visible_from is None:
        return True
    if today is None:
        today = date.today()
    return visible_from <= today


def visible_from_text_expr(model) -> Any:
    return func.coalesce(
        func.nullif(func.trim(model.visible_from_date), ""),
        literal(VISIBLE_FROM_BASELINE),
    )


def visible_on_or_before(model, *, target_date: date) -> Any:
    return visible_from_text_expr(model) <= target_date.isoformat()
