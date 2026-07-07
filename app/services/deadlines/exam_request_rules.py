from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from app.services.case.case_kind import resolve_profile_case_kind

_COUNTRY_ALIASES: dict[str, str] = {
    "USA": "US",
    "U.S.": "US",
    "U S": "US",
    "UNITEDSTATES": "US",
    "UNITEDSTATESOFAMERICA": "US",
    "": "US",
}

_YES_TOKENS: set[str] = {"Y", "YES", "TRUE", "1", "T"}


def normalize_country_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    raw = raw.replace("-", "").replace("_", "").replace(" ", "")
    return _COUNTRY_ALIASES.get(raw, raw)


def apply_exam_request_date_default_when_requested(
    data: MutableMapping[str, Any],
    *,
    allowed_keys: set[str] | None = None,
) -> None:
    """
    If exam is requested and exam_request_date is empty, default it to application_date.

    This rule is intentionally division/type-agnostic and runs as a safe fallback so that
    UI payload differences do not leave requested-exam cases without a done date.
    """
    if allowed_keys is not None:
        if "exam_requested" not in allowed_keys:
            return
        if "exam_request_date" not in allowed_keys:
            return

    exam_requested = str(data.get("exam_requested") or "").strip().upper()
    if exam_requested not in _YES_TOKENS:
        return

    app_dt = str(data.get("application_date") or "").strip()
    ex_dt = str(data.get("exam_request_date") or "").strip()
    if app_dt and not ex_dt:
        data["exam_request_date"] = app_dt


def apply_out_exam_request_defaults(
    data: MutableMapping[str, Any],
    *,
    division: str,
    case_type: str,
    allowed_keys: set[str] | None = None,
) -> None:
    """
    Apply defaults/constraints for OUT cases regarding examination request.

    Rules (as requested):
    - OUT (Patent/Trademark/Design) cases default to exam requested.
    - US: exam "Billing" is not allowed (filing = exam request).

    Side-effect:
    - If exam is requested and `application_date` exists but `exam_request_date` is empty,
      default `exam_request_date` to `application_date` (keeps UX consistent and prevents
      "Examination  Billing In Progress" for cases treated as filing=exam request).
    """
    div, typ = resolve_profile_case_kind(division, case_type)

    if div != "OUT":
        return
    if typ not in {"PATENT", "UTILITY", "DESIGN", "TRADEMARK"}:
        return

    # Only act when the field is actually part of this case profile (prevents accidental pollution).
    if allowed_keys is not None and "exam_requested" not in allowed_keys:
        return

    country = normalize_country_code(data.get("application_country"))

    exam_requested = str(data.get("exam_requested") or "").strip().upper()
    if not exam_requested:
        exam_requested = "Y"

    # US: disallow "Billing"
    if country == "US":
        exam_requested = "Y"

    data["exam_requested"] = exam_requested

    apply_exam_request_date_default_when_requested(data, allowed_keys=allowed_keys)
