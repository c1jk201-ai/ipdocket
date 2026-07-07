from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.extensions import db
from app.models.matter_facts import MatterFacts
from app.models.ip_records import MatterCustomField
from app.utils.docket_dates import parse_date as _parse_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.registration_date import (
    REGISTRATION_CUSTOM_KEYS,
    REGISTRATION_EVENT_KEYS,
    find_first_date,
)

logger = logging.getLogger(__name__)

_REG_EVENT_KEYS_SQL = ", ".join([f"'{k}'" for k in REGISTRATION_EVENT_KEYS])


@dataclass(frozen=True)
class RegDateResult:
    reg_date: Optional[date]
    source: Optional[str]


def get_registration_date(matter_id: str, *, refresh: bool = False) -> date | None:
    mid = (matter_id or "").strip()
    if not mid:
        return None

    if not refresh:
        try:
            mf = MatterFacts.query.get(mid)
            if mf:
                cached_date = mf.registration_date
                if isinstance(cached_date, date):
                    return cached_date
        except Exception as exc:
            # Best-effort: fall back to recompute if cache lookup fails.
            report_swallowed_exception(
                exc,
                context="matter_facts_service.get_registration_date.cached_lookup",
                log_key="matter_facts_service.get_registration_date.cached_lookup",
                log_window_seconds=300,
            )

    res = _compute_registration_date(mid)
    if res.reg_date:
        _upsert_registration_date(mid, res.reg_date, res.source or "unknown")
    return res.reg_date


def _compute_registration_date(matter_id: str) -> RegDateResult:
    # 1) matter_event Recent Nitemsfrom    
    try:
        rows = db.session.execute(
            text(
                f"""
                SELECT event_at
                FROM matter_event
                WHERE matter_id = :mid
                  AND event_key IN ({_REG_EVENT_KEYS_SQL})
                  AND event_at IS NOT NULL
                  AND TRIM(event_at) <> ''
                ORDER BY mevent_id DESC
                LIMIT 20
                """
            ),
            {"mid": matter_id},
        ).all()
        for (raw,) in rows or []:
            dt = _parse_date(raw)
            if dt:
                return RegDateResult(dt, "matter_event")
    except Exception as e:
        logger.debug("matter_event reg_date lookup failed: %s", e)

    # 2) custom fieldsfrom    
    try:
        rows = MatterCustomField.query.filter_by(matter_id=matter_id).all()
        for r in rows:
            dt = find_first_date(r.data or {}, REGISTRATION_CUSTOM_KEYS, key_substring="Registration date")
            if dt:
                return RegDateResult(dt, f"custom_field:{r.namespace}")
    except Exception as e:
        logger.debug("custom_field reg_date lookup failed: %s", e)

    return RegDateResult(None, None)


def _upsert_registration_date(matter_id: str, reg_date: date, source: str) -> None:
    try:
        mf = MatterFacts.query.get(matter_id)
        if not mf:
            mf = MatterFacts(matter_id=matter_id)
            db.session.add(mf)
        mf.registration_date = reg_date
        mf.registration_date_source = (source or "").strip() or None
        # commit (annuity_service ) row
    except Exception as e:
        logger.debug("matter_facts upsert failed: %s", e)
