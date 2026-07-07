"""
Event Pipeline Service

Converts office_action and matter_event records into deadline_engine Event objects
for deadline computation.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)


# Mapping of doc_name patterns to EventType
# Order matters: first match wins
DOC_NAME_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Final rejection → FINAL_REJECTION_SERVED
    (
        re.compile(
            r"|Patent|Utility model|Design|Trademark", re.IGNORECASE
        ),
        "FINAL_REJECTION_SERVED",
    ),
    # Grant decision → GRANT_DECISION_SERVED
    (
        re.compile(r"Notice of allowance|Patent|SettingsRegistration|Registration|PatentPayment|RegistrationPayment", re.IGNORECASE),
        "GRANT_DECISION_SERVED",
    ),
    # Office action → OA_SERVED
    (
        re.compile(
            r"Notice||Notice|||Notice|"
            r"Notice|Notice|||Notice|Notice|Period|"
            r"Period(?!||)",
            re.IGNORECASE,
        ),
        "OA_SERVED",
    ),
]

# Mapping of matter_event.event_key to EventType
EVENT_KEY_TO_TYPE: dict[str, str] = {
    #  
    "": "FINAL_REJECTION_SERVED",
    "": "FINAL_REJECTION_SERVED",
    "Notice": "FINAL_REJECTION_SERVED",
    # Notice of allowance 
    "Notice of allowance": "GRANT_DECISION_SERVED",
    "Notice of allowance": "GRANT_DECISION_SERVED",
    "RegistrationPayment": "GRANT_DECISION_SERVED",
    # Standard event keys (from event_key_map)
    "REJECTION_RECEIVED_DATE": "FINAL_REJECTION_SERVED",
    "ALLOWANCE_RECEIVED_DATE": "GRANT_DECISION_SERVED",
}


def _parse_date(v) -> date | None:
    """Parse various date formats into a date object."""
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    # Remove brackets and extra chars
    s = s.strip("[](){}<>")
    # Try ISO format
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        s = m.group(1)
    # Handle dot/slash separators
    s = s.replace(".", "-").replace("/", "-")
    try:
        return date.fromisoformat(s.split("T")[0])
    except Exception:
        return None


def _map_doc_name_to_event_type(doc_name: str | None) -> str | None:
    """
    Map office_action doc_name to EventType.

    Returns EventType string value or None if no match.
    """
    if not doc_name:
        return None
    doc = doc_name.strip()
    for pattern, event_type in DOC_NAME_PATTERNS:
        if pattern.search(doc):
            return event_type
    return None


def _events_from_office_actions(matter_id: str) -> list[dict]:
    """
    Extract events from office_action table.

    Returns list of dicts with keys: event_type, event_date, specified_due, oa_id, doc_name
    """
    mid = (matter_id or "").strip()
    if not mid:
        return []

    events = []
    try:
        rows = db.session.execute(
            text(
                """
                SELECT
                  oa_id,
                  doc_name,
                  received_date,
                  due_date,
                  extended_due_date
                FROM office_action
                WHERE matter_id = :mid
                  AND (
                    received_date IS NOT NULL AND TRIM(received_date) <> ''
                  )
                ORDER BY received_date ASC
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as e:
        report_swallowed_exception(
            e,
            context=f"event_pipeline._events_from_office_actions.query(matter_id={mid})",
            log_key="event_pipeline._events_from_office_actions.query",
            log_window_seconds=300,
        )
        return []

    for row in rows:
        oa_id, doc_name, received_date, due_date, extended_due_date = row
        event_type = _map_doc_name_to_event_type(doc_name)
        if not event_type:
            continue

        event_date = _parse_date(received_date)
        if not event_date:
            continue

        # Use extended_due_date if available, else due_date as specified_due
        specified_due = _parse_date(extended_due_date) or _parse_date(due_date)

        events.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "specified_due": specified_due,
                "oa_id": oa_id,
                "doc_name": doc_name,
            }
        )

    return events


def _events_from_matter_events(matter_id: str) -> list[dict]:
    """
    Extract events from matter_event table.

    Returns list of dicts with keys: event_type, event_date
    """
    mid = (matter_id or "").strip()
    if not mid:
        return []

    events = []
    try:
        rows = db.session.execute(
            text(
                """
                SELECT
                  event_key,
                  event_at
                FROM matter_event
                WHERE matter_id = :mid
                  AND event_at IS NOT NULL
                  AND TRIM(event_at) <> ''
                ORDER BY event_at ASC, mevent_id ASC
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as e:
        report_swallowed_exception(
            e,
            context=f"event_pipeline._events_from_matter_events.query(matter_id={mid})",
            log_key="event_pipeline._events_from_matter_events.query",
            log_window_seconds=300,
        )
        return []

    for row in rows:
        event_key, event_at = row
        event_key = (event_key or "").strip()

        # Try direct mapping
        event_type = EVENT_KEY_TO_TYPE.get(event_key)

        # If no direct mapping, try pattern matching on event_key
        if not event_type:
            if "" in event_key or "Notice" in event_key:
                event_type = "FINAL_REJECTION_SERVED"
            elif "Notice of allowance" in event_key or "SettingsRegistration" in event_key:
                event_type = "GRANT_DECISION_SERVED"

        if not event_type:
            continue

        event_date = _parse_date(event_at)
        if not event_date:
            continue

        events.append(
            {
                "event_type": event_type,
                "event_date": event_date,
                "specified_due": None,
            }
        )

    return events


def build_events_for_matter(matter_id: str) -> list:
    """
    Build deadline_engine Event objects for a matter.

    Collects events from office_action and matter_event tables,
    converts them to deadline_engine.Event objects.

    Returns:
        List of deadline_engine.Event objects
    """
    try:
        from deadline_engine import Event, EventType
    except ImportError:
        logger.debug("deadline_engine module not available")
        return []

    mid = (matter_id or "").strip()
    if not mid:
        return []

    # Collect raw events from both sources
    raw_events = []
    raw_events.extend(_events_from_office_actions(mid))
    raw_events.extend(_events_from_matter_events(mid))

    if not raw_events:
        return []

    # Convert to deadline_engine Event objects
    ipm_events = []
    for ev in raw_events:
        try:
            event_type_str = ev["event_type"]
            # Convert string to EventType enum
            event_type = EventType(event_type_str)

            ipm_event = Event(
                type=event_type,
                date=ev["event_date"],
                specified_due=ev.get("specified_due"),
                meta={
                    "oa_id": ev.get("oa_id"),
                    "doc_name": ev.get("doc_name"),
                },
            )
            ipm_events.append(ipm_event)
        except (ValueError, KeyError) as e:
            logger.debug(f"Failed to create Event from {ev}: {e}")
            continue

    # Sort by date
    ipm_events.sort(key=lambda e: e.date)

    logger.debug(f"Built {len(ipm_events)} events for matter {mid}")
    return ipm_events
