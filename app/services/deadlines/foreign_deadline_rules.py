from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from app.utils.error_logging import report_swallowed_exception

_RULES_CACHE: dict[str, tuple[float, dict]] = {}


def _load_json(path: str, cache: dict[str, tuple[float, dict]]) -> dict:
    if not path:
        return {}
    try:
        p = Path(path)
        if not p.exists():
            return {}
        mtime = p.stat().st_mtime
        cached = cache.get(str(p))
        if cached and cached[0] == mtime:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(data, dict):
            return {}
        cache[str(p)] = (mtime, data)
        return data
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"foreign_deadline_rules._load_json(path={path})",
            log_key="foreign_deadline_rules._load_json",
            log_window_seconds=300,
        )
        return {}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.split("T")[0].split(" ")[0]
    raw = raw.replace("/", "-").replace(".", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_period_to_delta(value: str) -> timedelta | tuple[int, int] | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    match = re.search(r"(\d+)", raw)
    if not match:
        return None
    amount = int(match.group(1))
    if any(token in raw for token in ("month", "months")):
        return (amount, 0)
    if any(token in raw for token in ("year", "years")):
        return (0, amount)
    if any(token in raw for token in ("week", "weeks")):
        return timedelta(weeks=amount)
    if any(token in raw for token in ("day", "days")):
        return timedelta(days=amount)
    return None


def _add_months(base: date, months: int, years: int = 0) -> date:
    total_months = base.month - 1 + months + years * 12
    year = base.year + total_months // 12
    month = total_months % 12 + 1
    day = min(
        base.day,
        [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month - 1],
    )
    return date(year, month, day)


def compute_response_deadline(
    *,
    jurisdiction: str | None,
    doc_type: str | None,
    mailing_date: str | None,
    oa_date: str | None,
    response_period: str | None,
    rules_path: str | None,
    holidays_path: str | None,
) -> tuple[str | None, str | None]:
    """
    Compute response deadline using raw calendar offsets.
    Returns (deadline_iso, reason) or (None, None) if unavailable.
    """
    rules = _load_json(rules_path or "", _RULES_CACHE)
    _ = holidays_path

    base = _parse_date(mailing_date) or _parse_date(oa_date)
    if not base:
        return None, None

    rule_list = rules.get("rules") if isinstance(rules, dict) else None
    if not isinstance(rule_list, list):
        rule_list = []

    chosen = None
    jur = (jurisdiction or "").strip().upper()
    dtype = (doc_type or "").strip().upper()
    for rule in rule_list:
        if not isinstance(rule, dict):
            continue
        rule_jur = (rule.get("jurisdiction") or "").strip().upper()
        rule_doc = (rule.get("doc_type") or "").strip().upper()
        if rule_jur and rule_jur != jur:
            continue
        if rule_doc and rule_doc not in ("*", dtype):
            continue
        chosen = rule
        break

    delta = None
    if chosen:
        months = chosen.get("response_period_months")
        days = chosen.get("response_period_days")
        years = chosen.get("response_period_years")
        if isinstance(months, int) or isinstance(years, int):
            delta = ("months", int(months or 0), int(years or 0))
        elif isinstance(days, int):
            delta = ("days", int(days))

    if not delta and response_period:
        parsed = _parse_period_to_delta(response_period)
        if isinstance(parsed, tuple):
            delta = ("months", parsed[0], parsed[1])
        elif isinstance(parsed, timedelta):
            delta = ("days", parsed.days)

    if not delta:
        return None, None

    if delta[0] == "months":
        target = _add_months(base, delta[1], delta[2])
        reason = "rule_months"
    else:
        target = base + timedelta(days=delta[1])
        reason = "rule_days"

    return target.isoformat(), reason
