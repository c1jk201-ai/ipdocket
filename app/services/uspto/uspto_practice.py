"""USPTO practice-oriented document analysis."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from dateutil.relativedelta import relativedelta

from app.services.uspto.uspto_form_parser import (
    UsptoFormParseResult,
    infer_uspto_doc_type,
    looks_like_uspto_form,
    parse_uspto_form_rule_based,
)


@dataclass(frozen=True)
class UsptoPracticeDeadline:
    kind: str
    label: str
    trigger_date: str
    due_date: str
    statutory_due_date: str = ""
    extendable: bool = False
    source: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UsptoDocumentAnalysis:
    doc_type: str
    matter_kind: str
    task_type: str
    confidence: str
    fields: dict[str, str]
    deadline: UsptoPracticeDeadline | None = None
    warnings: tuple[str, ...] = ()
    evidence: tuple[dict[str, str], ...] = ()
    parser: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["deadline"] = self.deadline.to_dict() if self.deadline else None
        return data


_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%B %d %Y",
    "%b %d %Y",
)

_PRACTICE_DOC_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("USPTO Information Disclosure Statement", ("information disclosure statement", "pto/sb/08")),
    ("USPTO Advisory Action", ("advisory action",)),
    ("USPTO Restriction Requirement", ("restriction requirement", "election of species")),
    ("USPTO Notice of Allowance", ("notice of allowance", "ptol-85")),
    ("USPTO Non-Final Office Action", ("non-final office action", "non final office action")),
    ("USPTO Final Office Action", ("final office action", "final rejection")),
    ("USPTO Office Action", ("office action", "office communication")),
    ("USPTO Issue Notification", ("issue notification",)),
)

_TASK_TYPES = {
    "USPTO Filing Receipt": "US filing receipt review",
    "USPTO Application Data Sheet": "US application data review",
    "USPTO Information Disclosure Statement": "US IDS review",
    "USPTO Advisory Action": "US advisory action review",
    "USPTO Restriction Requirement": "US restriction requirement response",
    "USPTO Notice of Allowance": "US issue fee payment",
    "USPTO Final Office Action": "US final office action response",
    "USPTO Non-Final Office Action": "US non-final office action response",
    "USPTO Office Action": "US office action response",
    "USPTO Issue Notification": "US issue notification review",
    "USPTO TEAS Form": "US trademark filing receipt review",
}


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).strip(" .,:;")


def _normalize_date(value: str | None) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    raw = raw.replace(",", " ")
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", raw)
    if m:
        month, day, year = m.groups()
        if len(year) == 2:
            year = "20" + year if int(year) < 70 else "19" + year
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return ""
    return ""


def _add_months(iso_date: str, months: int) -> str:
    try:
        base = date.fromisoformat(iso_date)
    except ValueError:
        return ""
    return (base + relativedelta(months=months)).isoformat()


def _extract_labeled_date(text: str, labels: tuple[str, ...]) -> str:
    lines = [_clean(line) for line in (text or "").splitlines() if _clean(line)]
    date_pattern = (
        r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|"
        r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|"
        r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
    )
    for idx, line in enumerate(lines):
        for label in labels:
            same_line = re.search(
                rf"(?:^|\b){label}\s*(?:[:#-]|\s{{2,}})\s*{date_pattern}",
                line,
                re.IGNORECASE,
            )
            if same_line:
                normalized = _normalize_date(same_line.group(1))
                if normalized:
                    return normalized
            if re.fullmatch(rf"{label}\s*[:#-]?", line, flags=re.IGNORECASE):
                for nxt in lines[idx + 1 : idx + 3]:
                    normalized = _normalize_date(nxt)
                    if normalized:
                        return normalized
    return ""


def infer_uspto_practice_doc_type(text: str, *, filename: str = "") -> str:
    haystack = f"{filename}\n{text}".lower()
    for doc_type, needles in _PRACTICE_DOC_CHECKS:
        if any(needle in haystack for needle in needles):
            return doc_type
    return infer_uspto_doc_type(text or "", filename=filename) or (
        "USPTO Document" if looks_like_uspto_form(text or "", filename=filename) else ""
    )


def _deadline_for_doc_type(doc_type: str, text: str) -> UsptoPracticeDeadline | None:
    mail_date = _extract_labeled_date(
        text,
        (
            r"mail\s+date",
            r"mailing\s+date",
            r"notification\s+date",
            r"date\s+mailed",
            r"office\s+action\s+mailed",
        ),
    )

    if doc_type in {
        "USPTO Final Office Action",
        "USPTO Non-Final Office Action",
        "USPTO Office Action",
    }:
        if not mail_date:
            return None
        return UsptoPracticeDeadline(
            kind="office_action_response",
            label="Response to USPTO Office Action",
            trigger_date=mail_date,
            due_date=_add_months(mail_date, 3),
            statutory_due_date=_add_months(mail_date, 6),
            extendable=True,
            source="MPEP 710 and 710.01(a)",
            notes="Default USPTO patent practice: 3-month shortened statutory period, up to 6-month statutory limit unless the action states otherwise.",
        )

    if doc_type == "USPTO Notice of Allowance":
        trigger = mail_date or _extract_labeled_date(
            text,
            (r"notice\s+of\s+allowance\s+date", r"allowance\s+date"),
        )
        explicit_due = _extract_labeled_date(
            text,
            (r"issue\s+fee\s+due(?:\s+date)?", r"fee(?:s)?\s+due(?:\s+date)?"),
        )
        due = explicit_due or (_add_months(trigger, 3) if trigger else "")
        if not trigger or not due:
            return None
        return UsptoPracticeDeadline(
            kind="issue_fee",
            label="Pay USPTO issue fee",
            trigger_date=trigger,
            due_date=due,
            statutory_due_date=due,
            extendable=False,
            source="MPEP 1306",
            notes="Issue fee and any required publication fee are due 3 months from the Notice of Allowance and the period is not extendable.",
        )

    return None


def _warnings_for_doc_type(doc_type: str, deadline: UsptoPracticeDeadline | None) -> tuple[str, ...]:
    warnings: list[str] = []
    if doc_type == "USPTO Information Disclosure Statement":
        warnings.append(
            "IDS timing depends on prosecution stage, issue-fee status, certification, and fee requirements; no automatic due date was created."
        )
    if doc_type.endswith("Office Action") and not deadline:
        warnings.append("Office Action detected, but no mail date was found for deadline calculation.")
    if doc_type == "USPTO Notice of Allowance" and not deadline:
        warnings.append("Notice of Allowance detected, but no mail date or issue-fee due date was found.")
    return tuple(warnings)


def _evidence_from_fields(parsed: UsptoFormParseResult, mail_date: str) -> tuple[dict[str, str], ...]:
    evidence: list[dict[str, str]] = []
    for field, value in parsed.to_dict().items():
        if field == "parser" or not value:
            continue
        evidence.append({"field": field, "value": str(value), "source": "USPTO text"})
    if mail_date:
        evidence.append({"field": "mail_date", "value": mail_date, "source": "USPTO text"})
    return tuple(evidence)


def analyze_uspto_document_text(text: str, *, filename: str = "") -> UsptoDocumentAnalysis:
    raw = text or ""
    parsed = parse_uspto_form_rule_based(raw, filename=filename)
    doc_type = infer_uspto_practice_doc_type(raw, filename=filename) or parsed.doc_type
    if not doc_type and parsed.has_core_identifier:
        doc_type = "USPTO Document"

    deadline = _deadline_for_doc_type(doc_type, raw)
    mail_date = _extract_labeled_date(
        raw,
        (
            r"mail\s+date",
            r"mailing\s+date",
            r"notification\s+date",
            r"date\s+mailed",
            r"office\s+action\s+mailed",
        ),
    )
    fields = parsed.to_dict()
    fields["doc_type"] = doc_type
    fields["mail_date"] = mail_date
    if deadline:
        fields["deadline_kind"] = deadline.kind
        fields["deadline_due_date"] = deadline.due_date

    confidence = "LOW"
    if doc_type and (parsed.has_core_identifier or deadline):
        confidence = "HIGH"
    elif doc_type:
        confidence = "MEDIUM"

    return UsptoDocumentAnalysis(
        doc_type=doc_type,
        matter_kind=parsed.matter_kind,
        task_type=_TASK_TYPES.get(doc_type, "US document review" if doc_type else ""),
        confidence=confidence,
        fields={key: str(value or "") for key, value in fields.items()},
        deadline=deadline,
        warnings=_warnings_for_doc_type(doc_type, deadline),
        evidence=_evidence_from_fields(parsed, mail_date),
        parser=parsed.parser,
    )
