from __future__ import annotations

import json
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta

from flask import has_app_context
from sqlalchemy import func, literal

from app.extensions import db
from app.services.case.case_kind import is_uspto_managed_matter, resolve_profile_case_kind
from app.services.case.terminal_status import is_future_term_expiry_status
from app.services.core.config_service import ConfigService
from app.services.matter.auto_status_rules import (
    RedRule,
    _get_red_rule_by_key,
    _get_red_rule_by_label,
    _get_red_rules,
)
from app.services.matter.status_normalization import (
    _NOTICE_SEND_NAME_REF_RE,
    _add_months,
    _add_years,
    _is_candidate_office_action_doc,
    _looks_like_non_red_document_title,
    _looks_like_non_response_notice_label,
    _looks_like_oa_response_notice,
    _looks_like_payment_notice_label,
    _looks_like_trial_pending_notice,
    _looks_like_trial_pending_response,
    _normalize_space,
    _parse_date,
    _today,
    date_only_str,
    is_internal_mgmt_non_status_red_ref,
    normalize_blue_status,
    normalize_red_status,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.status_red_visibility import is_non_action_status_red_label, is_status_red_visible

logger = logging.getLogger(__name__)


def _parse_removed_response_metadata(_raw: bytes) -> dict:
    return {}


def _no_registration_fee_deadlines(*_args, **_kwargs):
    return None, None


def _normalize_case_division(value: str | None) -> str:
    raw = _normalize_space(value or "")
    compact = raw.replace(" ", "")
    upper = raw.upper()
    if upper in ("DOM", "INC", "OUT"):
        return upper
    if upper in ("INCOMING", "INBOUND"):
        return "INC"
    if upper in ("OUTGOING", "OUTBOUND", "FOREIGN"):
        return "OUT"
    if upper in ("DOMESTIC",):
        return "DOM"
    if compact in ("Domestic", ""):
        return "DOM"
    if compact in ("", "Matter", ""):
        return "INC"
    if compact in ("Foreign", "Foreign", "", ""):
        return "OUT"
    return ""


def _infer_case_division_from_our_ref(our_ref: str | None) -> str:
    """
    Best-effort division inference based on our_ref pattern.

    Examples (from production data):
      - 25PD0159US -> code=PD -> DOM
      - 25PI0001US -> code=PI -> INC
      - 25PO0001US -> code=PO -> OUT
    """
    s = (our_ref or "").strip().upper()
    if len(s) >= 4 and s[:2].isdigit():
        code = s[2:4]
        if len(code) == 2:
            div = code[1:2]
            if div == "D":
                return "DOM"
            if div == "I":
                return "INC"
            if div == "O":
                return "OUT"
    return ""


def _infer_matter_type_from_our_ref(our_ref: str | None) -> str:
    s = (our_ref or "").strip().upper()
    if len(s) >= 4 and s[:2].isdigit():
        code = s[2:4]
        if code.startswith("P"):
            return "PATENT"
        if code.startswith("U"):
            return "UTILITY"
        if code.startswith("D"):
            return "DESIGN"
        if code.startswith("T"):
            return "TRADEMARK"
    return ""


def _get_matter_meta_for_status(matter_id: str) -> tuple[str, str, str]:
    mid = (matter_id or "").strip()
    if not mid:
        return ("", "", "")
    try:
        from app.models.ip_records import Matter
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._get_matter_meta_for_status.import_matter",
            log_key="matter_auto_status._get_matter_meta_for_status.import_matter",
            log_window_seconds=300,
        )
        return ("", "", "")
    try:
        # Use the identity map when possible so in-session updates are reflected immediately
        # (e.g. right_group changes during an edit flow). Avoid process-global caching to
        # prevent stale auto-status decisions across requests.
        obj = db.session.get(Matter, mid)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._get_matter_meta_for_status.db_get",
            log_key="matter_auto_status._get_matter_meta_for_status.db_get",
            log_window_seconds=300,
        )
        obj = None
    if not obj:
        return ("", "", "")
    return (
        (getattr(obj, "right_group", "") or "").strip(),
        (getattr(obj, "matter_type", "") or "").strip(),
        (getattr(obj, "our_ref", "") or "").strip(),
    )


@dataclass(frozen=True)
class MatterContext:
    division: str  # DOM | INC | OUT | ""
    matter_type: str  # PATENT | UTILITY | DESIGN | TRADEMARK | PCT | ...
    our_ref: str
    is_uspto: bool


_PCT_ADVISORY_STATUS_RED_LABELS = {
    "PCTPreliminary examinationDeadline",
    "Domestic Deadline 1  Notice",
    "DomesticDeadline19itemsDeadline",
}
_PCT_ADVISORY_STATUS_RED_COMPACTS = {
    label.replace(" ", "") for label in _PCT_ADVISORY_STATUS_RED_LABELS
}


def _is_pct_context(ctx: MatterContext | None) -> bool:
    if ctx is None:
        return False
    return ctx.matter_type == "PCT" or "PCT" in (ctx.our_ref or "").upper()


def _is_pct_advisory_status_red_label(value: str | None) -> bool:
    compact = _normalize_space(value or "").replace(" ", "")
    if not compact:
        return False
    return compact in _PCT_ADVISORY_STATUS_RED_COMPACTS


def _get_matter_context(matter_id: str) -> MatterContext:
    right_group, mtype_raw, our_ref = _get_matter_meta_for_status(matter_id)
    division, mtype = resolve_profile_case_kind(right_group, mtype_raw)
    division = division or _infer_case_division_from_our_ref(our_ref)
    mtype = mtype or _infer_matter_type_from_our_ref(our_ref)

    try:
        from app.models.ip_records import Matter
    except Exception:
        Matter = None

    matter_obj = db.session.get(Matter, (matter_id or "").strip()) if Matter else None
    # Auto-status should follow normalized storage/public case kind, not legacy ETC markers
    # such as MADRID/HAGUE that are displayed and operated as OUT matters.
    is_uspto = division in {"DOM", "INC"} or mtype == "PCT"
    if not is_uspto and not division:
        # Legacy: some rows have no right_group; use our_ref suffix as a weak hint.
        if (our_ref or "").strip().upper().endswith("US") and mtype != "PCT":
            is_uspto = True

    return MatterContext(
        division=division,
        matter_type=mtype,
        our_ref=(our_ref or "").strip(),
        is_uspto=is_uspto,
    )


@dataclass(frozen=True)
class AutoStatus:
    status_red: str = ""
    status_red_related_date: str = ""
    status_blue: str = ""
    display_red: str = ""
    display_blue: str = ""


_STD_EVENT_TO_RED_LABEL: dict[str, str] = {
    "APPLICATION_DEADLINE": "FilingDeadline",
    "FOREIGN_FILING_DEADLINE": "ForeignFilingDeadline",
    "EXAM_REQUEST_DEADLINE": "Examination requestDeadline",
    "APPEAL_DEADLINE": "Deadline",
    "REGISTRATION_DEADLINE": "RegistrationDeadline",
    "PENALTY_REG_DEADLINE": "RegistrationDeadline",
    "TERM_EXPIRY_DATE": "Term expired",
    "ABANDON_WITHDRAW_DATE": "Abandoned",
    "CLOSE_DATE": "Matter closed",
}

_RED_LABEL_TO_STD_EVENT: dict[str, str] = {v: k for k, v in _STD_EVENT_TO_RED_LABEL.items()}
_RAW_EVENT_TO_RED_LABEL: dict[str, str] = {
    "/Billing/ Deadline": " /Billing/Deadline",
    "Filing deadline": "FilingDeadline",
    "ForeignFilingDeadline": "ForeignFilingDeadline",
    "Examination request Due date": "Examination requestDeadline",
    "RegistrationDue date": "RegistrationDeadline",
    "RegistrationDue date": "RegistrationDeadline",
    " Period ": "Term expired",
    "Abandoned/Withdrawn": "Abandoned",
    "Done/Closed": "Matter closed",
}
_RAW_RED_LABEL_TO_EVENT_KEY: dict[str, str] = {v: k for k, v in _RAW_EVENT_TO_RED_LABEL.items()}

_RAW_EVENT_KEY_TO_STD_EVENT: dict[str, str] = {
    "APP_DATE": "APPLICATION_DATE",
    "APPLICATION_DATE": "APPLICATION_DATE",
    "Filing date": "APPLICATION_DATE",
    "Filing deadline": "APPLICATION_DEADLINE",
    "APPLICATION_DEADLINE": "APPLICATION_DEADLINE",
    "FOREIGN_FILING_DEADLINE": "FOREIGN_FILING_DEADLINE",
    "ForeignFilingDeadline": "FOREIGN_FILING_DEADLINE",
    "ForeignFiling date": "FOREIGN_FILING_DATE",
    "FOREIGN_FILING_DATE": "FOREIGN_FILING_DATE",
    "EXAM_REQ_DATE": "EXAM_REQUEST_DATE",
    "EXAM_REQUEST_DATE": "EXAM_REQUEST_DATE",
    "EXAM_REQUEST_DEADLINE": "EXAM_REQUEST_DEADLINE",
    "Examination request date": "EXAM_REQUEST_DATE",
    "Examination request Due date": "EXAM_REQUEST_DEADLINE",
    "REG_DATE": "REGISTRATION_DATE",
    "REGISTRATION_DATE": "REGISTRATION_DATE",
    "REGISTRATION_FEE_PAID": "REGISTRATION_FEE_PAID",
    "RegistrationPayment": "REGISTRATION_FEE_PAID",
    "Registration Payment": "REGISTRATION_FEE_PAID",
    "REGISTRATION_DEADLINE": "REGISTRATION_DEADLINE",
    "PENALTY_REG_DEADLINE": "PENALTY_REG_DEADLINE",
    "ALLOWANCE_DATE": "ALLOWANCE_DATE",
    "ALLOWANCE_RECEIVED_DATE": "ALLOWANCE_RECEIVED_DATE",
    "REJECTION_DATE": "REJECTION_DATE",
    "REJECTION_RECEIVED_DATE": "REJECTION_RECEIVED_DATE",
    "Notice of allowance Upload": "ALLOWANCE_RECEIVED_DATE",
    "Notice of allowance": "ALLOWANCE_DATE",
    "Notice of allowance ": "ALLOWANCE_RECEIVED_DATE",
    "Final rejection Upload": "REJECTION_RECEIVED_DATE",
    "": "REJECTION_DATE",
    "Final rejection ": "REJECTION_RECEIVED_DATE",
    "APPEAL_DEADLINE": "APPEAL_DEADLINE",
    "Registration date": "REGISTRATION_DATE",
    "RegistrationDue date": "REGISTRATION_DEADLINE",
    "RegistrationDue date": "PENALTY_REG_DEADLINE",
    " Period ": "TERM_EXPIRY_DATE",
    "Abandoned/Withdrawn": "ABANDON_WITHDRAW_DATE",
    "Done/Closed": "CLOSE_DATE",
    "TERM_EXPIRY_DATE": "TERM_EXPIRY_DATE",
    "ABANDON_WITHDRAW_DATE": "ABANDON_WITHDRAW_DATE",
    "CLOSE_DATE": "CLOSE_DATE",
}

_STD_EVENT_TO_POLICY_DEADLINE_CODE: dict[str, str] = {
    "FOREIGN_FILING_DEADLINE": "FOREIGN_FILING_PARIS",
}


# =============================================================================
#  Search    
# =============================================================================


@dataclass
class EventSummary:
    presence: set[str]
    min_dates: dict[str, date]
    max_dates: dict[str, date]
    raw_keys: set[str]


_BOOLEAN_EVENT_KEYS: set[str] = {
    # Explicit boolean-style keys (no date required).
    "EXAM_REQUESTED",
}

_CUSTOM_FIELD_EVENT_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("application_date", "APPLICATION_DATE"),
    ("filing_deadline", "APPLICATION_DEADLINE"),
    ("foreign_filing_deadline", "FOREIGN_FILING_DEADLINE"),
    ("foreign_filing_date", "FOREIGN_FILING_DATE"),
    ("exam_request_date", "EXAM_REQUEST_DATE"),
    ("exam_deadline", "EXAM_REQUEST_DEADLINE"),
    ("exam_request_deadline", "EXAM_REQUEST_DEADLINE"),
    ("reg_decision_date", "ALLOWANCE_DATE"),
    ("reg_decision_received", "ALLOWANCE_RECEIVED_DATE"),
    ("registration_date", "REGISTRATION_DATE"),
    ("reg_fee_paid_date", "REGISTRATION_FEE_PAID"),
    ("reg_deadline", "REGISTRATION_DEADLINE"),
    ("reg_penalty_deadline", "PENALTY_REG_DEADLINE"),
    ("rejection_date", "REJECTION_DATE"),
    ("rejection_received_date", "REJECTION_RECEIVED_DATE"),
    ("appeal_deadline", "APPEAL_DEADLINE"),
    ("term_expiry_date", "TERM_EXPIRY_DATE"),
    ("abandon_date", "ABANDON_WITHDRAW_DATE"),
    ("complete_date", "CLOSE_DATE"),
)


def _is_internal_filing_deadline_payload(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    raw = str(payload.get("filing_deadline_type") or "").strip().upper()
    return raw in {"INTERNAL", "INNER", "INHOUSE", "IN", "I", "Internal", "Internal deadline", "Internal"}


def _is_internal_filing_deadline_for_matter(*, matter_id: str, due_date: date | None) -> bool:
    mid = (matter_id or "").strip()
    if not mid or not due_date:
        return False

    try:
        from app.models.ip_records import MatterCustomField
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._is_internal_filing_deadline_for_matter.import_model",
            log_key="matter_auto_status._is_internal_filing_deadline_for_matter.import_model",
            log_window_seconds=300,
        )
        return False

    try:
        rows = (
            MatterCustomField.query.with_entities(MatterCustomField.data)
            .filter(MatterCustomField.matter_id == mid)
            .filter(MatterCustomField.data.isnot(None))
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._is_internal_filing_deadline_for_matter.query",
            log_key="matter_auto_status._is_internal_filing_deadline_for_matter.query",
            log_window_seconds=300,
        )
        return False

    has_internal_match = False
    has_non_internal_match = False
    for (payload,) in rows or []:
        if not isinstance(payload, dict):
            continue
        filing_due = _parse_date(payload.get("filing_deadline"))
        if filing_due != due_date:
            continue
        if _is_internal_filing_deadline_payload(payload):
            has_internal_match = True
        else:
            has_non_internal_match = True

    return has_internal_match and not has_non_internal_match


def _raw_keys_by_std_key() -> dict[str, set[str]]:
    """
    Build reverse index for raw keys that imply a given std_event_key.
    Used to reduce edge-case branching when std_event_key mapping is incomplete.
    """
    out: dict[str, set[str]] = {}
    for raw, std in _RAW_EVENT_KEY_TO_STD_EVENT.items():
        raw_k = (raw or "").strip()
        std_k = (std or "").strip()
        if not raw_k or not std_k:
            continue
        out.setdefault(std_k, set()).add(raw_k)
    return out


def _has_event(event_summary: EventSummary, std_key: str) -> bool:
    """
    Robust presence check:
    - direct std presence OR
    - any known raw key that maps to the std key.
    """
    k = (std_key or "").strip()
    if not k:
        return False
    if k in _BOOLEAN_EVENT_KEYS and k in event_summary.presence:
        return True
    if k in event_summary.min_dates or k in event_summary.max_dates:
        return True
    raw_keys = _raw_keys_by_std_key().get(k) or set()
    return bool(raw_keys and (event_summary.raw_keys & raw_keys))


def supplement_event_summary_from_payload(
    event_summary: EventSummary, payload: dict | None
) -> None:
    """Merge normalized event evidence from a MatterCustomField payload."""
    if not event_summary or not isinstance(payload, dict):
        return

    def _merge(std_key: str, dt: date) -> None:
        event_summary.presence.add(std_key)
        cur_min = event_summary.min_dates.get(std_key)
        cur_max = event_summary.max_dates.get(std_key)
        if cur_min is None or dt < cur_min:
            event_summary.min_dates[std_key] = dt
        if cur_max is None or dt > cur_max:
            event_summary.max_dates[std_key] = dt

    def _payload_supports_exam_requested_flag(payload_data: dict) -> bool:
        if not isinstance(payload_data, dict):
            return False
        if _parse_date(payload_data.get("application_date")):
            return True
        if _parse_date(payload_data.get("exam_request_date")):
            return True
        return _has_event(event_summary, "APPLICATION_DATE")

    exam_requested = str(payload.get("exam_requested") or "").strip().upper()
    if exam_requested in {"Y", "YES", "TRUE", "1", "T"} and _payload_supports_exam_requested_flag(
        payload
    ):
        event_summary.presence.add("EXAM_REQUESTED")

    def _remove_internal_filing_deadline_signal() -> None:
        internal_due = _parse_date(payload.get("filing_deadline"))
        if not internal_due:
            return
        std_key = "APPLICATION_DEADLINE"
        if event_summary.min_dates.get(std_key) == internal_due:
            event_summary.min_dates.pop(std_key, None)
        if event_summary.max_dates.get(std_key) == internal_due:
            event_summary.max_dates.pop(std_key, None)
        if std_key not in event_summary.min_dates and std_key not in event_summary.max_dates:
            event_summary.presence.discard(std_key)
            for raw_key in _raw_keys_by_std_key().get(std_key, set()):
                event_summary.raw_keys.discard(raw_key)

    internal_filing_deadline = _is_internal_filing_deadline_payload(payload)
    if internal_filing_deadline:
        _remove_internal_filing_deadline_signal()

    for field_key, std_key in _CUSTOM_FIELD_EVENT_KEY_MAP:
        if field_key == "filing_deadline" and internal_filing_deadline:
            continue
        dt = _parse_date(payload.get(field_key))
        if not dt:
            continue
        _merge(std_key, dt)
        if std_key == "EXAM_REQUEST_DATE":
            event_summary.presence.add("EXAM_REQUESTED")


_TERMINAL_STD_KEYS: tuple[str, ...] = (
    "CLOSE_DATE",
    "ABANDON_WITHDRAW_DATE",
    "TERM_EXPIRY_DATE",
)


def _is_terminal_event_reached(
    *,
    event_summary: EventSummary | None = None,
    event_presence: set[str] | None = None,
    today: date | None = None,
) -> bool:
    """
    Terminal state is reached when a terminal event date exists and is on/before today.

    Fallback behavior:
    - If only presence is available (no summary), preserve legacy behavior.
    """
    base_today = today or _today()

    if event_summary is not None:
        for key in _TERMINAL_STD_KEYS:
            dt = event_summary.min_dates.get(key) or event_summary.max_dates.get(key)
            if dt and dt <= base_today:
                return True
        # With event_summary we have parsed date context; avoid presence-only close for future dates.
        return False

    if event_presence:
        return any(key in event_presence for key in _TERMINAL_STD_KEYS)

    return False


def _best_date_str(dates: list[date], *, today: date) -> str:
    if not dates:
        return ""
    upcoming = [d for d in dates if d >= today]
    best = min(upcoming) if upcoming else max(dates)
    return best.strftime("%Y-%m-%d")


def _normalize_std_key(raw_key: str | None, std_key: str | None) -> str:
    std = (std_key or "").strip()
    if not std:
        raw = (raw_key or "").strip()
        std = _RAW_EVENT_KEY_TO_STD_EVENT.get(raw, "")
    if not std:
        raw = (raw_key or "").strip()
        if raw and raw.isupper() and "_" in raw:
            std = raw
    return std


def _fetch_event_rows(matter_id: str) -> list[tuple[str, str | None, str | None]]:
    mid = (matter_id or "").strip()
    if not mid:
        return []

    from app.models.ip_records import EventKeyMap, MatterEvent

    non_empty_event_at = MatterEvent.event_at.isnot(None) & (func.trim(MatterEvent.event_at) != "")
    # Keep no-date legacy boolean signals for exam-request completion only.
    boolean_no_date_keys = ("EXAM_REQUEST_DATE", "EXAM_REQUESTED")

    rows = (
        db.session.query(MatterEvent.event_key, EventKeyMap.std_event_key, MatterEvent.event_at)
        .outerjoin(EventKeyMap, EventKeyMap.raw_event_key == MatterEvent.event_key)
        .filter(MatterEvent.matter_id == mid)
        .filter(non_empty_event_at | MatterEvent.event_key.in_(boolean_no_date_keys))
        .all()
    )
    return rows


def _summarize_event_rows(event_rows: list[tuple[str, str | None, str | None]]) -> EventSummary:
    presence: set[str] = set()
    min_dates: dict[str, date] = {}
    max_dates: dict[str, date] = {}
    raw_keys: set[str] = set()

    for raw_key, std_key, event_at in event_rows:
        raw = (raw_key or "").strip()
        std = _normalize_std_key(raw_key, std_key)
        if not std:
            continue
        dt = _parse_date(event_at)
        # Legacy compatibility: some rows store EXAM_REQUEST_DATE without event_at.
        # Treat them as boolean completion evidence.
        if not dt:
            if std == "EXAM_REQUEST_DATE":
                presence.add("EXAM_REQUESTED")
            elif std in _BOOLEAN_EVENT_KEYS:
                presence.add(std)
            continue
        if raw:
            raw_keys.add(raw)
        presence.add(std)
        if std == "EXAM_REQUEST_DATE":
            # Date implies requested=true for completion checks.
            presence.add("EXAM_REQUESTED")
        current = min_dates.get(std)
        if current is None or dt < current:
            min_dates[std] = dt
        current_max = max_dates.get(std)
        if current_max is None or dt > current_max:
            max_dates[std] = dt

    return EventSummary(
        presence=presence,
        min_dates=min_dates,
        max_dates=max_dates,
        raw_keys=raw_keys,
    )


def _supplement_event_summary_from_custom_fields(
    matter_id: str, event_summary: EventSummary, *, ctx: MatterContext | None = None
) -> None:
    """
    Best-effort augmentation of event_summary using the primary MatterCustomField payload.

    Why:
    - Imported/migrated cases may have custom fields filled but no matter_event rows.
    - Some cases have partial matter_event rows; we still want key due dates from the form.
    - DOM/INC share the USPTO timeline; OUT relies more on explicit form-entered dates.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    try:
        from app.models.ip_records import MatterCustomField
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._supplement_event_summary_from_custom_fields.import_model",
            log_key="matter_auto_status._supplement_event_summary_from_custom_fields.import_model",
            log_window_seconds=300,
        )
        return

    try:
        rows = MatterCustomField.query.filter(MatterCustomField.matter_id == mid).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._supplement_event_summary_from_custom_fields.query",
            log_key="matter_auto_status._supplement_event_summary_from_custom_fields.query",
            log_window_seconds=300,
        )
        return

    ctx = ctx or _get_matter_context(mid)

    _EVENT_SYNC_NAMESPACE_ORDER = (
        "domestic_patent",
        "domestic_design",
        "domestic_trademark",
        "incoming_patent",
        "incoming_design",
        "incoming_trademark",
        "outgoing_patent",
        "outgoing_design",
        "outgoing_trademark",
        "pct",
        "litigation",
    )

    def _expected_namespace_for_ctx() -> str:
        mt = (ctx.matter_type or "").strip().upper()
        div = (ctx.division or "").strip().upper()

        if mt == "PCT":
            return "pct"
        if mt in ("TRIAL", "LITIGATION", "LAWSUIT"):
            return "litigation"

        is_patent_like = mt in ("PATENT", "UTILITY")
        if div == "DOM":
            if is_patent_like:
                return "domestic_patent"
            if mt == "DESIGN":
                return "domestic_design"
            if mt == "TRADEMARK":
                return "domestic_trademark"
        if div == "INC":
            if is_patent_like:
                return "incoming_patent"
            if mt == "DESIGN":
                return "incoming_design"
            if mt == "TRADEMARK":
                return "incoming_trademark"
        if div == "OUT":
            if is_patent_like:
                return "outgoing_patent"
            if mt == "DESIGN":
                return "outgoing_design"
            if mt == "TRADEMARK":
                return "outgoing_trademark"
        return ""

    data_by_ns: dict[str, dict] = {}
    for row in rows or []:
        ns = (getattr(row, "namespace", "") or "").strip()
        if not ns:
            continue
        data = row.data or {}
        if not isinstance(data, dict) or not data:
            continue
        data_by_ns[ns] = data

    if not data_by_ns:
        return

    payload: dict | None = None
    if len(data_by_ns) == 1:
        payload = next(iter(data_by_ns.values()))
    else:
        ns_hint = _expected_namespace_for_ctx()
        if ns_hint and ns_hint in data_by_ns:
            payload = data_by_ns[ns_hint]
        else:
            for ns in _EVENT_SYNC_NAMESPACE_ORDER:
                if ns in data_by_ns:
                    payload = data_by_ns[ns]
                    break
    if not isinstance(payload, dict):
        return

    supplement_event_summary_from_payload(event_summary, payload)


def _build_due_by_std_key(event_summary: EventSummary) -> dict[str, date]:
    """
    Use the latest date for deadline-like events to reflect extensions/renewals.
    """
    due_by_std_key: dict[str, date] = {}
    keys = set(event_summary.min_dates.keys()) | set(event_summary.max_dates.keys())
    for std_key in keys:
        if std_key.endswith("_DEADLINE"):
            due = event_summary.max_dates.get(std_key) or event_summary.min_dates.get(std_key)
        else:
            due = event_summary.min_dates.get(std_key) or event_summary.max_dates.get(std_key)
        if due:
            due_by_std_key[std_key] = due
    return due_by_std_key


def _policy_expire_after_days_by_std_key() -> dict[str, int]:
    try:
        from app.services.deadlines.mgmt_deadlines import _load_deadline_policies
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._policy_expire_after_days_by_std_key.import",
            log_key="matter_auto_status._policy_expire_after_days_by_std_key.import",
            log_window_seconds=300,
        )
        return {}
    try:
        policies = _load_deadline_policies()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._policy_expire_after_days_by_std_key.load",
            log_key="matter_auto_status._policy_expire_after_days_by_std_key.load",
            log_window_seconds=300,
        )
        return {}
    expire_by_code: dict[str, int] = {}
    for p in policies:
        post_due = (p.get("post_due_policy") or "").strip().upper()
        if post_due not in ("AUTO_EXPIRE", "AUTO_EXPIRE_WITH_FOLLOWUP"):
            continue
        expire_after = int(p.get("expire_after_days") or 0)
        match = p.get("match") or {}
        codes = []
        if isinstance(match, dict):
            codes = match.get("deadline_codes") or []
        codes = codes or p.get("deadline_codes") or p.get("deadline_code") or []
        if not isinstance(codes, (list, tuple, set)):
            codes = [codes]
        for code in codes:
            code_str = (str(code) or "").strip()
            if not code_str:
                continue
            if code_str not in expire_by_code or expire_after < expire_by_code[code_str]:
                expire_by_code[code_str] = expire_after
    expire_by_std: dict[str, int] = {}
    for std_key, code in _STD_EVENT_TO_POLICY_DEADLINE_CODE.items():
        if code in expire_by_code:
            expire_by_std[std_key] = expire_by_code[code]
    return expire_by_std


def _expired_deadlines_by_policy(
    event_due_by_std_key: dict[str, date],
    *,
    today: date,
) -> set[str]:
    expired: set[str] = set()
    expire_by_std = _policy_expire_after_days_by_std_key()
    if not expire_by_std:
        return expired
    for std_key, due in event_due_by_std_key.items():
        if std_key not in expire_by_std:
            continue
        if not due:
            continue
        expire_after = expire_by_std.get(std_key, 0)
        if today > (due + timedelta(days=expire_after)):
            expired.add(std_key)
    return expired


def _is_rule_applicable(
    rule: RedRule,
    event_presence: set[str],
    event_summary: EventSummary | None = None,
) -> bool:
    if not rule.activation_event_keys:
        return True
    if event_summary is None:
        return all(k in event_presence for k in rule.activation_event_keys)
    return all(_has_event(event_summary, k) for k in rule.activation_event_keys)


def _is_rule_completed(
    rule: RedRule, event_presence: set[str], event_summary: EventSummary | None = None
) -> bool:
    """
    Done items : completion_event_key  True ( Done).
    """
    if not rule.completion_event_key:
        return False
    if event_summary is None:
        completed = rule.completion_event_key in event_presence
        if not completed and rule.key in {"REGISTRATION_DEADLINE", "PENALTY_REG_DEADLINE"}:
            completed = "REGISTRATION_FEE_PAID" in event_presence
        return completed
    completed = _has_event(event_summary, rule.completion_event_key)
    if not completed and rule.key in {"REGISTRATION_DEADLINE", "PENALTY_REG_DEADLINE"}:
        completed = _has_event(event_summary, "REGISTRATION_FEE_PAID")
    return completed


def _has_live_filing_deadline_source(*, matter_id: str, due_date: date | None = None) -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False

    if _is_internal_filing_deadline_for_matter(matter_id=mid, due_date=due_date):
        return False

    due_txt = due_date.isoformat() if due_date else ""
    params = {"mid": mid, "due": due_txt}

    try:
        docket_row = db.session.execute(
            text(
                """
                SELECT 1
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND (
                    UPPER(TRIM(COALESCE(category, ''))) = 'FILING'
                    OR TRIM(COALESCE(name_ref, '')) IN (
                      'Filing',
                      'Filing (Process)',
                      'MGMT:FILING',
                      'MGMT:STATUS_RED:FilingDeadline'
                    )
                    OR REPLACE(COALESCE(name_free, ''), ' ', '') IN (
                      'FilingDeadline',
                      'FilingDeadline(Process)'
                    )
                  )
                  AND (
                    :due = ''
                    OR TRIM(COALESCE(CAST(due_date AS TEXT), '')) = :due
                  )
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            params,
        ).first()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_live_filing_deadline_source.docket_query",
            log_key="matter_auto_status._has_live_filing_deadline_source.docket_query",
            log_window_seconds=300,
        )
        docket_row = None
    if docket_row:
        return True

    try:
        workflow_row = db.session.execute(
            text(
                """
                SELECT 1
                FROM workflows
                WHERE case_id = :mid
                  AND (status IS NULL OR TRIM(status) NOT IN ('Completed', 'Abandoned'))
                  AND REPLACE(COALESCE(name, ''), ' ', '') IN ('FilingDeadline', 'Filing deadline')
                  AND (
                    :due = ''
                    OR TRIM(COALESCE(CAST(legal_due_date AS TEXT), '')) = :due
                  )
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            params,
        ).first()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_live_filing_deadline_source.workflow_query",
            log_key="matter_auto_status._has_live_filing_deadline_source.workflow_query",
            log_window_seconds=300,
        )
        workflow_row = None
    return bool(workflow_row)


def _event_context_has_application_date(
    *, event_presence: set[str] | None = None, event_summary: EventSummary | None = None
) -> bool:
    if event_summary is not None and _has_event(event_summary, "APPLICATION_DATE"):
        return True
    return bool(event_presence and "APPLICATION_DATE" in event_presence)


def _foreign_filing_priority_inactive_for_status(
    *,
    matter_id: str,
    ctx: MatterContext | None = None,
    event_presence: set[str] | None = None,
    event_summary: EventSummary | None = None,
) -> bool:
    mid = (matter_id or "").strip()
    if mid:
        try:
            from app.models.ip_records import Matter
            from app.services.deadlines.mgmt_deadlines import (
                _foreign_filing_priority_done_signal_date,
                _is_foreign_filing_priority_excluded,
                _merge_custom_fields,
            )

            matter = db.session.get(Matter, mid)
            if matter is not None:
                custom_data = _merge_custom_fields(mid)
                if _is_foreign_filing_priority_excluded(
                    matter=matter,
                    custom_data=custom_data,
                ):
                    return True
                if _foreign_filing_priority_done_signal_date(
                    matter=matter,
                    custom_data=custom_data,
                ):
                    return True
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter_auto_status.foreign_filing_priority_inactive",
                log_key="matter_auto_status.foreign_filing_priority_inactive",
                log_window_seconds=300,
            )

    if ctx is None:
        return False
    if ctx.matter_type == "PCT" or "PCT" in (ctx.our_ref or "").upper():
        return True
    if ctx.division in {"INC", "OUT"} and _event_context_has_application_date(
        event_presence=event_presence,
        event_summary=event_summary,
    ):
        return True
    return False


def _is_operational_deadline_red_candidate(
    *,
    matter_id: str,
    red_label: str,
    due_date: date | None,
    ctx: MatterContext | None = None,
    today: date | None = None,
    event_presence: set[str] | None = None,
    event_summary: EventSummary | None = None,
) -> bool:
    label = normalize_red_status(red_label)
    if not label:
        return False
    if is_non_action_status_red_label(label):
        return False

    if not is_status_red_visible(
        red_label=label,
        due_date=due_date,
        is_uspto_managed_case=bool(ctx and ctx.is_uspto),
        today=today,
    ):
        return False

    if label == "FilingDeadline":
        return _has_live_filing_deadline_source(matter_id=matter_id, due_date=due_date)

    if label == "ForeignFilingDeadline" and _foreign_filing_priority_inactive_for_status(
        matter_id=matter_id,
        ctx=ctx,
        event_presence=event_presence,
        event_summary=event_summary,
    ):
        return False

    return True


def _is_supported_current_deadline_red_candidate(
    *,
    matter_id: str,
    red_label: str,
    due_date: date | None,
    event_summary: EventSummary | None,
) -> bool:
    """
    Validate an already persisted deadline Red against the same visibility window.

    Matter.status_red is used by multiple screens and cache jobs as the canonical
    red value. Keeping an otherwise-supported deadline before its display window
    makes hidden future deadlines leak into caches and dashboards.
    """
    label = normalize_red_status(red_label)
    if not label or not is_known_deadline_red_label(label):
        return False
    ctx = _get_matter_context(matter_id) if matter_id else None
    if not is_status_red_visible(
        red_label=label,
        due_date=due_date,
        is_uspto_managed_case=bool(ctx and ctx.is_uspto),
        today=_today(),
    ):
        return False
    if label == "FilingDeadline" and not _has_live_filing_deadline_source(
        matter_id=matter_id,
        due_date=due_date,
    ):
        return False
    if label == "ForeignFilingDeadline" and _foreign_filing_priority_inactive_for_status(
        matter_id=matter_id,
        ctx=ctx,
        event_summary=event_summary,
    ):
        return False
    return _has_supporting_red_signal(
        matter_id=matter_id,
        red_label=label,
        event_summary=event_summary,
    )


def _resolve_deadline_red_due_date(
    *,
    matter_id: str,
    red_label: str,
    current_red_date: str | None,
    event_due_by_std_key: dict[str, date],
    event_rows: list[tuple[str, str | None, str]],
    today: date,
) -> date | None:
    due_dt = _parse_date(current_red_date)
    if due_dt:
        return due_dt

    inferred = _infer_red_related_date_from_event_signals(
        red_label,
        event_due_by_std_key=event_due_by_std_key,
        event_rows=event_rows,
        today=today,
    )
    due_dt = _parse_date(inferred)
    if due_dt:
        return due_dt

    inferred = _pick_open_mgmt_status_red_due_for_label(matter_id, red_label)
    return _parse_date(inferred)


def _pick_red_by_priority(
    event_presence: set[str],
    event_due_by_std_key: dict[str, date],
    *,
    matter_id: str = "",
    ctx: MatterContext | None = None,
    today: date | None = None,
    expired_deadlines: set[str] | None = None,
    event_summary: EventSummary | None = None,
) -> tuple[str, str]:
    """
    Red Select Priority ( →   → ):
    1.   In Progress  Quick due_date
    2.   In Progress   'Done ' due_date

    Returns: (label, due_date_str)
    """
    today = today or _today()

    #   (activeO & DoneX)
    interrupt_candidates: list[tuple[RedRule, date]] = []
    pipeline_candidates: list[tuple[RedRule, date]] = []
    pipeline_open_rules: list[RedRule] = []

    for rule in _get_red_rules():
        if not _is_rule_applicable(rule, event_presence, event_summary):
            continue
        if _is_rule_completed(rule, event_presence, event_summary):
            continue
        if expired_deadlines and rule.deadline_event_key in expired_deadlines:
            continue
        due = event_due_by_std_key.get(rule.deadline_event_key)
        if rule.red_class == "pipeline" and not due:
            pipeline_open_rules.append(rule)
            continue
        if not due:
            continue
        if not _is_operational_deadline_red_candidate(
            matter_id=matter_id,
            red_label=rule.label,
            due_date=due,
            ctx=ctx,
            today=today,
            event_presence=event_presence,
            event_summary=event_summary,
        ):
            continue
        if rule.red_class == "pipeline":
            pipeline_open_rules.append(rule)

        if rule.red_class == "interrupt":
            interrupt_candidates.append((rule, due))
        else:
            pipeline_candidates.append((rule, due))

    # 1)   ( Quick due)
    if interrupt_candidates:
        interrupt_candidates.sort(key=lambda x: x[1])
        rule, due = interrupt_candidates[0]
        return rule.label, due.strftime("%Y-%m-%d")

    # 2) : Current Done  Next  Select
    if pipeline_open_rules and pipeline_candidates:
        pipeline_candidates.sort(key=lambda x: (x[0].stage, x[1]))
        target_stage = min(r.stage for r in pipeline_open_rules)
        stage_candidates = [x for x in pipeline_candidates if x[0].stage == target_stage]
        if stage_candidates:
            rule, due = min(stage_candidates, key=lambda x: x[1])
            return rule.label, due.strftime("%Y-%m-%d")
        # Do not jump to later stages when an earlier pipeline stage is still open.
        return "", ""

    return "", ""


def _has_supporting_red_signal(
    *,
    matter_id: str,
    red_label: str,
    event_summary: EventSummary | None = None,
) -> bool:
    mid = (matter_id or "").strip()
    red = (red_label or "").strip()
    if not mid or not red:
        return False

    raw_key = _RAW_RED_LABEL_TO_EVENT_KEY.get(red)
    if raw_key:
        if event_summary is not None:
            # We already fetched all non-empty event_at rows for this matter.
            # Avoid extra DB round-trips and keep behavior deterministic.
            if raw_key in event_summary.raw_keys:
                return True
        else:
            from app.models.ip_records import MatterEvent

            row = (
                db.session.query(literal(1))
                .filter(MatterEvent.matter_id == mid)
                .filter(MatterEvent.event_key == raw_key)
                .filter(MatterEvent.event_at.isnot(None))
                .filter(func.trim(MatterEvent.event_at) != "")
                .first()
            )
            if row:
                return True

    std_key = _RED_LABEL_TO_STD_EVENT.get(red)
    if std_key:
        if event_summary is not None:
            # Same rationale: event_summary is complete for non-empty event_at rows.
            return _has_event(event_summary, std_key)
        from app.models.ip_records import EventKeyMap, MatterEvent

        subq = db.session.query(EventKeyMap.raw_event_key).filter(
            EventKeyMap.std_event_key == std_key
        )

        row = (
            db.session.query(literal(1))
            .filter(MatterEvent.matter_id == mid)
            .filter((MatterEvent.event_key == std_key) | (MatterEvent.event_key.in_(subq)))
            .filter(MatterEvent.event_at.isnot(None))
            .filter(func.trim(MatterEvent.event_at) != "")
            .first()
        )
        if row:
            return True

    return False


def is_known_deadline_red_label(label: str | None) -> bool:
    red = normalize_red_status(label)
    return bool(red) and (red in _RED_LABEL_TO_STD_EVENT or red in _RAW_RED_LABEL_TO_EVENT_KEY)


def has_supporting_red_signal(*, matter_id: str, red_label: str | None) -> bool:
    red = normalize_red_status(red_label)
    return _has_supporting_red_signal(matter_id=matter_id, red_label=red)


def _office_action_label_matches(doc_name: str | None, label: str | None) -> bool:
    lbl = normalize_red_status(label)
    if not lbl:
        return False
    dn = _normalize_space(doc_name or "")
    if lbl == "Notice":
        return "Notice" in dn or "Notice" in dn
    return normalize_red_status(doc_name) == lbl


def _infer_red_related_date_from_event_signals(
    red_label: str | None,
    *,
    event_due_by_std_key: dict[str, date],
    event_rows: list[tuple[str, str | None, str]],
    today: date,
) -> str:
    """
    Infer the canonical due/date for a known red label from the already-fetched event snapshot.

    This is used to:
    - backfill missing red dates
    - refresh stale cached red dates when the underlying deadline date changed
    """
    red = normalize_red_status(red_label)
    if not red:
        return ""

    rule = _get_red_rule_by_label().get(red)
    if rule:
        due = event_due_by_std_key.get(rule.deadline_event_key)
        if due:
            return due.strftime("%Y-%m-%d")

    std_key = _RED_LABEL_TO_STD_EVENT.get(red)
    if std_key:
        due = event_due_by_std_key.get(std_key)
        if due:
            return due.strftime("%Y-%m-%d")

    raw_key = _RAW_RED_LABEL_TO_EVENT_KEY.get(red)
    if raw_key:
        dts: list[date] = []
        for rk, _sk, event_at in event_rows:
            if (rk or "").strip() != raw_key:
                continue
            dt = _parse_date(event_at)
            if dt:
                dts.append(dt)
        return _best_date_str(dts, today=today)

    return ""


def _pick_open_office_action_due_for_label(matter_id: str, red_label: str) -> str:
    mid = (matter_id or "").strip()
    lbl = normalize_red_status(red_label)
    if not mid or not lbl:
        return ""

    rows = _fetch_open_office_action_rows_with_due(
        mid,
        context="matter_auto_status._pick_open_office_action_due_for_label.query",
        log_key="matter_auto_status._pick_open_office_action_due_for_label.query",
    )
    if rows is None or not rows:
        return ""

    open_oas = _build_open_office_actions(rows)
    if not open_oas:
        return ""

    signals = _fetch_response_signals(mid, include_weak_signals=False)
    handled: set[str] = set()
    if signals:
        handled, _blocked_fallback = _compute_handled_open_oa_ids(
            open_oas=open_oas, signals=signals
        )
    handled |= set(
        _superseded_open_office_action_done_dates(matter_id=mid, open_oas=open_oas).keys()
    )

    today = _today()
    parsed_candidates: list[date] = []
    raw_candidates: list[str] = []
    for oa in open_oas:
        if oa.oa_id in handled:
            continue
        if not _office_action_label_matches(oa.doc_name, lbl):
            continue
        due_raw = date_only_str(oa.due_raw)
        if due_raw:
            raw_candidates.append(due_raw)
        if oa.due_dt:
            parsed_candidates.append(oa.due_dt)

    if parsed_candidates:
        upcoming = [dt for dt in parsed_candidates if dt >= today]
        chosen = min(upcoming) if upcoming else max(parsed_candidates)
        return chosen.strftime("%Y-%m-%d")
    if raw_candidates:
        return sorted(raw_candidates)[0]
    return ""


def _pick_open_mgmt_status_red_due_for_label(matter_id: str, red_label: str) -> str:
    mid = (matter_id or "").strip()
    lbl = normalize_red_status(red_label)
    if not mid or not lbl:
        return ""

    try:
        rows = db.session.execute(
            text(
                """
                SELECT name_ref, name_free, due_date
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND name_ref IS NOT NULL
                  AND UPPER(TRIM(name_ref)) LIKE 'MGMT:STATUS_RED:%'
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND due_date IS NOT NULL
                  AND TRIM(due_date) <> ''
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._pick_open_mgmt_status_red_due_for_label.query",
            log_key="matter_auto_status._pick_open_mgmt_status_red_due_for_label.query",
            log_window_seconds=300,
        )
        return ""

    if not rows:
        return ""

    today = _today()
    ctx = _get_matter_context(mid)
    candidates: list[date] = []
    for name_ref, name_free, due_raw in rows:
        due_dt = _parse_date(due_raw)
        if not due_dt:
            continue

        label = ""
        ref = (name_ref or "").strip()
        marker = "MGMT:STATUS_RED:"
        if ref.upper().startswith(marker):
            label = normalize_red_status(ref[len(marker) :].strip())
        if not label:
            label = normalize_red_status((name_free or "").strip())
        if label != lbl:
            continue

        if not _is_operational_deadline_red_candidate(
            matter_id=mid,
            red_label=label,
            due_date=due_dt,
            ctx=ctx,
            today=today,
        ):
            continue

        candidates.append(due_dt)

    if not candidates:
        return ""

    upcoming = [dt for dt in candidates if dt >= today]
    chosen = min(upcoming) if upcoming else max(candidates)
    return chosen.strftime("%Y-%m-%d")


def _pick_open_notice_send_due_for_name_ref(matter_id: str, name_ref: str) -> str:
    mid = (matter_id or "").strip()
    ref = (name_ref or "").strip()
    if not mid or not ref or not _NOTICE_SEND_NAME_REF_RE.match(ref):
        return ""

    try:
        row = db.session.execute(
            text(
                """
                SELECT COALESCE(NULLIF(TRIM(due_date), ''), '')
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, false) = false
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND UPPER(COALESCE(TRIM(name_ref), '')) = UPPER(:name_ref)
                ORDER BY COALESCE(NULLIF(TRIM(due_date), ''), '9999-12-31'), docket_id
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid, "name_ref": ref},
        ).first()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._pick_open_notice_send_due_for_name_ref.query",
            log_key="matter_auto_status._pick_open_notice_send_due_for_name_ref.query",
            log_window_seconds=300,
        )
        return ""

    if not row:
        return ""
    return date_only_str(row[0])


def _refresh_red_related_date_from_authoritative_sources(
    *,
    matter_id: str,
    raw_red_label: str | None,
    event_due_by_std_key: dict[str, date],
    event_rows: list[tuple[str, str | None, str]],
    today: date,
) -> str:
    mid = (matter_id or "").strip()
    raw_red = (raw_red_label or "").strip()
    red = normalize_red_status(raw_red)
    if not mid or not raw_red:
        return ""

    inferred = _infer_red_related_date_from_event_signals(
        red,
        event_due_by_std_key=event_due_by_std_key,
        event_rows=event_rows,
        today=today,
    )
    if inferred:
        return inferred

    if _NOTICE_SEND_NAME_REF_RE.match(raw_red):
        inferred = _pick_open_notice_send_due_for_name_ref(mid, raw_red)
        if inferred:
            return inferred

    if _is_candidate_office_action_doc(raw_red) or _is_candidate_office_action_doc(red):
        inferred = _pick_open_office_action_due_for_label(mid, red or raw_red)
        if inferred:
            return inferred

    inferred = _pick_open_mgmt_status_red_due_for_label(mid, red or raw_red)
    if inferred:
        return inferred

    return ""


def _format_red_display(red: str, red_date: str, memo: str) -> str:
    base = (red or "").strip()
    if not base:
        return ""
    if base and red_date:
        base = f"{base}[{red_date}]"
    if base.startswith("Abandoned") and memo and "(" not in base:
        snip = _normalize_space(memo)
        if len(snip) > 40:
            snip = snip[:40] + "…"
        base = f"{base}({snip})"
    return base


def _is_manual_terminal_red_status(red: str | None) -> bool:
    value = (red or "").strip()
    if not value:
        return False
    return value.startswith("Abandoned") or value == "Matter closed"


def _suggest_blue_from_red(red: str) -> str:
    """Suggest a blue status from the current red status."""
    r = (red or "").strip()
    if not r:
        return ""
    compact = r.replace(" ", "")
    # Registration 
    if r in ("Notice of allowance", "RegistrationDeadline", "RegistrationDeadline"):
        return "RegistrationWaiting In Progress"
    # OA 
    if "Office action" in r or "Office action" in r:
        return "   In Progress"
    if "Notice" in r:
        return "Filing  In Progress"
    if ("Publicationdecision" in compact) or (compact in {"Publication", "PublicationIn Progress"}):
        return "Filing Publication In Progress"
    # Examination request
    if r == "Examination requestDeadline":
        return "Examination  Billing In Progress"
    if r == "ExaminationOpen":
        return "Examination In progress"
    # 
    if r == "Deadline":
        return "   In Progress"
    # ForeignFiling ( Add)
    if r == "ForeignFilingDeadline":
        return "ForeignFiling  In Progress"
    # Renewal
    if "Renewal" in r:
        return "Renewal In Progress"
    # 
    if r.startswith("Abandoned") or r in ("Matter closed", "Term expired"):
        return "Matter closed"
    # FilingDeadline (Default)
    if r == "FilingDeadline":
        return "Filing  In Progress"
    # Other Deadline 
    if r.endswith("Deadline") or r in ("Applicant  Notice(Period)",):
        return "Filing  In Progress"
    return ""


def _collect_post_filing_pending_deadlines(
    event_presence: set[str],
    event_due_by_std_key: dict[str, date],
    *,
    matter_id: str = "",
    ctx: MatterContext | None = None,
    today: date | None = None,
    event_summary: EventSummary | None = None,
    fallback_due_by_std_key: dict[str, date] | None = None,
    expired_deadlines: set[str] | None = None,
) -> list[tuple[str, date]]:
    """
    Collect "post-filing" deadlines that should remain visible/managed together.

    This is specifically to avoid hiding 'Examination requestDeadline' behind 'ForeignFilingDeadline' when
    both are pending after filing.
    """
    if not event_presence:
        return []
    if today is None:
        today = _today()

    # Terminal / end states
    if _is_terminal_event_reached(
        event_summary=event_summary,
        event_presence=event_presence,
    ):
        return []
    if event_summary is not None:
        if _has_event(event_summary, "REGISTRATION_DATE") or _has_event(
            event_summary,
            "REGISTRATION_FEE_PAID",
        ):
            return []
    elif event_presence and (
        "REGISTRATION_DATE" in event_presence or "REGISTRATION_FEE_PAID" in event_presence
    ):
        return []

    # Only meaningful after filing.
    if event_summary is not None:
        if not _has_event(event_summary, "APPLICATION_DATE"):
            return []
    elif "APPLICATION_DATE" not in event_presence:
        return []

    order: list[tuple[str, str]] = [
        ("FOREIGN_FILING_DEADLINE", "FOREIGN_FILING_DATE"),
        ("EXAM_REQUEST_DEADLINE", "EXAM_REQUESTED"),
    ]

    out: list[tuple[str, date]] = []
    for deadline_key, completion_key in order:
        if event_summary is not None:
            if _has_event(event_summary, completion_key):
                continue
        elif completion_key in event_presence:
            continue
        if expired_deadlines and deadline_key in expired_deadlines:
            continue
        due = event_due_by_std_key.get(deadline_key) or (fallback_due_by_std_key or {}).get(
            deadline_key
        )
        if not due:
            continue
        label = _STD_EVENT_TO_RED_LABEL.get(deadline_key) or ""
        if not label:
            continue
        if label == "ForeignFilingDeadline" and _foreign_filing_priority_inactive_for_status(
            matter_id=matter_id,
            ctx=ctx,
            event_presence=event_presence,
            event_summary=event_summary,
        ):
            continue
        out.append((label, due))
    return out


_STRONG_BLUE_STATES: set[str] = {"OA  In Progress", "RegistrationWaiting In Progress", "RegistrationDone", "Matter closed"}
_PRIMARY_BLUE_STATES: set[str] = {"Filing Examination In Progress", "Filing Publication In Progress", ""}
_EVIDENCE_REQUIRED_BLUE_STATES: set[str] = {
    "Filing Examination In Progress",
    "Examination  Billing In Progress",
    "ForeignFiling  In Progress",
    "ForeignFiling In Progress(ExaminationBilling)",
    "Filing Publication In Progress",
}


def is_evidence_required_blue_status(value: str | None) -> bool:
    """Return True when a blue status should only be kept with event/deadline evidence."""
    return normalize_blue_status(value) in _EVIDENCE_REQUIRED_BLUE_STATES


def _merge_blue_with_pending_post_filing(
    base_blue: str,
    pending_post_filing: list[tuple[str, date]],
    *,
    preserve_primary_blue: bool = False,
) -> str:
    display_blue = normalize_blue_status(base_blue)
    parts: list[str] = []
    for lbl, _due in pending_post_filing:
        if lbl == "ForeignFilingDeadline":
            parts.append("ForeignFiling  In Progress")
        elif lbl == "Examination requestDeadline":
            parts.append("Examination  Billing In Progress")
    parts = list(dict.fromkeys(parts))

    if preserve_primary_blue and display_blue in _PRIMARY_BLUE_STATES:
        merged_parts = [display_blue] + [part for part in parts if part != display_blue]
        return " · ".join(merged_parts) if merged_parts else display_blue

    has_foreign_filing_pending = "ForeignFiling  In Progress" in parts
    has_exam_request_pending = "Examination  Billing In Progress" in parts
    if (
        has_foreign_filing_pending
        and has_exam_request_pending
        and display_blue not in _STRONG_BLUE_STATES
    ):
        if (
            (not display_blue)
            or (display_blue in parts)
            or (display_blue in ("Filing Examination In Progress", "Filing  In Progress", "ForeignFiling In Progress(ExaminationBilling)"))
        ):
            return "ForeignFiling In Progress(ExaminationBilling)"

    if len(parts) >= 2 and display_blue not in _STRONG_BLUE_STATES:
        if (
            (not display_blue)
            or (display_blue in parts)
            or (display_blue in ("Filing Examination In Progress", "Filing  In Progress"))
        ):
            return " · ".join(parts)
    if len(parts) == 1 and (not display_blue or display_blue in ("Filing Examination In Progress", "Filing  In Progress")):
        return parts[0]
    return display_blue


def _derive_blue_from_events(
    matter_id: str,
    *,
    event_presence: set[str] | None = None,
    event_due_by_std_key: dict[str, date] | None = None,
    ctx: MatterContext | None = None,
    event_summary: EventSummary | None = None,
    expired_deadlines: set[str] | None = None,
) -> str:
    """  Blue Status  (ForeignFiling  )"""
    mid = (matter_id or "").strip()
    if not mid:
        return ""

    if event_presence is None and event_summary is None:
        rows = _fetch_event_rows(mid)
        event_summary = _summarize_event_rows(rows)
    if event_summary is not None:
        if event_presence is None:
            event_presence = set(event_summary.presence)
        else:
            event_presence = set(event_presence) | set(event_summary.presence)
    if event_presence is None:
        event_presence = set()
    if event_due_by_std_key is None and event_summary is not None:
        event_due_by_std_key = _build_due_by_std_key(event_summary)
    if event_due_by_std_key is None:
        event_due_by_std_key = {}
    today = _today()
    if expired_deadlines is None:
        expired_deadlines = _expired_deadlines_by_policy(event_due_by_std_key, today=today)

    def _has(k: str) -> bool:
        if event_summary is not None and _has_event(event_summary, k):
            return True
        return k in event_presence

    # Terminal / end states ()
    if _is_terminal_event_reached(
        event_summary=event_summary,
        event_presence=event_presence,
    ):
        return "Matter closed"

    # Registration-related
    if _has("REGISTRATION_DATE") or _has("REGISTRATION_FEE_PAID"):
        return "RegistrationDone"
    if (
        _has("ALLOWANCE_DATE")
        or _has("ALLOWANCE_RECEIVED_DATE")
        or _has("REGISTRATION_DEADLINE")
        or _has("PENALTY_REG_DEADLINE")
    ):
        return "RegistrationWaiting In Progress"

    # Rejection / appeal
    if _has("REJECTION_DATE") or _has("REJECTION_RECEIVED_DATE"):
        return "   In Progress" if _has("APPEAL_DEADLINE") else "Filing Examination In Progress"

    # Filing Done  
    if _has("APPLICATION_DATE"):
        # Examination Open Filing Done   .
        if _has("EXAM_REQUEST_DATE") or _has("EXAM_REQUESTED"):
            return "Filing Examination In Progress"
        # ForeignFiling  In Progress: FilingO, ForeignFilingDeadlineO, ForeignFiling dateX
        if _has("FOREIGN_FILING_DEADLINE") and not _has("FOREIGN_FILING_DATE"):
            if expired_deadlines and "FOREIGN_FILING_DEADLINE" in expired_deadlines:
                pass
            elif not _is_operational_deadline_red_candidate(
                matter_id=mid,
                red_label="ForeignFilingDeadline",
                due_date=event_due_by_std_key.get("FOREIGN_FILING_DEADLINE"),
                ctx=ctx,
                today=today,
                event_presence=event_presence,
                event_summary=event_summary,
            ):
                pass
            else:
                return "ForeignFiling  In Progress"
        # Examination  Billing In Progress: FilingO, Examination requestDeadlineO, Examination request dateX
        if _has("EXAM_REQUEST_DEADLINE"):
            if expired_deadlines and "EXAM_REQUEST_DEADLINE" in expired_deadlines:
                pass
            elif not _is_operational_deadline_red_candidate(
                matter_id=mid,
                red_label="Examination requestDeadline",
                due_date=event_due_by_std_key.get("EXAM_REQUEST_DEADLINE"),
                ctx=ctx,
                today=today,
                event_presence=event_presence,
                event_summary=event_summary,
            ):
                pass
            else:
                return "Examination  Billing In Progress"
        #  : Filing Examination In Progress
        return "Filing Examination In Progress"

    return ""


def _default_blue_for_matter(matter_id: str) -> str:
    mid = (matter_id or "").strip()
    if not mid:
        return ""
    from app.models.ip_records import Matter

    try:
        row = (
            Matter.query.filter_by(matter_id=mid)
            .with_entities(
                func.coalesce(func.trim(Matter.matter_type), ""),
                func.coalesce(func.trim(Matter.right_group), ""),
                func.coalesce(func.trim(Matter.our_ref), ""),
            )
            .first()
        )
    except RuntimeError:
        return ""
    if not row:
        return ""
    matter_type_raw = (row[0] or "").strip()
    right_group_raw = (row[1] or "").strip()
    our_ref = (row[2] or "").strip().upper()
    _division, matter_type = resolve_profile_case_kind(right_group_raw, matter_type_raw)

    # Some imported rows may not have `matter_type` populated yet.
    # Heuristic: Our Ref. = YY + (Type)(Division)...
    # - P? => PATENT
    # - T? => TRADEMARK
    # - D? => DESIGN
    if not matter_type and len(our_ref) >= 4 and our_ref[:2].isdigit():
        code = our_ref[2:4]
        t = code[:1]
        if t == "P":
            matter_type = "PATENT"
        elif t == "U":
            matter_type = "UTILITY"
        elif t == "T":
            matter_type = "TRADEMARK"
        elif t == "D":
            matter_type = "DESIGN"

    if matter_type in ("PATENT", "UTILITY", "DESIGN", "TRADEMARK"):
        return "Filing  In Progress"
    if matter_type in ("TRIAL", "LITIGATION", "LAWSUIT"):
        return "Matter In Progress"
    return ""


@dataclass(frozen=True)
class _ResponseSignal:
    dt: date
    dispatch_digits: str
    kind: str | None = None


def _digits_only(value: str | None) -> str:
    return re.sub(r"[^0-9]", "", (value or "").strip())


def _infer_response_kind(doc_type: str | None) -> str | None:
    s = _normalize_space(doc_type or "")
    if not s:
        return None
    if any(token in s for token in ("", "", "")):
        return "correction"
    if any(token in s for token in ("", "", "")):
        return "opinion"
    return None


def _infer_return_notice_expected_kinds(doc_name: str | None) -> set[str]:
    s = _normalize_space(doc_name or "")
    if "" in s:
        return {"opinion"}
    if "" in s:
        return {"correction"}
    # Ambiguous (imported data often stores a generic label). Allow both.
    return {"opinion", "correction"}


def _fetch_non_email_response_rows(matter_id: str) -> list[tuple[str, str | None]]:
    """
    Fetch response(comm_type='R') rows, excluding emails.

    Returns:
      list[(dt_raw, note)]
    """
    mid = (matter_id or "").strip()
    if not mid:
        return []

    try:
        return db.session.execute(
            text(
                """
                    SELECT
                      COALESCE(
                        NULLIF(TRIM(c.sent_date), ''),
                        NULLIF(TRIM(c.received_date), ''),
                        NULLIF(TRIM(c.done_date), '')
                      ) AS dt,
                      c.note
                    FROM communication c
                    WHERE c.matter_id = :mid
                      AND c.comm_type = 'R'
                      AND (
                        (c.sent_date IS NOT NULL AND TRIM(c.sent_date) <> '')
                        OR (c.received_date IS NOT NULL AND TRIM(c.received_date) <> '')
                        OR (c.done_date IS NOT NULL AND TRIM(c.done_date) <> '')
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM communication_file_asset cf2
                        JOIN file_asset fa2 ON fa2.file_asset_id = cf2.file_asset_id
                        WHERE cf2.comm_id = c.comm_id
                          AND COALESCE(cf2.is_deleted, false) = false
                          AND (
                            LOWER(COALESCE(fa2.original_name, '')) LIKE '%.eml'
                            OR LOWER(COALESCE(fa2.original_name, '')) LIKE '%.msg'
                            OR LOWER(COALESCE(fa2.mime_type, '')) IN ('message/rfc822', 'application/vnd.ms-outlook')
                            OR LOWER(COALESCE(fa2.file_path, '')) LIKE 'emails/%'
                          )
                      )
                    """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_non_email_response_rows",
            log_key="matter_auto_status._fetch_non_email_response_rows",
            log_window_seconds=300,
        )
        return []


def _fetch_response_signals(
    matter_id: str, *, include_weak_signals: bool = True
) -> list[_ResponseSignal]:
    """
    Fetch response(communication.comm_type='R') signals from attached .txt files.

    We rely on KEAPS document field 'Applicant   Send' as the linkage key.
    """
    mid = (matter_id or "").strip()
    if not mid or not has_app_context():
        return []

    try:
        from app.services.storage.file_asset_service import get_file_asset_service
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_response_signals.imports",
            log_key="matter_auto_status._fetch_response_signals.imports",
            log_window_seconds=300,
        )
        return []

    try:
        rows = db.session.execute(
            text(
                """
                SELECT
                  COALESCE(
                    NULLIF(TRIM(c.sent_date), ''),
                    NULLIF(TRIM(c.received_date), ''),
                    NULLIF(TRIM(c.done_date), '')
                  ) AS dt,
                  fa.file_path
                FROM communication c
                JOIN communication_file_asset cfa
                  ON cfa.comm_id = c.comm_id
                 AND COALESCE(cfa.is_deleted, false) = false
                JOIN file_asset fa
                  ON fa.file_asset_id = cfa.file_asset_id
                WHERE c.matter_id = :mid
                  AND c.comm_type = 'R'
                  AND (
                    LOWER(COALESCE(fa.original_name, '')) LIKE '%.txt'
                    OR LOWER(COALESCE(fa.file_path, '')) LIKE '%.txt'
                  )
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_response_signals.query",
            log_key="matter_auto_status._fetch_response_signals.query",
            log_window_seconds=300,
        )
        return []

    max_bytes = 5 * 1024 * 1024
    try:
        from flask import current_app

        raw = current_app.config.get("document_MAX_PARSE_BYTES")
        if raw not in (None, ""):
            max_bytes = int(raw)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_response_signals.max_bytes",
            log_window_seconds=300,
        )

    skip_document_parse = max_bytes <= 0

    file_service = None
    try:
        file_service = get_file_asset_service()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_response_signals.get_file_asset_service",
            log_key="matter_auto_status._fetch_response_signals.get_file_asset_service",
            log_window_seconds=300,
        )
        file_service = None

    out: list[_ResponseSignal] = []
    seen: set[tuple[date, str, str]] = set()
    if not skip_document_parse:
        for raw_dt, file_path in rows:
            dt = _parse_date(raw_dt)
            if not dt:
                continue
            path = (file_path or "").strip()
            if not path or not file_service:
                continue
            try:
                abs_path = file_service.abs_path(path)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="matter_auto_status._fetch_response_signals.abs_path",
                    log_window_seconds=300,
                )
                continue

            try:
                if not abs_path.exists():
                    logger.debug(
                        "Skipping missing response document file during auto-status scan "
                        "(matter_id=%s, path=%s)",
                        mid,
                        path,
                    )
                    continue
                if max_bytes and abs_path.stat().st_size > max_bytes:
                    continue
                raw = abs_path.read_bytes()
                if max_bytes and len(raw) > max_bytes:
                    raw = raw[:max_bytes]
            except FileNotFoundError:
                logger.debug(
                    "Skipping missing response document file during auto-status scan "
                    "(matter_id=%s, path=%s)",
                    mid,
                    path,
                )
                continue
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="matter_auto_status._fetch_response_signals.read_bytes",
                    log_window_seconds=300,
                )
                continue

            try:
                document = _parse_removed_response_metadata(raw)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="matter_auto_status._fetch_response_signals.parse_document",
                    log_window_seconds=300,
                )
                continue

            dispatch_digits = _digits_only(document.get("dispatch_no"))
            kind = _infer_response_kind(document.get("doc_type"))
            key = (dt, dispatch_digits, kind or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(_ResponseSignal(dt=dt, dispatch_digits=dispatch_digits, kind=kind))

    if include_weak_signals:
        # Add weaker signals from communication rows (no dispatch number).
        for raw_dt, note in _fetch_non_email_response_rows(mid):
            dt = _parse_date(raw_dt)
            if not dt:
                continue
            kind = _infer_response_kind(note)
            key = (dt, "", kind or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(_ResponseSignal(dt=dt, dispatch_digits="", kind=kind))

    out.sort(key=lambda x: (x.dt, x.dispatch_digits, x.kind or ""))
    return out


def _has_litigation_pending_signal(
    matter_id: str,
    *,
    ctx: MatterContext | None = None,
    event_summary: EventSummary | None = None,
) -> bool:
    mid = (matter_id or "").strip()
    if not mid or not has_app_context():
        return False

    matter_ctx = ctx or _get_matter_context(mid)
    if matter_ctx.matter_type not in ("TRIAL", "LITIGATION", "LAWSUIT"):
        return False

    if event_summary is not None:
        if (
            "CLOSE_DATE" in event_summary.presence
            or "ABANDON_WITHDRAW_DATE" in event_summary.presence
        ):
            return False
        if "// " in event_summary.raw_keys:
            return False

    try:
        from app.models.communication import Communication, OfficeAction
        from app.models.ip_records import MatterCustomField
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_litigation_pending_signal.imports",
            log_key="matter_auto_status._has_litigation_pending_signal.imports",
            log_window_seconds=300,
        )
        return False

    try:
        row = MatterCustomField.query.filter_by(matter_id=mid, namespace="litigation").first()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_litigation_pending_signal.custom_field_query",
            log_key="matter_auto_status._has_litigation_pending_signal.custom_field_query",
            log_window_seconds=300,
        )
        row = None

    data = row.data if row and isinstance(row.data, dict) else {}
    if data:
        # Once a decision/termination is recorded, "" should not win over later stages.
        if any(
            date_only_str(data.get(key))
            for key in ("decision_date", "complete_date", "abandon_date")
        ):
            return False
        if date_only_str(data.get("request_date")):
            return True
        if (data.get("case_no") or "").strip():
            return True

    try:
        notice_rows = (
            OfficeAction.query.with_entities(OfficeAction.doc_name)
            .filter(OfficeAction.matter_id == mid)
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_litigation_pending_signal.office_action_query",
            log_key="matter_auto_status._has_litigation_pending_signal.office_action_query",
            log_window_seconds=300,
        )
        notice_rows = []

    for (doc_name,) in notice_rows:
        if _looks_like_trial_pending_notice(doc_name):
            return True

    try:
        response_rows = (
            Communication.query.with_entities(Communication.note)
            .filter(Communication.matter_id == mid, Communication.comm_type == "R")
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._has_litigation_pending_signal.response_query",
            log_key="matter_auto_status._has_litigation_pending_signal.response_query",
            log_window_seconds=300,
        )
        response_rows = []

    for (note,) in response_rows:
        if _looks_like_trial_pending_response(note):
            return True

    return False


def _is_return_notice_handled(
    *,
    expected_kinds: set[str],
    start_dt: date,
    signals: list[_ResponseSignal],
) -> bool:
    if not expected_kinds:
        return False

    # For return notices, a direct 1:1 match is unreliable because the notice dispatch number
    # doesn't always appear in the response document. Strong evidence is:
    # - same dispatch_no appears both before and after the return notice date (same kind).
    for kind in expected_kinds:
        pre: set[str] = set()
        post: set[str] = set()
        for s in signals:
            if not s.dispatch_digits:
                continue
            if s.kind is not None and s.kind != kind:
                continue
            if s.dt < start_dt:
                pre.add(s.dispatch_digits)
            else:
                post.add(s.dispatch_digits)
        if pre & post:
            return True
    return False


def _is_return_notice_dispatch_mismatch(
    *,
    expected_kinds: set[str],
    start_dt: date,
    signals: list[_ResponseSignal],
) -> bool:
    """
    Negative evidence for return notices:
    - If we have dispatch numbers both before and after the notice date (same kind),
      but *none* match, we should NOT auto-clear by weaker heuristics.
    """
    if len(expected_kinds) != 1:
        return False
    (kind,) = tuple(expected_kinds)

    pre: set[str] = set()
    post: set[str] = set()
    for s in signals:
        if not s.dispatch_digits:
            continue
        if s.kind is not None and s.kind != kind:
            continue
        if s.dt < start_dt:
            pre.add(s.dispatch_digits)
        else:
            post.add(s.dispatch_digits)
    return bool(pre and post and not (pre & post))


def _is_uspto_notice_handled(
    *,
    uspto_raw_id: str | None,
    start_dt: date,
    signals: list[_ResponseSignal],
) -> bool:
    digits = _digits_only(uspto_raw_id)
    if not digits:
        return False
    return any(
        s.dispatch_digits and s.dispatch_digits == digits and s.dt >= start_dt for s in signals
    )


def _is_office_action_handled(
    *,
    doc_name: str | None,
    raw_id: str | None,
    start_dt: date,
    signals: list[_ResponseSignal],
) -> bool:
    doc = _normalize_space(doc_name or "")
    if "Notice" in doc:
        expected_kinds = _infer_return_notice_expected_kinds(doc_name)
        return _is_return_notice_handled(
            expected_kinds=expected_kinds, start_dt=start_dt, signals=signals
        )
    if (raw_id or "").strip().lower().startswith("uspto:"):
        return _is_uspto_notice_handled(uspto_raw_id=raw_id, start_dt=start_dt, signals=signals)
    return False


@dataclass(frozen=True)
class _OpenOfficeAction:
    oa_id: str
    doc_name: str
    raw_id: str | None
    start_dt: date | None
    due_dt: date | None
    due_raw: str


_OA_SUPERSEDING_EVENT_KEYS: set[str] = {
    "ALLOWANCE_DATE",
    "ALLOWANCE_RECEIVED_DATE",
    "REJECTION_DATE",
    "REJECTION_RECEIVED_DATE",
    "REGISTRATION_DATE",
    "REGISTRATION_FEE_PAID",
}
_OA_SUPERSEDING_CUSTOM_KEYS: tuple[str, ...] = (
    "reg_decision_date",
    "reg_decision_received",
    "gazette_decision_date",
    "gazette_decision_received",
    "rejection_date",
    "rejection_received_date",
    "registration_date",
    "reg_fee_paid_date",
)
_OA_SUPERSEDING_DOC_TOKENS: tuple[str, ...] = (
    "",
    "Publication decision",
    "Notice of allowance",
    "Patent",
    "Registration",
    "SettingsRegistration",
)


def _looks_like_oa_superseding_doc(doc_name: str | None) -> bool:
    compact = _normalize_space(doc_name or "").replace(" ", "")
    return bool(compact) and any(token in compact for token in _OA_SUPERSEDING_DOC_TOKENS)


def _fetch_office_action_superseding_dates(matter_id: str) -> list[date]:
    mid = (matter_id or "").strip()
    if not mid:
        return []

    out: list[date] = []

    def _add(raw: object) -> None:
        dt = _parse_date(raw)
        if dt:
            out.append(dt)

    try:
        from app.models.ip_records import EventKeyMap, MatterCustomField, MatterEvent

        event_rows = (
            db.session.query(MatterEvent.event_key, EventKeyMap.std_event_key, MatterEvent.event_at)
            .outerjoin(EventKeyMap, EventKeyMap.raw_event_key == MatterEvent.event_key)
            .filter(MatterEvent.matter_id == mid)
            .filter(MatterEvent.event_at.isnot(None))
            .filter(func.trim(MatterEvent.event_at) != "")
            .all()
        )
        for raw_key, std_key, event_at in event_rows:
            std = _normalize_std_key(raw_key, std_key)
            raw = (raw_key or "").strip()
            compact_raw = raw.replace(" ", "")
            if (
                std in _OA_SUPERSEDING_EVENT_KEYS
                or raw in _OA_SUPERSEDING_EVENT_KEYS
                or any(
                    token in compact_raw
                    for token in ("Notice of allowance", "", "Publication decision", "RegistrationPayment")
                )
            ):
                _add(event_at)

        custom_rows = MatterCustomField.query.filter(MatterCustomField.matter_id == mid).all()
        for row in custom_rows or []:
            data = row.data or {}
            if not isinstance(data, dict):
                continue
            for key in _OA_SUPERSEDING_CUSTOM_KEYS:
                _add(data.get(key))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_office_action_superseding_dates.events",
            log_key="matter_auto_status._fetch_office_action_superseding_dates.events",
            log_window_seconds=300,
        )

    try:
        rows = db.session.execute(
            text(
                """
                SELECT doc_name, received_date, notified_date
                FROM office_action
                WHERE matter_id = :mid
                  AND doc_name IS NOT NULL
                  AND TRIM(doc_name) <> ''
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
        for doc_name, received_date, notified_date in rows:
            if _looks_like_oa_superseding_doc(doc_name):
                _add(notified_date or received_date)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._fetch_office_action_superseding_dates.office_action",
            log_key="matter_auto_status._fetch_office_action_superseding_dates.office_action",
            log_window_seconds=300,
        )

    return sorted(set(out))


def _superseded_open_office_action_done_dates(
    *,
    matter_id: str,
    open_oas: list[_OpenOfficeAction],
) -> dict[str, date]:
    if not open_oas:
        return {}
    progress_dates = _fetch_office_action_superseding_dates(matter_id)
    if not progress_dates:
        return {}

    out: dict[str, date] = {}
    for oa in open_oas:
        if _looks_like_oa_superseding_doc(oa.doc_name):
            continue
        anchor = oa.start_dt or oa.due_dt
        if not anchor:
            continue
        later = [dt for dt in progress_dates if dt > anchor]
        if later:
            out[oa.oa_id] = min(later)
    return out


def _oa_expected_kinds(doc_name: str | None) -> set[str] | None:
    doc = _normalize_space(doc_name or "")
    if not doc:
        return None
    if "Notice" in doc:
        return _infer_return_notice_expected_kinds(doc_name)
    if any(
        token in doc
        for token in (
            "Office action",
            "Office action",
            "Notice",
            "Notice",
            "Notice",
        )
    ):
        return {"opinion", "correction"}
    if any(
        token in doc
        for token in (
            "",
            "",
            "Correction notice",
            "",
            "",
            "",
        )
    ):
        return {"correction"}
    return None


def _signal_kind_matches(
    expected: set[str] | None,
    signal_kind: str | None,
    *,
    signal: _ResponseSignal | None = None,
) -> bool:
    if expected is None:
        # When doc-name based expectation is unknown, allow strong dispatch-based
        # linkage even if kind inference failed.
        if signal is not None and bool(signal.dispatch_digits):
            return True
        return signal_kind is not None
    if signal_kind is None:
        return False
    return signal_kind in expected


def _oa_response_window_days() -> int:
    if not has_app_context():
        return 365
    try:
        value = ConfigService.get_int("OA_RESPONSE_SIGNAL_WINDOW_DAYS", 365)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._oa_response_window_days",
            log_key="matter_auto_status._oa_response_window_days",
            log_window_seconds=300,
        )
        value = 365
    if value is None:
        return 365
    return max(0, int(value))


def _assign_response_signals_to_open_office_actions_with_dates(
    *,
    open_oas: list[_OpenOfficeAction],
    signals: list[_ResponseSignal],
) -> dict[str, date]:
    """
    Like `_assign_response_signals_to_open_office_actions`, but returns an `oa_id -> dt`
    mapping for the assigned signal.
    """
    candidates = [oa for oa in open_oas if oa.start_dt]
    if not candidates or not signals:
        return {}

    candidates.sort(key=lambda x: (x.start_dt or date.min, x.oa_id))
    start_dates = [oa.start_dt or date.min for oa in candidates]
    expected_by_id: dict[str, set[str] | None] = {
        oa.oa_id: _oa_expected_kinds(oa.doc_name) for oa in candidates
    }

    window_days = _oa_response_window_days()
    window_delta = timedelta(days=window_days) if window_days > 0 else None

    # For return notices, require at least one "pre" response signal to avoid
    # clearing on a single unrelated post-notice response.
    has_pre_signal: dict[str, bool] = {}
    for oa in candidates:
        expected = expected_by_id.get(oa.oa_id)
        if expected is None or not oa.start_dt:
            continue
        if "Notice" not in _normalize_space(oa.doc_name):
            continue
        found = False
        for s in signals:
            if s.dt >= oa.start_dt:
                break
            if not _signal_kind_matches(expected, s.kind, signal=s):
                continue
            found = True
            break
        has_pre_signal[oa.oa_id] = found

    uspto_digits: set[str] = set()
    for oa in candidates:
        raw = (oa.raw_id or "").strip()
        if raw.lower().startswith("uspto:"):
            digits = _digits_only(raw)
            if digits:
                uspto_digits.add(digits)

    handled: dict[str, date] = {}
    for s in signals:
        idx = bisect_right(start_dates, s.dt) - 1
        if idx < 0:
            continue

        restrict_to_uspto = bool(s.dispatch_digits and s.dispatch_digits in uspto_digits)
        for j in range(idx, -1, -1):
            oa = candidates[j]
            if not oa.start_dt or oa.start_dt > s.dt:
                continue
            if window_delta and s.dt > (oa.start_dt + window_delta):
                continue

            if restrict_to_uspto:
                raw = (oa.raw_id or "").strip()
                is_uspto = raw.lower().startswith("uspto:") and _digits_only(raw) == s.dispatch_digits
                is_return_notice = "Notice" in _normalize_space(oa.doc_name)
                if not (is_uspto or is_return_notice):
                    continue

            expected = expected_by_id.get(oa.oa_id)
            if not _signal_kind_matches(expected, s.kind, signal=s):
                continue

            if (
                expected is not None
                and "Notice" in _normalize_space(oa.doc_name)
                and not has_pre_signal.get(oa.oa_id, False)
            ):
                continue

            prev = handled.get(oa.oa_id)
            if prev is None or s.dt < prev:
                handled[oa.oa_id] = s.dt
            break

    return handled


def _assign_response_signals_to_open_office_actions(
    *,
    open_oas: list[_OpenOfficeAction],
    signals: list[_ResponseSignal],
) -> set[str]:
    """
    Fallback matching when dispatch_no is missing/unreliable.

    We assign each response signal to the *most recent* open OA (by start date)
    that existed at that time, instead of clearing all older OAs.
    """
    return set(
        _assign_response_signals_to_open_office_actions_with_dates(
            open_oas=open_oas, signals=signals
        ).keys()
    )


_OPEN_OFFICE_ACTION_WITH_DUE_SQL = """
    SELECT
      oa.oa_id,
      oa.doc_name,
      oa.raw_id,
      oa.received_date,
      oa.notified_date,
      COALESCE(NULLIF(TRIM(oa.extended_due_date), ''), NULLIF(TRIM(oa.due_date), '')) AS due_date
    FROM office_action oa
    WHERE oa.matter_id = :mid
      AND (oa.done_date IS NULL OR TRIM(oa.done_date) = '')
      AND (
        (oa.due_date IS NOT NULL AND TRIM(oa.due_date) <> '')
        OR (oa.extended_due_date IS NOT NULL AND TRIM(oa.extended_due_date) <> '')
      )
"""


def _fetch_open_office_action_rows_with_due(
    matter_id: str,
    *,
    context: str,
    log_key: str,
) -> list[tuple[object, str | None, str | None, str | None, str | None, str | None]] | None:
    mid = (matter_id or "").strip()
    if not mid:
        return []
    try:
        return db.session.execute(
            text(_OPEN_OFFICE_ACTION_WITH_DUE_SQL).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=log_key,
            log_window_seconds=300,
        )
        return None


def _oa_start_dt(received_raw: str | None, notified_raw: str | None) -> date | None:
    # Prefer "notified/dispatch" date when present; fallback to received date.
    return _parse_date(notified_raw) or _parse_date(received_raw)


def _build_open_office_actions(
    rows: list[tuple[object, str | None, str | None, str | None, str | None, str | None]],
) -> list[_OpenOfficeAction]:
    out: list[_OpenOfficeAction] = []
    for oa_id, doc_name, raw_id, received_date, notified_date, due_raw in rows:
        if not _is_candidate_office_action_doc(doc_name):
            continue
        out.append(
            _OpenOfficeAction(
                oa_id=str(oa_id),
                doc_name=doc_name or "",
                raw_id=raw_id,
                start_dt=_oa_start_dt(received_date, notified_date),
                due_dt=_parse_date(due_raw),
                due_raw=due_raw or "",
            )
        )
    return out


def _compute_handled_open_oa_ids(
    *,
    open_oas: list[_OpenOfficeAction],
    signals: list[_ResponseSignal],
) -> tuple[set[str], set[str]]:
    handled: set[str] = set()
    blocked_fallback: set[str] = set()
    if not signals:
        return handled, blocked_fallback

    # Strong signals first (dispatch-based).
    for oa in open_oas:
        if not oa.start_dt:
            continue
        if "Notice" in _normalize_space(oa.doc_name):
            expected_kinds = _infer_return_notice_expected_kinds(oa.doc_name)
            if _is_return_notice_dispatch_mismatch(
                expected_kinds=expected_kinds,
                start_dt=oa.start_dt,
                signals=signals,
            ):
                blocked_fallback.add(oa.oa_id)
        if _is_office_action_handled(
            doc_name=oa.doc_name,
            raw_id=oa.raw_id,
            start_dt=oa.start_dt,
            signals=signals,
        ):
            handled.add(oa.oa_id)

    # Fallback assignment: timeline-based heuristics.
    assigned = _assign_response_signals_to_open_office_actions(open_oas=open_oas, signals=signals)
    handled |= assigned - blocked_fallback
    return handled, blocked_fallback


def _pick_open_office_action_red(matter_id: str) -> tuple[str, str]:
    mid = (matter_id or "").strip()
    if not mid:
        return "", ""

    try:
        rows = _fetch_open_office_action_rows_with_due(
            mid,
            context="matter_auto_status._pick_open_office_action_red.query",
            log_key="matter_auto_status._pick_open_office_action_red.query",
        )
        if rows is None:
            return "", ""
        if not rows:
            return "", ""

        open_oas = _build_open_office_actions(rows)
        if not open_oas:
            return "", ""

        # Only fetch/parsing response signals when we have actual open OA rows.
        # This saves a lot of overhead for matters that never had office actions.
        signals = _fetch_response_signals(mid)
        handled, _blocked_fallback = _compute_handled_open_oa_ids(
            open_oas=open_oas, signals=signals
        )
        handled |= set(
            _superseded_open_office_action_done_dates(matter_id=mid, open_oas=open_oas).keys()
        )

        candidates: list[tuple[date, str, str]] = []
        fallback: tuple[str, str] | None = None
        for oa in open_oas:
            if oa.oa_id in handled:
                continue
            if fallback is None or oa.oa_id < fallback[0]:
                fallback = (oa.oa_id, oa.doc_name)
            if oa.due_dt:
                candidates.append((oa.due_dt, oa.oa_id, oa.doc_name))

        if candidates:
            due_dt, _oa_id, doc_name = min(candidates, key=lambda x: (x[0], x[1]))
            return _normalize_space(doc_name), due_dt.strftime("%Y-%m-%d")

        if fallback:
            _oa_id, doc_name = fallback
            # due_raw exists by query filter; date_only_str is lenient.
            due_raw = next((oa.due_raw for oa in open_oas if oa.oa_id == _oa_id), "")
            return _normalize_space(doc_name), date_only_str(due_raw)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._pick_open_office_action_red",
            log_key="matter_auto_status._pick_open_office_action_red",
            log_window_seconds=300,
        )
        return "", ""
    return "", ""


def _collect_open_mgmt_status_red_deadline_candidates(
    matter_id: str,
    *,
    include_hidden: bool = False,
) -> list[tuple[date, str]]:
    """
    Fallback/display red sources from open MGMT status-red dockets.

    Why:
    - Some matters (notably migrated PCT rows) have sparse matter_event data.
    - MGMT deadline generation can still create the correct actionable
      `MGMT:STATUS_RED:*` docket row even when event-based red derivation has no signal.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return []

    try:
        rows = db.session.execute(
            text(
                """
                SELECT name_ref, name_free, due_date
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, FALSE) = FALSE
                  AND name_ref IS NOT NULL
                  AND UPPER(TRIM(name_ref)) LIKE 'MGMT:STATUS_RED:%'
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND due_date IS NOT NULL
                  AND TRIM(due_date) <> ''
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._collect_open_mgmt_status_red_deadline_candidates.query",
            log_key="matter_auto_status._collect_open_mgmt_status_red_deadline_candidates.query",
            log_window_seconds=300,
        )
        return []

    if not rows:
        return []

    today = _today()
    ctx = _get_matter_context(mid)
    candidates: list[tuple[date, str]] = []
    for name_ref, name_free, due_raw in rows:
        due_dt = _parse_date(due_raw)
        if not due_dt:
            continue

        label = ""
        ref = (name_ref or "").strip()
        marker = "MGMT:STATUS_RED:"
        if ref.upper().startswith(marker):
            label = normalize_red_status(ref[len(marker) :].strip())
        if not label:
            label = normalize_red_status((name_free or "").strip())
        if not label:
            continue
        if _is_pct_context(ctx) and _is_pct_advisory_status_red_label(label):
            continue

        # Ignore annuity-derived status-red labels (e.g. "4RenewalDeadline").
        try:
            from app.utils.annuity_deadline_routing import is_annuity_status_red_label

            if is_annuity_status_red_label(label):
                continue
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter_auto_status._pick_open_mgmt_status_red_deadline.annuity_label_check",
                log_key="matter_auto_status._pick_open_mgmt_status_red_deadline.annuity_label_check",
                log_window_seconds=300,
            )

        if include_hidden:
            if is_non_action_status_red_label(label) or _looks_like_non_red_document_title(label):
                continue
            if label == "FilingDeadline" and not _has_live_filing_deadline_source(
                matter_id=mid,
                due_date=due_dt,
            ):
                continue
            if label == "ForeignFilingDeadline" and _foreign_filing_priority_inactive_for_status(
                matter_id=mid,
                ctx=ctx,
            ):
                continue
        else:
            if not _is_operational_deadline_red_candidate(
                matter_id=mid,
                red_label=label,
                due_date=due_dt,
                ctx=ctx,
                today=today,
            ):
                continue

        candidates.append((due_dt, label))

    return candidates


def _pick_open_mgmt_status_red_deadline(matter_id: str) -> tuple[str, str]:
    candidates = _collect_open_mgmt_status_red_deadline_candidates(
        matter_id,
        include_hidden=False,
    )

    if not candidates:
        return "", ""

    today = _today()
    priority_exam = [(d, lbl) for d, lbl in candidates if lbl == "ExaminationOpen"]
    if priority_exam:
        priority_upcoming = [(d, lbl) for d, lbl in priority_exam if d >= today]
        if priority_upcoming:
            due_dt, label = min(priority_upcoming, key=lambda x: (x[0], x[1]))
        else:
            due_dt, label = max(priority_exam, key=lambda x: (x[0], x[1]))
        return label, due_dt.strftime("%Y-%m-%d")

    upcoming = [(d, lbl) for d, lbl in candidates if d >= today]
    if upcoming:
        due_dt, label = min(upcoming, key=lambda x: (x[0], x[1]))
    else:
        due_dt, label = max(candidates, key=lambda x: (x[0], x[1]))

    return label, due_dt.strftime("%Y-%m-%d")


def _prefer_earlier_mgmt_status_red(
    current_red: str | None,
    current_red_date: str | None,
    mgmt_red: str | None,
    mgmt_red_date: str | None,
) -> tuple[str, str]:
    current_label = normalize_red_status(current_red)
    current_due = date_only_str(current_red_date)
    mgmt_label = normalize_red_status(mgmt_red)
    mgmt_due = date_only_str(mgmt_red_date)
    if not mgmt_label or not mgmt_due:
        return current_label, current_due

    mgmt_due_dt = _parse_date(mgmt_due)
    if not mgmt_due_dt:
        return current_label, current_due

    current_due_dt = _parse_date(current_due)
    if not current_label or not current_due_dt or mgmt_due_dt < current_due_dt:
        return mgmt_label, mgmt_due

    return current_label, current_due


def _blue_from_open_office_action(doc_name: str | None) -> str:
    """
    Decide Blue status when there is an open(unhandled) office action.

    Goal:
    - Keep Blue as "what work are we doing now?"
    - Even when Red is a different (earlier) deadline, an open OA should surface as Blue.
    """
    s = _normalize_space(doc_name or "")
    if not s:
        return ""
    if _looks_like_payment_notice_label(s):
        return ""
    if _looks_like_non_response_notice_label(s):
        mapped = normalize_blue_status(_suggest_blue_from_red(s))
        if mapped in {
            "Filing Publication In Progress",
            "RegistrationWaiting In Progress",
            "Matter closed",
        }:
            return mapped
        return ""
    if _looks_like_oa_response_notice(s):
        return "OA  In Progress"

    # For other OA-like documents (e.g. Notice of allowance), reuse the red->blue mapping when possible.
    mapped = _suggest_blue_from_red(s)
    return mapped or "OA  In Progress"


def _resolve_notice_send_doc_name_from_red(
    matter_id: str, red_label: str | None, *, name_free_hint: str | None = None
) -> str:
    """
    Resolve `MGMT:NOTICE_SEND_3D:<oa_id>` red labels into the underlying OA document name.

    This helps Blue status mapping use human-readable notice semantics
    (e.g. Publication decision notice -> Filing Publication In Progress) instead of a technical MGMT ref.
    """
    mid = (matter_id or "").strip()
    raw = (red_label or "").strip()
    if not mid or not raw:
        return ""

    m = _NOTICE_SEND_NAME_REF_RE.match(raw)
    if not m:
        return ""
    oa_id = (m.group(1) or "").strip()
    if not oa_id:
        return ""

    hint = _normalize_space(name_free_hint or "")
    if hint:
        if "·" in hint:
            hint = _normalize_space(hint.split("·")[-1])
        if hint:
            return hint

    try:
        row = db.session.execute(
            text(
                """
                SELECT COALESCE(NULLIF(TRIM(doc_name), ''), '')
                FROM office_action
                WHERE matter_id = :mid
                  AND oa_id = :oa_id
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid, "oa_id": oa_id},
        ).first()
        if row:
            doc_name = _normalize_space(row[0] or "")
            if doc_name:
                return doc_name
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._resolve_notice_send_doc_name_from_red.office_action",
            log_key="matter_auto_status._resolve_notice_send_doc_name_from_red.office_action",
            log_window_seconds=300,
        )

    # Fallback: parse docket label suffix "Notice Client(3 ) · <doc_name>".
    try:
        row = db.session.execute(
            text(
                """
                SELECT COALESCE(NULLIF(TRIM(name_free), ''), '')
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, false) = false
                  AND UPPER(COALESCE(TRIM(name_ref), '')) = UPPER(:name_ref)
                ORDER BY COALESCE(NULLIF(TRIM(due_date), ''), ''), docket_id
                LIMIT 1
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid, "name_ref": raw},
        ).first()
        if row:
            name_free = _normalize_space(row[0] or "")
            if "·" in name_free:
                name_free = _normalize_space(name_free.split("·")[-1])
            return name_free
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._resolve_notice_send_doc_name_from_red.docket_item",
            log_key="matter_auto_status._resolve_notice_send_doc_name_from_red.docket_item",
            log_window_seconds=300,
        )

    return ""


def _suggest_blue_from_red_with_notice_fallback(matter_id: str, red: str) -> str:
    mapped = _suggest_blue_from_red(red)
    if mapped:
        return mapped
    doc_name = _resolve_notice_send_doc_name_from_red(matter_id, red)
    if not doc_name:
        return ""
    return _suggest_blue_from_red(doc_name)


def _pick_open_notice_send_blue_signal(matter_id: str) -> str:
    """
    Best-effort blue signal from active NOTICE_SEND_3D dockets.

    Use this when current/status red is empty so near-term notice communication
    stages are still reflected in auto blue.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return ""

    try:
        rows = (
            db.session.execute(
                text(
                    """
                SELECT
                  COALESCE(NULLIF(TRIM(name_ref), ''), '') AS name_ref,
                  COALESCE(NULLIF(TRIM(name_free), ''), '') AS name_free
                FROM docket_item
                WHERE matter_id = :mid
                  AND COALESCE(is_deleted, false) = false
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND UPPER(COALESCE(TRIM(name_ref), '')) LIKE 'MGMT:NOTICE_SEND_3D:%'
                ORDER BY COALESCE(NULLIF(TRIM(due_date), ''), '9999-12-31'), docket_id
                LIMIT 20
                """
                ).execution_options(policy_bypass=True),
                {"mid": mid},
            )
            .mappings()
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._pick_open_notice_send_blue_signal.query",
            log_key="matter_auto_status._pick_open_notice_send_blue_signal.query",
            log_window_seconds=300,
        )
        return ""

    for r in rows:
        ref = _normalize_space(r.get("name_ref") or "")
        if not ref:
            continue
        doc_name = _resolve_notice_send_doc_name_from_red(
            mid,
            ref,
            name_free_hint=r.get("name_free") or "",
        )
        mapped = normalize_blue_status(_suggest_blue_from_red(doc_name))
        if mapped:
            return mapped
    return ""


def get_unhandled_open_office_action_ids(matter_id: str) -> set[str]:
    """
    Return office_action.oa_id values that are considered "open/unhandled" for the matter.

    This mirrors the same evidence model used by `_pick_open_office_action_red()`:
    - We only consider OfficeAction rows that are open (done_date blank) and have a due date.
    - If response signals indicate the OA was handled, it's excluded.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return set()

    rows = _fetch_open_office_action_rows_with_due(
        mid,
        context="matter_auto_status.get_unhandled_open_office_action_ids.query",
        log_key="matter_auto_status.get_unhandled_open_office_action_ids.query",
    )
    if rows is None:
        return set()
    if not rows:
        return set()
    open_oas = _build_open_office_actions(rows)
    if not open_oas:
        return set()

    # If we can't fetch response signals (no app context, no document parser, etc.),
    # be conservative: treat all open OA as unhandled so deadlines remain visible.
    superseded = set(
        _superseded_open_office_action_done_dates(matter_id=mid, open_oas=open_oas).keys()
    )
    signals = _fetch_response_signals(mid)
    if not signals:
        return {oa.oa_id for oa in open_oas if oa.oa_id not in superseded}

    handled, _blocked_fallback = _compute_handled_open_oa_ids(open_oas=open_oas, signals=signals)
    handled |= superseded

    return {oa.oa_id for oa in open_oas if oa.oa_id not in handled}


def _return_notice_handled_dt(
    expected_kinds: set[str], start_dt: date, signals: list[_ResponseSignal]
) -> date | None:
    if not expected_kinds:
        return None
    best: date | None = None
    for kind in expected_kinds:
        pre: set[str] = set()
        post_best: dict[str, date] = {}
        for s in signals:
            if not s.dispatch_digits:
                continue
            if s.kind is not None and s.kind != kind:
                continue
            if s.dt < start_dt:
                pre.add(s.dispatch_digits)
                continue
            prev = post_best.get(s.dispatch_digits)
            if prev is None or s.dt < prev:
                post_best[s.dispatch_digits] = s.dt
        common = pre & set(post_best.keys())
        if not common:
            continue
        dt = min(post_best[d] for d in common)
        if best is None or dt < best:
            best = dt
    return best


def _uspto_handled_dt(
    *, uspto_raw_id: str | None, start_dt: date, signals: list[_ResponseSignal]
) -> date | None:
    digits = _digits_only(uspto_raw_id)
    if not digits:
        return None
    dts = [
        s.dt
        for s in signals
        if s.dispatch_digits and s.dispatch_digits == digits and s.dt >= start_dt
    ]
    return min(dts) if dts else None


def get_handled_open_office_action_done_dates(matter_id: str) -> dict[str, str]:
    """
    Return `oa_id -> done_date(YYYY-MM-DD)` for OfficeAction rows that are:
    - open (done_date blank)
    - have a due date
    - and are considered handled based on the same response-signal model.

    This is intended for DB reconciliation (closing stale OA + linked dockets) so
    other systems (todos/notifications/calendar) don't diverge.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return {}

    rows = _fetch_open_office_action_rows_with_due(
        mid,
        context="matter_auto_status.get_handled_open_office_action_done_dates.query",
        log_key="matter_auto_status.get_handled_open_office_action_done_dates.query",
    )
    if rows is None:
        return {}
    if not rows:
        return {}
    open_oas = _build_open_office_actions(rows)
    if not open_oas:
        return {}

    signals = _fetch_response_signals(mid, include_weak_signals=False)
    if not signals:
        return {
            oa_id: done_dt.strftime("%Y-%m-%d")
            for oa_id, done_dt in _superseded_open_office_action_done_dates(
                matter_id=mid,
                open_oas=open_oas,
            ).items()
        }

    strong: dict[str, date] = {}
    blocked_fallback: set[str] = set()
    for oa in open_oas:
        if not oa.start_dt:
            continue
        doc = _normalize_space(oa.doc_name)
        raw = (oa.raw_id or "").strip()

        if "Notice" in doc:
            expected_kinds = _infer_return_notice_expected_kinds(oa.doc_name)
            if _is_return_notice_dispatch_mismatch(
                expected_kinds=expected_kinds, start_dt=oa.start_dt, signals=signals
            ):
                blocked_fallback.add(oa.oa_id)
            dt = _return_notice_handled_dt(expected_kinds, oa.start_dt, signals)
            if dt:
                strong[oa.oa_id] = dt
        elif raw.lower().startswith("uspto:"):
            dt = _uspto_handled_dt(uspto_raw_id=raw, start_dt=oa.start_dt, signals=signals)
            if dt:
                strong[oa.oa_id] = dt

    assigned = _assign_response_signals_to_open_office_actions_with_dates(
        open_oas=open_oas, signals=signals
    )

    done_by_oa: dict[str, date] = dict(strong)
    for oa_id, dt in assigned.items():
        if oa_id in blocked_fallback:
            continue
        prev = done_by_oa.get(oa_id)
        if prev is None or dt < prev:
            done_by_oa[oa_id] = dt
    for oa_id, dt in _superseded_open_office_action_done_dates(
        matter_id=mid,
        open_oas=open_oas,
    ).items():
        prev = done_by_oa.get(oa_id)
        if prev is None or dt < prev:
            done_by_oa[oa_id] = dt

    return {oa_id: dt.strftime("%Y-%m-%d") for oa_id, dt in done_by_oa.items()}


def _pick_event_based_red(
    matter_id: str,
    *,
    ctx: MatterContext | None = None,
    today: date | None = None,
    event_rows: list[tuple[str, str | None, str | None]] | None = None,
    event_summary: EventSummary | None = None,
    expired_deadlines: set[str] | None = None,
) -> tuple[str, str]:
    mid = (matter_id or "").strip()
    if not mid:
        return "", ""

    if event_rows is None:
        event_rows = _fetch_event_rows(mid)
    if event_summary is None:
        event_summary = _summarize_event_rows(event_rows)

    today = today or _today()
    present_std_keys = set(event_summary.presence)
    due_by_std_key = _build_due_by_std_key(event_summary)
    rule_by_key = _get_red_rule_by_key()
    rule_by_label = _get_red_rule_by_label()

    pipeline_target_stage: int | None = None
    try:
        open_pipeline_stages: list[int] = []
        for r in _get_red_rules():
            if r.red_class != "pipeline":
                continue
            if not _is_rule_applicable(r, present_std_keys, event_summary):
                continue
            if _is_rule_completed(r, present_std_keys, event_summary):
                continue
            if expired_deadlines and r.deadline_event_key in expired_deadlines:
                continue
            due = due_by_std_key.get(r.deadline_event_key)
            if not due:
                open_pipeline_stages.append(r.stage)
                continue
            if not _is_operational_deadline_red_candidate(
                matter_id=mid,
                red_label=r.label,
                due_date=due,
                ctx=ctx,
                today=today,
                event_presence=present_std_keys,
                event_summary=event_summary,
            ):
                continue
            open_pipeline_stages.append(r.stage)
        if open_pipeline_stages:
            pipeline_target_stage = min(open_pipeline_stages)
    except Exception:
        pipeline_target_stage = None

    candidates: list[tuple[date, str]] = []
    seen_labels: set[str] = set()

    # 1) Prefer aggregated std-key based due dates (handles extensions for *_DEADLINE correctly).
    for std_key, due in due_by_std_key.items():
        if expired_deadlines and std_key in expired_deadlines:
            continue
        label = (_STD_EVENT_TO_RED_LABEL.get(std_key) or "").strip()
        if not label:
            continue
        # If we have a rule for this std/label and it's already completed, skip.
        rule = rule_by_key.get(std_key) or rule_by_label.get(label)
        if rule and _is_rule_completed(rule, present_std_keys, event_summary):
            continue
        if (
            rule
            and rule.red_class == "pipeline"
            and pipeline_target_stage is not None
            and rule.stage != pipeline_target_stage
        ):
            continue
        if not _is_operational_deadline_red_candidate(
            matter_id=mid,
            red_label=label,
            due_date=due,
            ctx=ctx,
            today=today,
            event_presence=present_std_keys,
            event_summary=event_summary,
        ):
            continue
        candidates.append((due, label))
        seen_labels.add(label)

    # 2) Include raw-only labels that do not have reliable std mapping.
    #    For each label, keep the latest date to avoid selecting outdated pre-extension dates.
    raw_label_best: dict[str, date] = {}
    for raw_key, std_key, event_at in event_rows:
        raw = (raw_key or "").strip()
        label = (_RAW_EVENT_TO_RED_LABEL.get(raw) or "").strip()
        if not label or label in seen_labels:
            continue
        dt = _parse_date(event_at)
        if not dt:
            continue
        prev = raw_label_best.get(label)
        if prev is None or dt > prev:
            raw_label_best[label] = dt

    for label, due in raw_label_best.items():
        std = _RED_LABEL_TO_STD_EVENT.get(label)
        if std and expired_deadlines and std in expired_deadlines:
            continue
        rule = rule_by_label.get(label)
        if rule and _is_rule_completed(rule, present_std_keys, event_summary):
            continue
        if (
            rule
            and rule.red_class == "pipeline"
            and pipeline_target_stage is not None
            and rule.stage != pipeline_target_stage
        ):
            continue
        if not _is_operational_deadline_red_candidate(
            matter_id=mid,
            red_label=label,
            due_date=due,
            ctx=ctx,
            today=today,
            event_presence=present_std_keys,
            event_summary=event_summary,
        ):
            continue
        candidates.append((due, label))

    if not candidates:
        return "", ""

    upcoming = [(d, lbl) for d, lbl in candidates if d >= today]
    if upcoming:
        d, lbl = min(upcoming, key=lambda x: x[0])
        return lbl, d.strftime("%Y-%m-%d")

    d, lbl = max(candidates, key=lambda x: x[0])
    return lbl, d.strftime("%Y-%m-%d")


def _is_stale_oa_red(*, matter_id: str, red_label: str) -> bool:
    """
    Checks if 'red_label' is an Office Action document name that is no longer 'Open'.
    If it's stale (closed/done), return True -> should be cleared.
    """
    mid = (matter_id or "").strip()
    lbl = (red_label or "").strip()
    if not mid or not lbl:
        return False
    oa_like_label = _looks_like_oa_response_notice(lbl)

    def _oa_start_dt(received_raw: str | None, notified_raw: str | None) -> date | None:
        return _parse_date(notified_raw) or _parse_date(received_raw)

    def _is_blank(value: str | None) -> bool:
        return not (value or "").strip()

    try:
        # Normalize matching: Matter.status_red may be canonicalized (e.g. "Notice" -> "Notice"),
        # while office_action.doc_name may keep the original variant. Compare on normalized labels.
        rows = db.session.execute(
            text(
                """
                SELECT
                  oa_id,
                  doc_name,
                  raw_id,
                  received_date,
                  notified_date,
                  due_date,
                  extended_due_date,
                  done_date
                FROM office_action
                WHERE matter_id = :mid
                """
            ).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._is_stale_oa_red.query",
            log_key="matter_auto_status._is_stale_oa_red.query",
            log_window_seconds=300,
        )
        return False

    if not rows:
        if oa_like_label:
            return not bool(_pick_open_mgmt_status_red_due_for_label(mid, lbl))
        return False

    matching: list[tuple[str, str | None, str | None]] = []
    open_with_due_ids: set[str] = set()
    open_oas_all: list[_OpenOfficeAction] = []

    for (
        oa_id,
        doc_name,
        raw_id,
        received_date,
        notified_date,
        due_date,
        extended_due_date,
        done_date,
    ) in rows:
        if not _is_candidate_office_action_doc(doc_name):
            continue
        due_raw = (extended_due_date or "").strip() or (due_date or "").strip()

        if _office_action_label_matches(doc_name, lbl):
            matching.append((str(oa_id), done_date, due_raw))
            if _is_blank(done_date) and due_raw:
                open_with_due_ids.add(str(oa_id))

        if _is_blank(done_date) and due_raw:
            open_oas_all.append(
                _OpenOfficeAction(
                    oa_id=str(oa_id),
                    doc_name=doc_name or "",
                    raw_id=raw_id,
                    start_dt=_oa_start_dt(received_date, notified_date),
                    due_dt=_parse_date(due_raw),
                    due_raw=due_raw,
                )
            )

    # If we can't find any OA doc that normalizes to this label, treat it as a custom red label.
    if not matching:
        if oa_like_label:
            return not bool(_pick_open_mgmt_status_red_due_for_label(mid, lbl))
        return False

    if open_with_due_ids:
        superseded = set(
            _superseded_open_office_action_done_dates(
                matter_id=mid,
                open_oas=open_oas_all,
            ).keys()
        )
        if open_with_due_ids.issubset(superseded):
            return True

        signals = _fetch_response_signals(mid)
        if not signals:
            return False  # Valid (Open with due date, and no response signal)

        handled: set[str] = set()
        blocked_fallback: set[str] = set()
        for oa in open_oas_all:
            if not oa.start_dt:
                continue
            if "Notice" in _normalize_space(oa.doc_name):
                expected_kinds = _infer_return_notice_expected_kinds(oa.doc_name)
                if _is_return_notice_dispatch_mismatch(
                    expected_kinds=expected_kinds,
                    start_dt=oa.start_dt,
                    signals=signals,
                ):
                    blocked_fallback.add(oa.oa_id)
            if _is_office_action_handled(
                doc_name=oa.doc_name,
                raw_id=oa.raw_id,
                start_dt=oa.start_dt,
                signals=signals,
            ):
                handled.add(oa.oa_id)
        assigned = _assign_response_signals_to_open_office_actions(
            open_oas=open_oas_all, signals=signals
        )
        handled |= assigned - blocked_fallback

        return open_with_due_ids.issubset(handled)

    # If there are open instances but no due dates, treat as stale for red.
    if any(_is_blank(done_date) for _oa_id, done_date, _due_raw in matching):
        return True

    return True  # Stale (Closed or no open with due date)


def derive_auto_status(
    *,
    matter_id: str,
    current_red: str | None = None,
    current_red_date: str | None = None,
    current_blue: str | None = None,
    memo: str | None = None,
) -> AutoStatus:
    """
    Derive automatic matter status.

    Red status is based on key external deadlines.
    Blue status falls back through red status, manual values, then defaults.
    """
    raw_current_red = _normalize_space(current_red or "")
    invalid_internal_mgmt_red = is_internal_mgmt_non_status_red_ref(raw_current_red)
    red = "" if invalid_internal_mgmt_red else normalize_red_status(raw_current_red)
    red_date = date_only_str(current_red_date)
    blue = normalize_blue_status(current_blue)
    memo_txt = (memo or "").strip()
    mid = (matter_id or "").strip()
    ctx = _get_matter_context(mid) if mid else MatterContext("", "", "", False)

    if invalid_internal_mgmt_red:
        red_date = ""

    # Guardrail: historically some imports stored document titles (e.g. "PatentFiling") in status_red.
    # Those are not actionable "red" labels and should not be preserved as-is.
    if _looks_like_non_red_document_title(red):
        red = ""
        red_date = ""
    if is_non_action_status_red_label(red):
        red = ""
        red_date = ""

    # If the stored red includes a date suffix like "FilingDeadline[2026-11-19]",
    # split it into (red, red_date) for consistent rendering.
    if red:
        m = re.match(r"^(.*)\[(\d{4}[-./]\d{1,2}[-./]\d{1,2}).*\]$", red)
        if m:
            red = normalize_red_status(m.group(1))
            embedded = date_only_str(m.group(2))
            if embedded and not red_date:
                red_date = embedded
    if _is_pct_context(ctx) and _is_pct_advisory_status_red_label(red):
        red = ""
        red_date = ""
    preserve_manual_terminal_red = _is_manual_terminal_red_status(red)

    # =========================================================================
    # New   Red Select 
    # =========================================================================

    #   
    event_rows = _fetch_event_rows(mid) if mid else []
    event_summary = _summarize_event_rows(event_rows)
    _supplement_event_summary_from_custom_fields(mid, event_summary, ctx=ctx)
    event_presence = event_summary.presence
    event_due_by_std_key = _build_due_by_std_key(event_summary)
    today = _today()
    preserve_future_term_expiry_red = is_future_term_expiry_status(red, red_date, today=today)
    expired_deadlines: set[str] = set()
    passive_stage_blue = ""

    # -----------------------------------------------------------------------------
    # Fallback: registration fee deadline from allowance date
    #
    # Some migrated rows include allowance ("Notice of allowance ") but miss REGISTRATION_DEADLINE.
    # In that case, Blue can become "RegistrationWaiting In Progress" while Red shows nothing.
    # Compute:
    #   - RegistrationDeadline = allowance + 3 months
    #   - RegistrationDeadline = allowance + 6 months
    # This mirrors the USPTO upload automation logic.
    # -----------------------------------------------------------------------------
    try:
        has_reg = "REGISTRATION_DATE" in event_presence
        has_reg_deadline = bool(event_due_by_std_key.get("REGISTRATION_DEADLINE"))
        has_penalty_deadline = bool(event_due_by_std_key.get("PENALTY_REG_DEADLINE"))

        # USPTO-specific inference should not be applied to outgoing (Foreign) matters.
        # Also skip for non-IP case types (litigation/misc/pct).
        is_uspto_reg_inference_allowed = ctx.is_uspto and ctx.matter_type not in {
            "PCT",
            "LITIGATION",
            "TRIAL",
            "LAWSUIT",
            "MISC",
        }
        # Prefer the latest allowance-related signal if multiple exist.
        allowance_dt = (
            event_summary.max_dates.get("ALLOWANCE_DATE")
            or event_summary.max_dates.get("ALLOWANCE_RECEIVED_DATE")
            or event_due_by_std_key.get("ALLOWANCE_DATE")
            or event_due_by_std_key.get("ALLOWANCE_RECEIVED_DATE")
        )

        if is_uspto_reg_inference_allowed and allowance_dt and not has_reg:
            reg_due_dt, penalty_due_dt = _no_registration_fee_deadlines(
                allowance_dt,
                ctx.matter_type,
            )
            if not has_reg_deadline:
                event_due_by_std_key["REGISTRATION_DEADLINE"] = reg_due_dt
            if penalty_due_dt is not None and (not has_penalty_deadline):
                event_due_by_std_key["PENALTY_REG_DEADLINE"] = penalty_due_dt
    except Exception as exc:
        # Best-effort: do not block status derivation if inference fails.
        report_swallowed_exception(
            exc,
            context="matter_auto_status.registration_deadline_fallback",
            log_key="matter_auto_status.registration_deadline_fallback",
            log_window_seconds=300,
        )
    expired_deadlines = _expired_deadlines_by_policy(
        event_due_by_std_key,
        today=today,
    )
    registration_completed = _has_event(event_summary, "REGISTRATION_DATE") or _has_event(
        event_summary,
        "REGISTRATION_FEE_PAID",
    )

    #  Status Confirm ()
    is_closed = _is_terminal_event_reached(
        event_summary=event_summary,
        event_presence=event_presence,
        today=today,
    )

    # Open office action (unhandled) signal:
    # - Used to surface Blue as "OA  In Progress" even when Red is a different deadline.
    # - Also used to override Red when the OA due date is earlier than the current Red due date.
    open_oa_doc = ""
    open_oa_due = ""
    if not is_closed and not registration_completed and mid:
        open_oa_doc, open_oa_due = _pick_open_office_action_red(mid)

    if is_closed:
        #  Matter Red None
        red = ""
        red_date = ""
        preserve_manual_terminal_red = False
    elif registration_completed and not preserve_future_term_expiry_red:
        red = ""
        red_date = ""
        preserve_manual_terminal_red = False
    else:
        original_red = red
        original_red_date = red_date
        unknown_red = False

        def _preserve_hidden_current_deadline_for_open_oa(label: str | None) -> bool:
            return (
                bool(open_oa_doc)
                and normalize_red_status(label) == "ForeignFilingDeadline"
                and _has_supporting_red_signal(
                    matter_id=mid,
                    red_label="ForeignFilingDeadline",
                    event_summary=event_summary,
                )
                and not _foreign_filing_priority_inactive_for_status(
                    matter_id=mid,
                    ctx=ctx,
                    event_presence=event_presence,
                    event_summary=event_summary,
                )
            )

        # Existing Red   (Done  Clear)
        if red and not preserve_manual_terminal_red and not preserve_future_term_expiry_red:
            rule = _get_red_rule_by_label().get(red)
            if rule:
                #   Red: Done items Confirm
                if _is_rule_completed(rule, event_presence, event_summary):
                    red = ""
                    red_date = ""
                # active items   Clear
                elif not _is_rule_applicable(rule, event_presence, event_summary):
                    red = ""
                    red_date = ""
                elif expired_deadlines and rule.deadline_event_key in expired_deadlines:
                    red = ""
                    red_date = ""
                else:
                    due_dt = _resolve_deadline_red_due_date(
                        matter_id=mid,
                        red_label=red,
                        current_red_date=red_date,
                        event_due_by_std_key=event_due_by_std_key,
                        event_rows=event_rows,
                        today=today,
                    )
                    if (
                        not _is_operational_deadline_red_candidate(
                            matter_id=mid,
                            red_label=red,
                            due_date=due_dt,
                            ctx=ctx,
                            today=today,
                            event_presence=event_presence,
                            event_summary=event_summary,
                        )
                        and not _is_supported_current_deadline_red_candidate(
                            matter_id=mid,
                            red_label=red,
                            due_date=due_dt,
                            event_summary=event_summary,
                        )
                        and not _preserve_hidden_current_deadline_for_open_oa(red)
                    ):
                        red = ""
                        red_date = ""
            elif red in _RED_LABEL_TO_STD_EVENT or red in _RAW_RED_LABEL_TO_EVENT_KEY:
                # Legacy  
                std_key = _RED_LABEL_TO_STD_EVENT.get(red)
                if std_key and expired_deadlines and std_key in expired_deadlines:
                    red = ""
                    red_date = ""
                elif not _has_supporting_red_signal(
                    matter_id=mid,
                    red_label=red,
                    event_summary=event_summary,
                ):
                    red = ""
                    red_date = ""
                else:
                    due_dt = _resolve_deadline_red_due_date(
                        matter_id=mid,
                        red_label=red,
                        current_red_date=red_date,
                        event_due_by_std_key=event_due_by_std_key,
                        event_rows=event_rows,
                        today=today,
                    )
                    if (
                        not _is_operational_deadline_red_candidate(
                            matter_id=mid,
                            red_label=red,
                            due_date=due_dt,
                            ctx=ctx,
                            today=today,
                            event_presence=event_presence,
                            event_summary=event_summary,
                        )
                        and not _is_supported_current_deadline_red_candidate(
                            matter_id=mid,
                            red_label=red,
                            due_date=due_dt,
                            event_summary=event_summary,
                        )
                        and not _preserve_hidden_current_deadline_for_open_oa(red)
                    ):
                        red = ""
                        red_date = ""
            elif _is_stale_oa_red(matter_id=mid, red_label=red):
                # OA Done  Clear
                red = ""
                red_date = ""
            else:
                due_dt = _resolve_deadline_red_due_date(
                    matter_id=mid,
                    red_label=red,
                    current_red_date=red_date,
                    event_due_by_std_key=event_due_by_std_key,
                    event_rows=event_rows,
                    today=today,
                )
                if due_dt and not _is_operational_deadline_red_candidate(
                    matter_id=mid,
                    red_label=red,
                    due_date=due_dt,
                    ctx=ctx,
                    today=today,
                    event_presence=event_presence,
                    event_summary=event_summary,
                ):
                    red = ""
                    red_date = ""
                else:
                    unknown_red = True

        if not red and open_oa_doc and normalize_red_status(original_red) == "ForeignFilingDeadline":
            due_dt = _resolve_deadline_red_due_date(
                matter_id=mid,
                red_label=original_red,
                current_red_date=original_red_date,
                event_due_by_std_key=event_due_by_std_key,
                event_rows=event_rows,
                today=today,
            )
            if _has_supporting_red_signal(
                matter_id=mid,
                red_label="ForeignFilingDeadline",
                event_summary=event_summary,
            ) and not _foreign_filing_priority_inactive_for_status(
                matter_id=mid,
                ctx=ctx,
                event_presence=event_presence,
                event_summary=event_summary,
            ):
                red = "ForeignFilingDeadline"
                red_date = date_only_str(due_dt) or original_red_date

        # Red ( unknown red) New Select
        if (
            (not preserve_manual_terminal_red)
            and (not preserve_future_term_expiry_red)
            and (not red or unknown_red)
        ):
            # 1: Done OA ()
            oa_doc, oa_due = open_oa_doc, open_oa_due
            if oa_doc:
                red = oa_doc
                red_date = oa_due
            else:
                # 2:   Red Select (active items +  )
                ev_red, ev_dt = _pick_red_by_priority(
                    event_presence,
                    event_due_by_std_key,
                    matter_id=mid,
                    ctx=ctx,
                    today=today,
                    expired_deadlines=expired_deadlines,
                    event_summary=event_summary,
                )
                if ev_red:
                    red = ev_red
                    red_date = ev_dt
                else:
                    # 3: Legacy   (fallback)
                    ev_red, ev_dt = _pick_event_based_red(
                        mid,
                        ctx=ctx,
                        today=today,
                        event_rows=event_rows,
                        event_summary=event_summary,
                        expired_deadlines=expired_deadlines,
                    )
                    if ev_red:
                        red = ev_red
                        red_date = ev_dt
            # 4: column MGMT status-red  (PCT     fallback)
            # - no red at all, or
            # - unknown red is still unchanged after OA/event selection
            if (not red) or (unknown_red and red == original_red and red_date == original_red_date):
                mgmt_red, mgmt_dt = _pick_open_mgmt_status_red_deadline(mid)
                if mgmt_red:
                    red = mgmt_red
                    red_date = mgmt_dt
            if not red and unknown_red:
                red = original_red
                red_date = original_red_date

        current_is_open_oa = bool(
            open_oa_doc
            and _normalize_space(red) == _normalize_space(open_oa_doc)
            and date_only_str(red_date) == date_only_str(open_oa_due)
        )
        if (
            mid
            and not current_is_open_oa
            and not preserve_manual_terminal_red
            and not preserve_future_term_expiry_red
        ):
            mgmt_red, mgmt_dt = _pick_open_mgmt_status_red_deadline(mid)
            red, red_date = _prefer_earlier_mgmt_status_red(red, red_date, mgmt_red, mgmt_dt)

        # Litigation/trial matters often transition from a filing deadline into a pending stage
        # once the petition is actually filed or the IPTAB assigns a trial/panel number.
        # In that state, a past request deadline should no longer dominate the persisted red label.
        if _has_litigation_pending_signal(mid, ctx=ctx, event_summary=event_summary):
            red_due_dt = _parse_date(red_date)
            red_norm = _normalize_space(red)
            if (
                (not red)
                or red == ""
                or (red_norm == "/Billing/Deadline" and (not red_due_dt or red_due_dt <= today))
            ):
                passive_stage_blue = ""
                red = ""
                red_date = ""

    if is_non_action_status_red_label(red):
        passive_stage_blue = passive_stage_blue or normalize_red_status(red)
        red = ""
        red_date = ""

    if red:
        refreshed_red_date = _refresh_red_related_date_from_authoritative_sources(
            matter_id=mid,
            raw_red_label=red,
            event_due_by_std_key=event_due_by_std_key,
            event_rows=event_rows,
            today=today,
        )
        if refreshed_red_date:
            red_date = refreshed_red_date

    # Red  
    if red and not red_date:
        inferred = _infer_red_related_date_from_event_signals(
            red,
            event_due_by_std_key=event_due_by_std_key,
            event_rows=event_rows,
            today=today,
        )
        if inferred:
            red_date = inferred

    # If an unhandled open OA exists, and it's earlier than the currently-selected Red,
    # switch Red to the OA. This prevents a valid-but-outdated stored Red from hiding a new OA.
    if (
        open_oa_doc
        and open_oa_due
        and not preserve_manual_terminal_red
        and not preserve_future_term_expiry_red
    ):
        oa_dt = _parse_date(open_oa_due)
        red_dt = _parse_date(red_date)
        if oa_dt and (not red_dt or oa_dt < red_dt):
            red = _normalize_space(open_oa_doc)
            red_date = date_only_str(open_oa_due)

    # Fallback: if Examination requestDeadline  , Filing date (Patent/) Display  .
    fallback_due_by_std_key: dict[str, date] = {}
    try:
        needs_exam_deadline_fallback = (
            "APPLICATION_DATE" in event_presence
            and "EXAM_REQUEST_DATE" not in event_presence
            and "EXAM_REQUESTED" not in event_presence
            and "EXAM_REQUEST_DEADLINE" not in event_presence
        )
        if needs_exam_deadline_fallback:
            # USPTO-only: foreign/outgoing exam-request deadlines are jurisdiction-specific.
            if ctx.is_uspto and ctx.matter_type in ("PATENT", "UTILITY"):
                filing_dt = event_due_by_std_key.get("APPLICATION_DATE")
                if filing_dt:
                    fallback_due_by_std_key["EXAM_REQUEST_DEADLINE"] = _add_years(filing_dt, 3)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status.derive_auto_status.exam_deadline_fallback",
            log_key="matter_auto_status.derive_auto_status.exam_deadline_fallback",
            log_window_seconds=300,
        )
        fallback_due_by_std_key = {}

    # =========================================================================
    # Blue status priority: event presence, red status, manual value, default.
    # =========================================================================
    event_presence_for_blue = set(event_presence)
    event_due_for_blue = dict(event_due_by_std_key)
    if "EXAM_REQUEST_DEADLINE" in fallback_due_by_std_key:
        event_presence_for_blue.add("EXAM_REQUEST_DEADLINE")
        event_due_for_blue["EXAM_REQUEST_DEADLINE"] = fallback_due_by_std_key[
            "EXAM_REQUEST_DEADLINE"
        ]
    blue_from_events = normalize_blue_status(
        _derive_blue_from_events(
            mid,
            event_presence=event_presence_for_blue,
            event_due_by_std_key=event_due_for_blue,
            ctx=ctx,
            event_summary=event_summary,
            expired_deadlines=expired_deadlines,
        )
    )
    blue_from_red = normalize_blue_status(_suggest_blue_from_red_with_notice_fallback(mid, red))
    if preserve_future_term_expiry_red and blue_from_red == "Matter closed":
        blue_from_red = ""
    blue_from_notice_send = ""
    if _NOTICE_SEND_NAME_REF_RE.match((red or "").strip()):
        blue_from_notice_send = blue_from_red
    elif (not red) and mid:
        blue_from_notice_send = _pick_open_notice_send_blue_signal(mid)
    blue_from_open_oa = normalize_blue_status(_blue_from_open_office_action(open_oa_doc))
    terminal_blue_from_events = blue_from_events in {"Matter closed", "RegistrationDone"}

    blue = ""
    if preserve_manual_terminal_red:
        blue = "Matter closed"
    elif terminal_blue_from_events:
        blue = blue_from_events
    elif blue_from_open_oa:
        blue = blue_from_open_oa
    elif blue_from_notice_send and (
        (not blue_from_events) or (blue_from_events in _EVIDENCE_REQUIRED_BLUE_STATES)
    ):
        # NOTICE_SEND_3D is a near-term communication stage. When it is active, prefer its
        # semantic blue over generic pipeline states derived from legacy events.
        blue = blue_from_notice_send
    elif blue_from_events:
        blue = blue_from_events
    elif passive_stage_blue:
        blue = passive_stage_blue
    elif blue_from_red:
        blue = blue_from_red
    elif current_blue:
        current_blue_norm = normalize_blue_status(current_blue)
        # Guardrail: don't preserve stale pipeline states (e.g. "Filing Examination In Progress")
        # when there is no supporting signal left after edits/import cleanup.
        if current_blue_norm and not is_evidence_required_blue_status(current_blue_norm):
            blue = current_blue_norm

    if not blue:
        blue = _default_blue_for_matter(mid)
        if not blue and current_blue:
            blue = normalize_blue_status(current_blue)

    pending_post_filing = _collect_post_filing_pending_deadlines(
        event_presence,
        event_due_by_std_key,
        matter_id=mid,
        ctx=ctx,
        today=today,
        event_summary=event_summary,
        fallback_due_by_std_key=fallback_due_by_std_key,
        expired_deadlines=expired_deadlines,
    )

    display_red = _format_red_display(red, red_date, memo_txt)
    if pending_post_filing:
        extra_lines: list[str] = []
        for lbl, due in pending_post_filing:
            if lbl == red:
                continue
            extra_lines.append(f"{lbl}[{due.strftime('%Y-%m-%d')}]")
        if extra_lines:
            display_red = (
                "\n".join([display_red] + extra_lines) if display_red else "\n".join(extra_lines)
            )

    if mid:
        display_lines = [line for line in display_red.splitlines() if line.strip()]
        seen_display_lines = {_normalize_space(line) for line in display_lines}
        for due_dt, label in sorted(
            _collect_open_mgmt_status_red_deadline_candidates(mid, include_hidden=True),
            key=lambda item: (item[0], item[1]),
        ):
            line = f"{label}[{due_dt.strftime('%Y-%m-%d')}]"
            key = _normalize_space(line)
            if key in seen_display_lines:
                continue
            display_lines.append(line)
            seen_display_lines.add(key)
        if display_lines:
            display_red = "\n".join(display_lines)

    current_blue_norm = normalize_blue_status(current_blue)
    display_blue = _merge_blue_with_pending_post_filing(
        blue,
        pending_post_filing,
        preserve_primary_blue=(
            current_blue_norm in _PRIMARY_BLUE_STATES
            and normalize_blue_status(blue) == current_blue_norm
        ),
    )

    return AutoStatus(
        status_red=red,
        status_red_related_date=red_date,
        status_blue=blue,
        display_red=display_red,
        display_blue=display_blue,
    )
