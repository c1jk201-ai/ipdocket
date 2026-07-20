from __future__ import annotations

import re
from typing import Any


_PLACEHOLDER_EXACT = {
    "text",
    "text text",
    "text/text",
    "text/text text",
    "text(1)",
    "text(2)",
    "select",
    "yyyy-mm-dd",
}

_WORD_RE = re.compile(r"[A-Za-z]+|\d+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")

_LABEL_OVERRIDES = {
    "__blank__": "",
    "app_route": "Application route",
    "applicant_contact": "Applicant contact",
    "applicant_name": "Applicant",
    "application_agent": "Filing representative",
    "application_applicant_customer_no": "Applicant customer No.",
    "application_applicant_name": "Filing applicant",
    "application_classes": "Filing classes",
    "application_country": "Application country",
    "application_date": "Filing date",
    "application_goods": "Filing goods/services",
    "application_no": "Application No.",
    "application_reg_date": "Application/registration date",
    "application_reg_no": "Application/registration No.",
    "assignee1": "Assignee 1",
    "assignee2": "Assignee 2",
    "attorney": "Responsible attorney",
    "client_contact": "Client contact",
    "client_mgmt_no": "Client management No.",
    "client_name": "Client",
    "department": "Department",
    "drawing_handler": "Drawing handler",
    "entered_at": "Entry date",
    "filing_deadline": "Filing deadline",
    "filing_deadline_type": "Filing deadline type",
    "filing_type": "Filing type",
    "handler": "Handler",
    "image": "Image",
    "inhouse_status": "Internal status",
    "inventor_name": "Inventor",
    "invention_grade": "Invention grade",
    "manager": "Docketing owner",
    "matter_id": "Matter ID",
    "matter_type": "Matter type",
    "memo": "Memo",
    "old_our_ref": "Former Our Ref.",
    "our_ref": "Our Ref.",
    "priority_claimed": "Priority claimed",
    "priority_date": "Priority date",
    "priority_no": "Priority No.",
    "raw_id": "Raw ID",
    "retained_at": "Intake date",
    "retained_date": "Engagement date",
    "right_group": "Matter division",
    "right_name": "Matter title",
    "stand_reason": "Waiting reason",
    "status_blue": "Status blue",
    "status_red": "Status red",
    "status_red_related_date": "Status red related date",
    "title_en": "Title (English)",
    "tm_name": "Trademark",
    "tm_registration_payment_term": "Trademark registration payment term",
    "tm_right_type": "Trademark right type",
    "tm_type": "Trademark type",
    "your_ref": "Your Ref.",
}

_TOKEN_OVERRIDES = {
    "app": "application",
    "ctm": "CTM",
    "ep": "EP",
    "exam": "examination",
    "id": "ID",
    "inc": "inbound",
    "intl": "international",
    "mgmt": "management",
    "no": "No.",
    "oa": "OA",
    "ol": "O/L",
    "pct": "PCT",
    "reg": "registration",
    "tm": "trademark",
    "yn": "yes/no",
}

_PLACEHOLDER_WORDS = {
    "text",
    "select",
    "yyyy",
    "mm",
    "dd",
}


def _split_key_tokens(key: str) -> list[str]:
    normalized = _CAMEL_RE.sub("_", str(key or ""))
    normalized = re.sub(r"(?<=[A-Za-z])(?=\d)", "_", normalized)
    normalized = re.sub(r"(?<=\d)(?=[A-Za-z])", "_", normalized)
    return [token for token in re.split(r"[^A-Za-z0-9]+", normalized) if token]


def humanize_field_key(key: str) -> str:
    key = str(key or "").strip()
    if not key:
        return ""
    if key in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[key]

    words: list[str] = []
    for token in _split_key_tokens(key):
        lower = token.lower()
        replacement = _TOKEN_OVERRIDES.get(lower)
        if replacement:
            words.append(replacement)
        else:
            words.append(lower)

    if not words:
        return key

    label = " ".join(words)
    return label[:1].upper() + label[1:]


def is_placeholder_label(key: str, label: Any) -> bool:
    key = str(key or "").strip()
    if key == "__blank__":
        return False

    text = str(label or "").strip()
    if not text:
        return True

    compact = re.sub(r"\s+", " ", text).strip().lower()
    if compact in _PLACEHOLDER_EXACT:
        return True

    if not any(marker in compact for marker in ("text", "select", "yyyy", "mm", "dd")):
        return False

    stripped = re.sub(r"text|select|yyyy|mm|dd", " ", text, flags=re.IGNORECASE)
    meaningful = [
        word
        for word in _WORD_RE.findall(stripped)
        if len(word) > 2 and word.lower() not in _PLACEHOLDER_WORDS
    ]
    return not meaningful


def coerce_field_label(key: str, label: Any) -> str:
    if is_placeholder_label(key, label):
        return humanize_field_key(key)
    return str(label or "").strip()
