from __future__ import annotations

import re

_ANNUITY_STATUS_RED_LABEL_RE = re.compile(r"^[0-9]+(?:RenewalDeadline|Text)$")


def _compact_text(value: str | None) -> str:
    return "".join(str(value or "").split())


def _status_red_label_from_values(*, name_ref: str | None, title: str | None) -> str:
    title_clean = str(title or "").strip()
    if title_clean:
        return title_clean
    ref = str(name_ref or "").strip()
    marker = "MGMT:STATUS_RED:"
    if ref.upper().startswith(marker):
        return ref[len(marker) :].strip()
    return ref


def is_annuity_status_red_label(label: str | None) -> bool:
    compact_label = _compact_text(label)
    if not compact_label:
        return False
    return bool(_ANNUITY_STATUS_RED_LABEL_RE.match(compact_label))


def is_annuity_status_red_deadline(*, name_ref: str | None, title: str | None = None) -> bool:
    """
    True when the docket row is a status-red auto deadline representing an annuity due
    label such as "4RenewalDeadline".
    """
    compact_ref = _compact_text(name_ref).upper()
    if not compact_ref.startswith("MGMT:STATUS_RED:"):
        return False
    label = _status_red_label_from_values(name_ref=name_ref, title=title)
    return is_annuity_status_red_label(label)


def calendar_endpoint_for_docket(*, name_ref: str | None, title: str | None = None) -> str:
    if is_annuity_status_red_deadline(name_ref=name_ref, title=title):
        return "annuities.calendar_month"
    return "deadlines.calendar_month"
