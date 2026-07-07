from __future__ import annotations

import re
from datetime import date

from app.utils.docket_visibility import compute_visible_from


def _compact_status_red_label(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).upper()


_STATUS_RED_NAME_REF_PREFIX = "MGMT:STATUS_RED:"
_PASSIVE_STATUS_RED_EXACT = frozenset(
    {
        "ExaminationWaiting",
        "ExaminationIn Progress",
        "ExaminationIn Progress",
        "FilingExaminationIn Progress",
        "",
    }
)


def status_red_label_from_ref(
    *,
    name_ref: str | None,
    title: str | None = None,
) -> str:
    """Extract the user-visible label from an MGMT:STATUS_RED docket ref."""
    ref = (name_ref or "").strip()
    if ref.upper().startswith(_STATUS_RED_NAME_REF_PREFIX):
        return ref[len(_STATUS_RED_NAME_REF_PREFIX) :].strip()
    return (title or "").strip()


def is_non_action_status_red_label(value: str | None) -> bool:
    """Return True for passive case-stage labels that should not become work."""
    compact = _compact_status_red_label(value)
    if not compact:
        return False
    if compact in {_compact_status_red_label(v) for v in _PASSIVE_STATUS_RED_EXACT}:
        return True
    return compact.endswith("INPROGRESS") or compact.endswith("WAITING")


def is_non_action_status_red_ref(
    *,
    name_ref: str | None,
    title: str | None = None,
) -> bool:
    """Return True when an MGMT:STATUS_RED docket ref is a passive case state."""
    ref = (name_ref or "").strip()
    if not ref.upper().startswith(_STATUS_RED_NAME_REF_PREFIX):
        return False
    return is_non_action_status_red_label(status_red_label_from_ref(name_ref=name_ref, title=title))


def status_red_visibility_window(
    *,
    red_label: str | None,
    due_date: date | None,
    is_uspto_managed_case: bool = False,
) -> tuple[date | None, bool]:
    if due_date is None:
        return None, False

    compact = _compact_status_red_label(red_label)
    if compact in {"ForeignFilingDeadline".upper(), "ForeignFilingDeadline".upper()}:
        return compute_visible_from(due_date, months=-1), True
    if compact in {
        "PCTDomesticDeadline".upper(),
        ("PCTDomesticDeadline(30" + "items)").upper(),
        ("PCTDomesticDeadline(31" + "items)").upper(),
        "PCTNationalPhaseDeadline(30months)".upper(),
        "PCTNationalPhaseDeadline(31months)".upper(),
    }:
        return compute_visible_from(due_date, days=-120), True
    if (
        compact
        in {
            "Examination requestDeadline".upper(),
            "Examination requestDeadline".upper(),
            "Examination requestDue date".upper(),
        }
        and is_uspto_managed_case
    ):
        return compute_visible_from(due_date, months=-2), True

    return None, False


def is_status_red_visible(
    *,
    red_label: str | None,
    due_date: date | None,
    is_uspto_managed_case: bool = False,
    today: date | None = None,
) -> bool:
    if due_date is None:
        return True

    visible_from, has_window = status_red_visibility_window(
        red_label=red_label,
        due_date=due_date,
        is_uspto_managed_case=is_uspto_managed_case,
    )
    if not has_window:
        return True
    if visible_from is None:
        return True
    if today is None:
        today = date.today()
    return visible_from <= today
