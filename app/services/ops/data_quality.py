from __future__ import annotations

from datetime import datetime
from typing import Optional

from dateutil.parser import parse as dateutil_parse
from dateutil.relativedelta import relativedelta
from sqlalchemy import or_

from app.extensions import db
from app.models.communication import Communication
from app.models.matter import Matter, MatterIdentifier, MatterStaffAssignment
from app.services.case.case_kind import resolve_profile_case_kind
from app.utils.error_logging import report_swallowed_exception
from app.utils.timezone import today_local


def _contains_any(text: Optional[str], keywords: list[str]) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any((kw or "").lower() in t for kw in keywords if kw)


def _is_closed_matter(m: Matter) -> bool:
    """
    Best-effort "closed" detection for dashboard hygiene.

    The Dummy/Zombie panel is meant to surface *active* matters that look like test data
    or long-unused entries. Matters that are already closed/abandoned/expired should
    not be shown here.
    """

    # Prefer explicit status fields; keep logic aligned with other "closed" filters.
    status_red = getattr(m, "status_red", None)
    status_blue = getattr(m, "status_blue", None)
    inhouse_status = getattr(m, "inhouse_status", None)

    # Localized legacy IPM + common English strings.
    closed_keywords = [
        "Matter closed",
        "Text",
        "Abandoned",
        "Withdrawn",
        "Term expired",
        "Done",
        "Closed",
        "closed",
        "abandon",
        "withdraw",
        "expired",
        "terminate",
    ]

    return (
        _contains_any(status_red, closed_keywords)
        or _contains_any(status_blue, closed_keywords)
        or _contains_any(inhouse_status, closed_keywords)
    )


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return dateutil_parse(date_str)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="data_quality.parse_date",
            log_key="data_quality.parse_date",
            log_window_seconds=300,
        )
        return None


def get_dummy_candidates(staff_party_id=None):
    """
    Identify potential dummy/zombie matters.
    Criteria (V4):
    1. Minimum age gate: retained/entered date is at least 3 months old.
    2. No application numbers.
    3. Suspected due to:
       - Test keywords in Ref/Title
       - Title is placeholder
       - Zombie: Retained > 6 months ago AND No Comms

    If staff_party_id is provided, filter by assignment.
    """

    # Base query
    q = db.session.query(Matter).filter(
        or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None))
    )

    if staff_party_id:
        q = q.join(
            MatterStaffAssignment,
            Matter.matter_id == MatterStaffAssignment.matter_id,
        ).filter(MatterStaffAssignment.staff_party_id == staff_party_id)

    matters = q.all()
    matter_ids = [
        str(getattr(m, "matter_id", "") or "").strip()
        for m in matters
        if str(getattr(m, "matter_id", "") or "").strip()
    ]
    identifiers_by_matter_id: dict[str, list[MatterIdentifier]] = {mid: [] for mid in matter_ids}
    communication_matter_ids: set[str] = set()
    if matter_ids:
        identifiers = (
            db.session.query(MatterIdentifier)
            .filter(MatterIdentifier.matter_id.in_(matter_ids))
            .all()
        )
        for ident in identifiers:
            mid = str(getattr(ident, "matter_id", "") or "").strip()
            if mid:
                identifiers_by_matter_id.setdefault(mid, []).append(ident)

        communication_matter_ids = {
            str(mid or "").strip()
            for (mid,) in (
                db.session.query(Communication.matter_id)
                .filter(Communication.matter_id.in_(matter_ids))
                .distinct()
                .all()
            )
            if str(mid or "").strip()
        }
    results = []

    # Common test keywords
    test_keywords = [
        "test",
        "sample",
        "dummy",
        "demo",
        "temp",
        "abc",
        "xxx",
        "asdf",
        "qwer",
    ]

    # Application number types
    app_no_types = [
        "APP_NO",
        "APPLICATION_NO",
        "REG_NO",
        "REGISTRATION_NO",
        "PCT_NO",
        "INTL_REG_NO",
    ]

    today = today_local()
    three_months_ago = today - relativedelta(months=3)
    six_months_ago = today - relativedelta(months=6)

    # Pre-fetch needed data to avoid N+1 if possible,
    # but for "warning" count we might want to be optimized.
    # For now, we iterate python-side for complex logic, but limit db calls.
    # To optimize, we could do this purely in SQL, but logic is complex.
    # Given dashboard load, we should limit to headers or cache results.
    # Let's try to be reasonably efficient.

    for m in matters:
        # Skip closed matters; this panel is for cleanup of suspicious *active* rows.
        if _is_closed_matter(m):
            continue

        retained_dt = parse_date(m.retained_at) or parse_date(m.entered_at)
        retained_date = retained_dt.date() if retained_dt else None

        # Keep recommendations conservative: only consider matters older than 3 months.
        if not retained_date or retained_date > three_months_ago:
            continue

        # Check Identifiers
        matter_id = str(getattr(m, "matter_id", "") or "").strip()
        identifiers = identifiers_by_matter_id.get(matter_id, [])
        has_real_number = False
        for ident in identifiers:
            id_type = (ident.id_type or "").upper()
            id_value = (ident.id_value or "").strip()
            if any(t in id_type for t in app_no_types) and len(id_value) >= 5:
                has_real_number = True
                break
            if len(id_value) >= 8 and sum(1 for c in id_value if c.isdigit()) >= 6:
                has_real_number = True
                break

        if has_real_number:
            continue

        # Check Suspicion
        reasons = []
        is_suspicious = False

        title = (m.right_name or "").lower()
        ref = (m.our_ref or "").lower()
        matter_type = (getattr(m, "matter_type", "") or "").strip().upper()
        _division, profile_type = resolve_profile_case_kind(
            getattr(m, "right_group", None),
            matter_type,
        )
        is_non_prosecution_type = profile_type in ("LITIGATION", "TRIAL", "LAWSUIT", "MISC")

        # 1. Keywords
        for kw in (kw for kw in test_keywords if (kw or "").strip()):
            if kw in title:
                reasons.append(f"Title contains '{kw}'")
                is_suspicious = True
                break
            if kw in ref:
                reasons.append(f"Ref contains '{kw}'")
                is_suspicious = True
                break

        # 2. Placeholders
        if not is_suspicious:
            clean_title = (m.right_name or "").strip()
            if not clean_title or len(clean_title) <= 2:
                reasons.append("Title too short/empty")
                is_suspicious = True
            elif clean_title in ["-", ".", "_", "--", "...", "N/A", "n/a", "None", "Unspecified"]:
                reasons.append("Title is placeholder")
                is_suspicious = True

        # 3. Zombie check
        if not is_suspicious:
            if retained_date < six_months_ago:
                # LITIGATION/MISC matters often legitimately have no "communication" rows.
                # Restrict zombie logic to prosecution-like matters to reduce false positives.
                if not is_non_prosecution_type:
                    has_comms = matter_id in communication_matter_ids
                    if not has_comms:
                        reasons.append("Old case (>6mo) with no communications")
                        is_suspicious = True

        if is_suspicious:
            results.append(
                {
                    "matter_id": m.matter_id,
                    "our_ref": m.our_ref,
                    "title": m.right_name,
                    "retained_at": m.retained_at,
                    "reasons": reasons,
                }
            )

    return results
