from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception

_NOTICE_SEND_NAME_REF_RE = re.compile(r"^MGMT:NOTICE_SEND_3D:([^:]+)$", re.IGNORECASE)

_STATUS_MAP_CONFIG_KEYS = {
    "blue": "AUTO_STATUS_BLUE_CANONICAL_JSON",
    "red": "AUTO_STATUS_RED_CANONICAL_JSON",
}
_STATUS_RED_NAME_REF_PREFIX = "MGMT:STATUS_RED:"


def _normalize_space(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _get_json_config(key: str) -> object | None:
    if not has_app_context():
        return None
    return ConfigService.get_json(key, None)


def _coerce_status_map(raw: object | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = _normalize_space(str(k)) if k is not None else ""
        val = _normalize_space(str(v)) if v is not None else ""
        if key and val:
            out[key] = val
    return out


def _get_status_overrides(kind: str) -> dict[str, str]:
    if not has_app_context():
        return {}
    key = _STATUS_MAP_CONFIG_KEYS.get((kind or "").strip().lower())
    if not key:
        return {}
    raw = _get_json_config(key)
    return _coerce_status_map(raw)


def date_only_str(value: str | None) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    s = s.replace(".", "-").replace("/", "-")
    # Loose search for YYYY-MM-DD or YYYY-M-D anywhere in string
    m = re.search(r"(\d{4})[-.\s]+(\d{1,2})[-.\s]+(\d{1,2})", s)
    if m:
        try:
            yyyy = int(m.group(1))
            mm = int(m.group(2))
            dd = int(m.group(3))
            return date(yyyy, mm, dd).strftime("%Y-%m-%d")
        except ValueError as exc:
            try:
                from app.services.automation.parse_failure import record_parse_failure

                record_parse_failure(
                    kind="date",
                    raw_value=s,
                    error=str(exc),
                    source="matter_auto_status.date_only_str",
                )
            except Exception as log_exc:
                report_swallowed_exception(
                    log_exc,
                    context="matter_auto_status.date_only_str.record_parse_failure_value_error",
                    log_key="matter_auto_status.date_only_str.record_parse_failure_value_error",
                    log_window_seconds=300,
                )
            return ""
    # Also support compact YYYYMMDD format.
    m2 = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", s)
    if m2:
        try:
            yyyy = int(m2.group(1))
            mm = int(m2.group(2))
            dd = int(m2.group(3))
            return date(yyyy, mm, dd).strftime("%Y-%m-%d")
        except ValueError as exc:
            try:
                from app.services.automation.parse_failure import record_parse_failure

                record_parse_failure(
                    kind="date",
                    raw_value=s,
                    error=str(exc),
                    source="matter_auto_status.date_only_str",
                )
            except Exception as log_exc:
                report_swallowed_exception(
                    log_exc,
                    context="matter_auto_status.date_only_str.record_parse_failure_value_error",
                    log_key="matter_auto_status.date_only_str.record_parse_failure_value_error",
                    log_window_seconds=300,
                )
            return ""

    # If it "looks like a date" but we couldn't parse, record it (avoid noise on plain text).
    try:
        looks_dateish = (
            bool(re.search(r"\d{4}", s)) and any(ch in s for ch in (".", "-", "/"))
        ) or bool(re.search(r"\b\d{8}\b", s))
        if looks_dateish:
            from app.services.automation.parse_failure import record_parse_failure

            record_parse_failure(
                kind="date",
                raw_value=s,
                error="no_match",
                source="matter_auto_status.date_only_str",
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status.date_only_str.record_parse_failure_no_match",
            log_key="matter_auto_status.date_only_str.record_parse_failure_no_match",
            log_window_seconds=300,
        )
    return ""


def _parse_date(value: object) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        s_in = str(value).strip()
    except Exception:
        return None

    s = date_only_str(s_in)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception as exc:
        try:
            from app.services.automation.parse_failure import record_parse_failure

            record_parse_failure(
                kind="date",
                raw_value=s_in,
                normalized_value=s,
                error=str(exc),
                source="matter_auto_status._parse_date",
            )
        except Exception as log_exc:
            report_swallowed_exception(
                log_exc,
                context="matter_auto_status._parse_date.record_parse_failure",
                log_key="matter_auto_status._parse_date.record_parse_failure",
                log_window_seconds=300,
            )
        return None


def _add_years(base: date, years: int) -> date:
    try:
        return base.replace(year=base.year + int(years))
    except ValueError:
        # e.g. Feb 29 -> Feb 28
        return base.replace(month=2, day=28, year=base.year + int(years))


def _days_in_month(year: int, month: int) -> int:
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    # February
    is_leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
    return 29 if is_leap else 28


def _add_months(base: date, months: int) -> date:
    """
    Month offset helper without external deps (dateutil).

    Examples:
      - 2025-10-31 + 3M -> 2026-01-31
      - 2025-11-30 + 3M -> 2026-02-28
    """
    m0 = base.month - 1 + int(months)
    year = base.year + (m0 // 12)
    if year < date.min.year or year > date.max.year:
        raise ValueError(f"year out of range after month offset: {year}")
    month = (m0 % 12) + 1
    day = min(base.day, _days_in_month(year, month))
    return base.replace(year=year, month=month, day=day)


def _today() -> date:
    tzname = "America/New_York"
    if has_app_context():
        try:
            tzname = current_app.config.get("TIMEZONE", "America/New_York")
        except Exception:
            tzname = "America/New_York"
    try:
        return datetime.now(ZoneInfo(tzname)).date()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_auto_status._today",
            log_key="matter_auto_status._today",
            log_window_seconds=300,
        )
        return date.today()


_DEFAULT_BLUE_STATUS_CANONICAL: dict[str, str] = {
    # common spacing/typo variants
    "Matter  In Progress": "Matter In Progress",
    "OA In Progress": "OA  In Progress",
    "Filing In Progress": "Filing  In Progress",
    "FilingExamination In Progress": "Filing Examination In Progress",
    "Filing Examination In Progress": "Filing Examination In Progress",
    "ForeignFiling In Progress": "ForeignFiling  In Progress",
    "Examination Billing In Progress": "Examination  Billing In Progress",
    "  In Progress": "   In Progress",
    "    In Progress": "   In Progress",
    "  In Progress": "   In Progress",
    "Registration Done": "RegistrationDone",
    "Registration Waiting In Progress": "RegistrationWaiting In Progress",
    "PublicationIn Progress": "Filing Publication In Progress",
    "Filing PublicationIn Progress": "Filing Publication In Progress",
    "Filing Publication In Progress": "Filing Publication In Progress",
}
_DEFAULT_RED_STATUS_CANONICAL: dict[str, str] = {
    "Notice": "Notice",
    "  Notice": "Notice",
    "Examination request Deadline": "Examination requestDeadline",
    "Filing Deadline": "FilingDeadline",
    "Foreign Filing Deadline": "ForeignFilingDeadline",
}


def _strip_embedded_status_date(value: str) -> str:
    return re.sub(r"\[\d{4}[-./]\d{1,2}[-./]\d{1,2}.*\]$", "", value or "").strip()


def is_internal_mgmt_non_status_red_ref(value: str | None) -> bool:
    """Return True for internal MGMT task refs that should not be displayed as Red status."""
    raw = _strip_embedded_status_date(_normalize_space(value or ""))
    if not raw:
        return False
    raw_upper = raw.upper()
    if not raw_upper.startswith("MGMT:"):
        return False
    if raw_upper.startswith(_STATUS_RED_NAME_REF_PREFIX):
        return False
    if _NOTICE_SEND_NAME_REF_RE.match(raw):
        return False
    return True


_DOC_LIKE_RED_SUFFIXES: tuple[str, ...] = (
    # These are document titles (not actionable deadlines/office-actions) that were sometimes
    # incorrectly stored in Matter.status_red during imports or uploads.
    "Filing",
)

_DOC_LIKE_RED_EXACT: set[str] = {
    # document / submission document titles that should never be treated as "Red" status labels.
    "Examination request",
    "Examination",
    "",
    "",
    "",
    "",
    # KEAPS combined labels (sometimes appear verbatim)
    "··",
    "Examination request·Examination",
    # Generic
    "Department",
}

_DOC_LIKE_RED_EXACT_NOSPACE: set[str] = {x.replace(" ", "") for x in _DOC_LIKE_RED_EXACT}


def _looks_like_payment_notice_label(value: str | None) -> bool:
    """
    Detect payment/registration-fee notices that should not be treated as OA work.

    These titles often come from migration/import as generic "notice" rows and can
    carry long statutory dates (e.g. 5+ years), which should not drive OA status.
    """
    compact = _normalize_space(value or "").replace(" ", "")
    if not compact:
        return False

    if compact in {"Payment", "Department"}:
        return True
    if "Department" in compact:
        return True
    if "RegistrationPayment" in compact:
        return True
    if "SettingsRegistration" in compact and any(
        k and k in compact for k in ("Payment", "", "", "Guidance")
    ):
        return True
    return False


def _looks_like_oa_response_notice(value: str | None) -> bool:
    """
    Heuristic for notices that represent OA-style response work.

    Keep this intentionally narrow so non-response notices (e.g. /Guidance/target)
    do not get surfaced as "OA  In Progress".
    """
    doc = _normalize_space(value or "")
    if not doc:
        return False
    return any(
        token in doc
        for token in (
            "Notice",
            "Notice",
            "Notice",
            "",
            "",
            "",
            "",
            "Notice",
            "Notice",
            "Period",
            "Notice",
            "Notice",
        )
        if token
    )


def _looks_like_non_response_notice_label(value: str | None) -> bool:
    """
    Detect notices that should not be interpreted as OA response work.

    Keep this conservative: only block clearly non-response categories so
    legacy generic titles (e.g. OA1/OA2) still remain visible.
    """
    compact = _normalize_space(value or "").replace(" ", "")
    if not compact:
        return False

    if any(
        token.replace(" ", "") in compact
        for token in (
            "Period",
            "Period",
            "Period",
            "StatutoryPeriod",
            "StatutoryPeriod",
            "",
            "target",
            "target",
            "",
            "Publication decision",
            "PriorityDeadline",
        )
        if token.replace(" ", "")
    ):
        return True

    # Generic informational notices are non-action by default.
    if "Guidance" in compact or compact.endswith("Guidance"):
        return True
    return False


_TRIAL_PENDING_NOTICE_TOKENS: tuple[str, ...] = (
    "Notice",
    "Notice",
)
_TRIAL_PENDING_RESPONSE_TOKENS: tuple[str, ...] = (
    "Billing",
    "Billing",
    "trialfiled",
    "appealbrief",
)


def _looks_like_trial_pending_notice(value: str | None) -> bool:
    compact = _normalize_space(value or "").replace(" ", "")
    if not compact:
        return False
    return any(token in compact for token in _TRIAL_PENDING_NOTICE_TOKENS)


def _looks_like_trial_pending_response(value: str | None) -> bool:
    compact = _normalize_space(value or "").replace(" ", "").lower()
    if not compact:
        return False
    return any(token.lower() in compact for token in _TRIAL_PENDING_RESPONSE_TOKENS if token)


def normalize_blue_status(value: str | None) -> str:
    s = _normalize_space(value or "")
    if not s:
        return ""
    if s == "In Progress":
        return ""
    canonical = dict(_DEFAULT_BLUE_STATUS_CANONICAL)
    overrides = _get_status_overrides("blue")
    if overrides:
        canonical.update(overrides)
    return canonical.get(s, s)


def normalize_red_status(value: str | None) -> str:
    s = _normalize_space(value or "")
    if not s:
        return ""
    if s.upper().startswith(_STATUS_RED_NAME_REF_PREFIX):
        s = _normalize_space(s[len(_STATUS_RED_NAME_REF_PREFIX) :])
    canonical = dict(_DEFAULT_RED_STATUS_CANONICAL)
    overrides = _get_status_overrides("red")
    if overrides:
        canonical.update(overrides)
    s = canonical.get(s, s)
    return s


def _looks_like_non_red_document_title(value: str | None) -> bool:
    """
    Best-effort guardrail: some imported rows had `status_red` populated with document titles
    like 'PatentFiling'. These are not deadlines/OA labels and should not be kept as Red.
    """
    s = _normalize_space(value or "")
    if not s:
        return False

    # Remove a common date suffix format e.g. "RegistrationDeadline[2026-01-31]".
    s = re.sub(r"\[\d{4}[-./]\d{1,2}[-./]\d{1,2}.*\]$", "", s).strip()
    # Remove trailing annotations e.g. "PatentFiling ()".
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    if not s:
        return False

    compact = s.replace(" ", "")
    if compact in _DOC_LIKE_RED_EXACT_NOSPACE:
        return True
    # Payment-related notices are also non-action labels.
    if _looks_like_payment_notice_label(compact):
        return True
    return any(s.endswith(suffix) for suffix in _DOC_LIKE_RED_SUFFIXES)


def _is_candidate_office_action_doc(doc_name: str | None) -> bool:
    """
    Determine whether a document name should participate in OA status derivation.

    Document-like titles and payment notices (Department variants) are excluded.
    """
    doc = _normalize_space(doc_name or "")
    if not doc:
        return False
    if _looks_like_non_red_document_title(doc):
        return False
    if _looks_like_payment_notice_label(doc):
        return False
    if _looks_like_non_response_notice_label(doc):
        return False
    return True
