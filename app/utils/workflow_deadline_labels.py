from __future__ import annotations

import re
from datetime import date

_KNOWN_DEADLINE_LABELS = frozenset(
    {
        "Statutory deadline",
        "DraftDeadline",
        "Deadline",
        "Final",
        "Internal",
    }
)
_TRAILING_LABEL_RE = re.compile(r"\s*\[(?P<label>[^\[\]]+)\]\s*$")
_WF_DOCKET_KIND_RE = re.compile(
    r"^WF-\d+-(LEG|LEGAL|DRA|DRAFT|SUB|SUBMIT)(?:-|$)",
    re.IGNORECASE,
)
_WF_DOCKET_KIND_NORMALIZE = {
    "LEG": "LEG",
    "LEGAL": "LEG",
    "DRA": "DRA",
    "DRAFT": "DRA",
    "SUB": "SUB",
    "SUBMIT": "SUB",
}


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    raw = (str(value or "").strip() if value is not None else "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def workflow_deadline_kind_from_docket_id(docket_id: str | None) -> str | None:
    raw = (docket_id or "").strip()
    if not raw:
        return None
    match = _WF_DOCKET_KIND_RE.match(raw)
    if not match:
        return None
    return _WF_DOCKET_KIND_NORMALIZE.get(str(match.group(1) or "").upper())


def strip_workflow_deadline_title_suffix(title: str | None) -> str:
    value = str(title or "").strip()
    while value:
        match = _TRAILING_LABEL_RE.search(value)
        if not match:
            break
        label = str(match.group("label") or "").strip()
        if label not in _KNOWN_DEADLINE_LABELS:
            break
        value = value[: match.start()].rstrip()
    return value


def workflow_deadline_label(
    kind: str | None,
    *,
    legal_due_date: object = None,
    effective_due_date: object = None,
) -> str | None:
    key = (kind or "").strip().upper()
    if not key:
        return None
    if key == "LEG":
        legal_due = _as_date(legal_due_date)
        effective_due = _as_date(effective_due_date)
        if not legal_due and not effective_due:
            return None
        if legal_due and effective_due and legal_due != effective_due:
            return "Internal"
        return "Final"
    if key in {"DRA", "SUB"}:
        return "Internal"
    return None


def workflow_deadline_title(
    base_title: str | None,
    kind: str | None,
    *,
    legal_due_date: object = None,
    effective_due_date: object = None,
) -> str:
    base = strip_workflow_deadline_title_suffix(base_title) or "Task"
    label = workflow_deadline_label(
        kind,
        legal_due_date=legal_due_date,
        effective_due_date=effective_due_date,
    )
    if not label:
        return base
    return f"{base} [{label}]"
