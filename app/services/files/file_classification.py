from __future__ import annotations

from pathlib import Path

DOC_TYPE_LABELS = {
    "OFFICE_ACTION": "Notice/OA",
    "SPEC": "/",
    "POA": "",
    "INVOICE": "Billing",
    "RECEIPT": "",
    "TRANSLATION": "",
    "EVIDENCE": "",
    "OTHER": "Other",
}


_DOC_TYPE_RULES: list[tuple[str, list[str]]] = [
    (
        "OFFICE_ACTION",
        ["", "notification", "office action", "officeaction", "oa", "decision", "Notice"],
    ),
    ("POA", ["", "poa", "power of attorney"]),
    ("INVOICE", ["invoice", "Billing"]),
    ("RECEIPT", ["receipt", ""]),
    ("SPEC", ["spec", "", "claims", ""]),
    ("TRANSLATION", ["translation", ""]),
    ("EVIDENCE", ["evidence", ""]),
]


def classify_doc_type(filename: str | None) -> tuple[str, list[str]]:
    name = (filename or "").strip()
    lowered = name.lower()
    for doc_type, keywords in _DOC_TYPE_RULES:
        if any(k in lowered for k in keywords):
            return doc_type, ["AUTO_CLASSIFIED"]
    return "OTHER", ["AUTO_CLASSIFIED"]


def is_previewable(filename: str | None, mime_type: str | None) -> bool:
    mt = (mime_type or "").lower()
    if mt.startswith("image/") and mt != "image/svg+xml":
        return True
    if "pdf" in mt:
        return True
    ext = (Path(filename or "").suffix or "").lower()
    return ext in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
