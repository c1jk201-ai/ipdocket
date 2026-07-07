from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Any, Iterable

try:
    from openai import OpenAI, OpenAIError
except ImportError:
    OpenAI = None
    OpenAIError = Exception

from flask import current_app, has_app_context
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.cited_reference import CitedReference
from app.models.matter import Matter, MatterCustomField, MatterFamily
from app.models.workflow import Workflow
from app.services.core.config_service import ConfigService
from app.services.core.llm_model_registry import resolve_llm_model
from app.services.core.llm_runtime import get_openai_api_key
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.timezone import today_local, utcnow_naive
from app.utils.workflow_semantics import derive_workflow_category

_WS_RE = re.compile(r"\s+")
_PAGE_TOKEN_RE = re.compile(r"\b(?:\d{2}-\d{4}-\d{6,8}|10-\d{4}-\d{6,8})\s+\d+/\d+\b")
_DATE_RE = re.compile(
    r"\((\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})(?:\.)?(?:\s*[^)]*)?\)"
)
_YEAR_PAREN_RE = re.compile(r"\(\d{4}\)")
_INLINE_DATE_RE = re.compile(r"\b\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\.?\b")
_LABEL_RE = re.compile(
    r"(?:^|\s)[^A-Za-z0-9]{0,8}"
    r"("
    r"(?:Reference|Ref\.?|Citation|Patent|Publication|Design|Trademark|Text)\s*\d+"
    r")\s*[:;]\s*",
    re.IGNORECASE,
)
_PUB_PATTERNS = (
    re.compile(r"\bUS\s?[0-9]{4}/[0-9]{6,7}(?!\d)", re.IGNORECASE),
    re.compile(r"\bUS\s?[0-9]{6,11}(?!\d)", re.IGNORECASE),
    re.compile(r"\bWO\s?[0-9]{4}/[0-9]{5,7}(?!\d)", re.IGNORECASE),
    re.compile(r"\bEP\s?[0-9]{6,8}(?!\d)", re.IGNORECASE),
    re.compile(r"\bJP\s?[0-9]{4}-[0-9]{5,7}(?!\d)", re.IGNORECASE),
    re.compile(r"(?<!\d)10-[0-9]{4}-[0-9]{6,8}(?!\d)"),
    re.compile(r"(?<!\d)20-[0-9]{4}-[0-9]{6,8}(?!\d)"),
    re.compile(r"(?<!\d)10-[0-9]{6,8}(?!\d)"),
)
_PATENT_HINTS = (
    "Patent",
    "Published Application",
    "Publication",
    "FilingPublication",
    "Registration",
)
_PATENT_NUMBER_HINT_RE = re.compile(
    r"\b(?:US|WO|EP|JP)\s?[0-9]|(?<!\d)(?:10|20)-\d",
    re.IGNORECASE,
)
_PDF_CITATION_HARD_MAX_PAGES = 2
_AI_MAX_CHARS_DEFAULT = 8000
_AI_SYSTEM_PROMPT = """
Extract cited references from USPTO office action text.

Return only cited-reference entries such as patents, published applications,
trademarks, designs, and non-patent literature. Ignore billing, docketing,
applicant, representative, correspondence, and argument body text.

Rules:
- Put one cited reference in each references item.
- raw_text is required.
- publication_number, country, and published_date may be blank if unavailable.
- Normalize published_date to YYYY-MM-DD when present.
- Use ref_type patent, non_patent, trademark, design, or unknown.
""".strip()
_AI_CITATION_JSON_SCHEMA = {
    "name": "OaCitedReferences",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["references"],
        "properties": {
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "label",
                        "ref_type",
                        "country",
                        "publication_number",
                        "published_date",
                        "title",
                        "raw_text",
                    ],
                    "properties": {
                        "label": {"type": "string"},
                        "ref_type": {"type": "string"},
                        "country": {"type": "string"},
                        "publication_number": {"type": "string"},
                        "published_date": {"type": "string"},
                        "title": {"type": "string"},
                        "raw_text": {"type": "string"},
                    },
                },
            }
        },
    },
    "strict": True,
}


@dataclass(frozen=True)
class CitationDraft:
    raw_text: str
    label: str | None = None
    ref_type: str | None = None
    country: str | None = None
    publication_number: str | None = None
    published_date: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class IdsTaskResult:
    created_count: int = 0
    updated_count: int = 0
    workflow_ids: tuple[int, ...] = ()


def _clean_text(value: str | None) -> str:
    text = (value or "").replace("\u00a0", " ")
    text = _PAGE_TOKEN_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _normalize_key(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).casefold()


def _published_date(raw: str) -> str | None:
    match = _DATE_RE.search(raw or "")
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _publication_number(raw: str) -> str | None:
    text_value = raw or ""
    for pattern in _PUB_PATTERNS:
        match = pattern.search(text_value)
        if match:
            value = _WS_RE.sub("", match.group(0))
            return value.strip()
    return None


def _country(raw: str, publication_number: str | None) -> str | None:
    text_upper = (raw or "").upper()
    pub_upper = (publication_number or "").upper()
    if pub_upper.startswith("US") or "UNITED STATES" in text_upper:
        return "US"
    if pub_upper.startswith("WO"):
        return "WO"
    if pub_upper.startswith("EP"):
        return "EP"
    if pub_upper.startswith("JP"):
        return "JP"
    return None


def _ref_type(raw: str) -> str:
    if any(hint in (raw or "") for hint in _PATENT_HINTS):
        return "patent"
    if _PATENT_NUMBER_HINT_RE.search(raw or ""):
        return "patent"
    return "non_patent"


def _trim_citation_body(raw: str) -> str:
    text_value = _clean_text(raw)
    if not text_value:
        return ""

    date_match = _DATE_RE.search(text_value)
    if date_match:
        return text_value[: date_match.end()].rstrip(" .;")

    year_match = _YEAR_PAREN_RE.search(text_value)
    if year_match:
        return text_value[: year_match.end()].rstrip(" .;")

    for marker in (". o", ".o", ".O", ". Text ", ".Text ", " Remarks ", " Arguments "):
        idx = text_value.find(marker)
        if idx > 0:
            return text_value[: idx + 1].strip()

    inline_date_match = _INLINE_DATE_RE.search(text_value)
    if inline_date_match:
        next_sentence = text_value.find(". ", inline_date_match.end())
        if next_sentence > 0:
            return text_value[: next_sentence + 1].strip()

    return text_value.strip()


def _draft_from_parts(label: str | None, raw: str) -> CitationDraft | None:
    raw_clean = _trim_citation_body(raw)
    if not raw_clean:
        return None
    pub = _publication_number(raw_clean)
    return CitationDraft(
        raw_text=raw_clean,
        label=_clean_text(label),
        ref_type=_ref_type(raw_clean),
        country=_country(raw_clean, pub),
        publication_number=pub,
        published_date=_published_date(raw_clean),
    )


def parse_citations_from_text(
    text_value: str | None,
    *,
    allow_unlabeled_lines: bool = False,
) -> list[CitationDraft]:
    text_clean = _clean_text(text_value)
    if not text_clean:
        return []

    matches = list(_LABEL_RE.finditer(text_clean))
    drafts: list[CitationDraft] = []
    if matches:
        for idx, match in enumerate(matches):
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text_clean)
            raw = text_clean[start:end]
            draft = _draft_from_parts(match.group(1), raw)
            if draft:
                drafts.append(draft)
        return _dedupe_drafts(drafts)

    if not allow_unlabeled_lines:
        return []

    # Manual-input fallback: one reference per non-empty line.
    lines = [_clean_text(line) for line in (text_value or "").splitlines()]
    for idx, line in enumerate([line for line in lines if line], start=1):
        label = None
        raw = line
        match = re.match(r"^(.{1,40}?):\s*(.+)$", line)
        if match:
            label = match.group(1).strip()
            raw = match.group(2).strip()
        draft = _draft_from_parts(label or f"Manual {idx}", raw)
        if draft:
            drafts.append(draft)
    return _dedupe_drafts(drafts)


def parse_manual_citations(text_value: str | None) -> list[CitationDraft]:
    return parse_citations_from_text(text_value, allow_unlabeled_lines=True)


def _coerce_pdf_page_limit(max_pages: int | None) -> int:
    configured = ConfigService.get_int(
        "OA_CITATION_PDF_MAX_PAGES",
        _PDF_CITATION_HARD_MAX_PAGES,
        min_value=1,
        max_value=_PDF_CITATION_HARD_MAX_PAGES,
    )
    try:
        requested = int(max_pages or configured or _PDF_CITATION_HARD_MAX_PAGES)
    except (TypeError, ValueError):
        requested = _PDF_CITATION_HARD_MAX_PAGES
    return max(1, min(requested, int(configured or 1), _PDF_CITATION_HARD_MAX_PAGES))


def _extract_pdf_text_limited(
    pdf_bytes: bytes | None,
    *,
    max_pages: int | None,
    max_bytes: int | None,
) -> str:
    if not pdf_bytes:
        return ""
    if max_bytes is None:
        max_bytes = ConfigService.get_int(
            "OA_CITATION_PDF_MAX_BYTES", 25 * 1024 * 1024, min_value=1
        )
    if max_bytes and len(pdf_bytes) > int(max_bytes):
        return ""
    if b"%PDF-" not in pdf_bytes[:1024]:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        report_swallowed_exception(
            exc,
            context="cited_reference_service.extract_pdf.import_pypdf",
            log_key="cited_reference_service.extract_pdf.import_pypdf",
            log_window_seconds=300,
        )
        return ""

    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        chunks: list[str] = []
        page_limit = _coerce_pdf_page_limit(max_pages)
        for page in list(reader.pages)[:page_limit]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="cited_reference_service.extract_pdf.page_text",
                    log_key="cited_reference_service.extract_pdf.page_text",
                    log_window_seconds=300,
                )
        return "\n".join(chunks)
    except Exception as exc:
        if has_app_context():
            current_app.logger.info("OA citation PDF text extraction skipped: %s", exc)
        return ""


def parse_ai_citations_from_text(text_value: str | None) -> list[CitationDraft]:
    text_clean = _clean_text(text_value)
    if not text_clean:
        return []
    if not ConfigService.get_bool("OA_CITATION_AI_ENABLED", True):
        return []
    if OpenAI is None:
        return []
    api_key = get_openai_api_key()
    if not api_key:
        return []

    max_chars = ConfigService.get_int(
        "OA_CITATION_AI_MAX_CHARS",
        _AI_MAX_CHARS_DEFAULT,
        min_value=500,
        max_value=20000,
    )
    model = resolve_llm_model("default")

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _AI_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Extract cited references from the first pages of this office action as JSON.\n\n"
                        f"{text_clean[: int(max_chars or _AI_MAX_CHARS_DEFAULT)]}"
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": _AI_CITATION_JSON_SCHEMA,
            },
            temperature=0,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except (
        OpenAIError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        json.JSONDecodeError,
    ) as exc:
        report_swallowed_exception(
            exc,
            context="cited_reference_service.parse_ai_citations_from_text",
            log_key="cited_reference_service.parse_ai_citations_from_text",
            log_window_seconds=300,
        )
        return []

    refs = payload.get("references") if isinstance(payload, dict) else None
    if not isinstance(refs, list):
        return []
    return drafts_from_payload(refs)


def extract_citations_from_pdf_bytes(
    pdf_bytes: bytes | None,
    *,
    max_pages: int = _PDF_CITATION_HARD_MAX_PAGES,
    max_bytes: int | None = None,
) -> list[CitationDraft]:
    text_value = _extract_pdf_text_limited(
        pdf_bytes,
        max_pages=max_pages,
        max_bytes=max_bytes,
    )
    if not text_value:
        return []
    ai_drafts = parse_ai_citations_from_text(text_value)
    rule_drafts = parse_citations_from_text(text_value)
    return _dedupe_drafts([*ai_drafts, *rule_drafts])


def _citation_draft_from_payload_item(item: dict[str, Any]) -> CitationDraft | None:
    raw = _clean_text(str(item.get("raw_text") or ""))
    if not raw:
        return None
    return CitationDraft(
        raw_text=raw,
        label=_clean_text(str(item.get("label") or "")) or None,
        ref_type=_clean_text(str(item.get("ref_type") or "")) or None,
        country=_clean_text(str(item.get("country") or "")) or None,
        publication_number=_clean_text(str(item.get("publication_number") or "")) or None,
        published_date=_clean_text(str(item.get("published_date") or "")) or None,
        title=_clean_text(str(item.get("title") or "")) or None,
    )


def _dedupe_drafts(drafts: Iterable[CitationDraft]) -> list[CitationDraft]:
    out: list[CitationDraft] = []
    seen: set[str] = set()
    for draft in drafts or []:
        key = _normalize_key(draft.publication_number or draft.raw_text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(draft)
    return out


def drafts_to_payload(drafts: Iterable[CitationDraft]) -> list[dict[str, str | None]]:
    return [
        {
            "raw_text": draft.raw_text,
            "label": draft.label,
            "ref_type": draft.ref_type,
            "country": draft.country,
            "publication_number": draft.publication_number,
            "published_date": draft.published_date,
            "title": draft.title,
        }
        for draft in drafts or []
    ]


def drafts_from_payload(payload: object) -> list[CitationDraft]:
    if not isinstance(payload, list):
        return []
    drafts: list[CitationDraft] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        draft = _citation_draft_from_payload_item(item)
        if draft:
            drafts.append(draft)
    return _dedupe_drafts(drafts)


def _replace_citations(
    *,
    matter_id: str,
    drafts: Iterable[CitationDraft],
    source: str,
    workflow_id: int | None = None,
    office_action_id: str | None = None,
    delete_existing: bool = True,
) -> list[CitedReference]:
    if delete_existing:
        q = CitedReference.query.filter(CitedReference.matter_id == str(matter_id))
        if workflow_id is not None:
            q = q.filter(CitedReference.workflow_id == int(workflow_id))
        if office_action_id is not None:
            q = q.filter(CitedReference.office_action_id == str(office_action_id))
        q.delete(synchronize_session=False)

    rows: list[CitedReference] = []
    now = utcnow_naive()
    for idx, draft in enumerate(_dedupe_drafts(drafts), start=1):
        row = CitedReference(
            matter_id=str(matter_id),
            workflow_id=workflow_id,
            office_action_id=office_action_id,
            source=source,
            ref_type=draft.ref_type,
            label=draft.label,
            country=draft.country,
            publication_number=draft.publication_number,
            published_date=draft.published_date,
            title=draft.title,
            raw_text=draft.raw_text,
            sort_order=idx,
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
        rows.append(row)
    return rows


def save_auto_office_action_citations(
    *,
    matter_id: str,
    office_action_id: str,
    drafts: Iterable[CitationDraft],
    overwrite_manual: bool = False,
    clear_when_empty: bool = False,
) -> list[CitedReference]:
    draft_list = list(_dedupe_drafts(drafts))
    if not overwrite_manual:
        manual_exists = (
            CitedReference.query.filter_by(
                matter_id=str(matter_id),
                office_action_id=str(office_action_id),
            )
            .filter(CitedReference.source != "auto_pdf")
            .first()
            is not None
        )
        if manual_exists:
            return []
    if not draft_list:
        if clear_when_empty:
            delete_q = CitedReference.query.filter_by(
                matter_id=str(matter_id),
                office_action_id=str(office_action_id),
            )
            if not overwrite_manual:
                delete_q = delete_q.filter_by(source="auto_pdf")
            delete_q.delete(synchronize_session=False)
        return []
    delete_q = CitedReference.query.filter_by(
        matter_id=str(matter_id),
        office_action_id=str(office_action_id),
    )
    if not overwrite_manual:
        delete_q = delete_q.filter_by(source="auto_pdf")
    delete_q.delete(synchronize_session=False)
    return _replace_citations(
        matter_id=str(matter_id),
        office_action_id=str(office_action_id),
        drafts=draft_list,
        source="auto_pdf",
        delete_existing=False,
    )


def office_action_has_manual_citations(*, matter_id: str, office_action_id: str) -> bool:
    return (
        CitedReference.query.filter_by(
            matter_id=str(matter_id),
            office_action_id=str(office_action_id),
        )
        .filter(CitedReference.workflow_id.is_(None))
        .filter(CitedReference.source != "auto_pdf")
        .first()
        is not None
    )


def save_auto_workflow_citations(
    *,
    matter_id: str,
    workflow_id: int,
    drafts: Iterable[CitationDraft],
    source: str = "auto_uspto",
    clear_when_empty: bool = True,
) -> list[CitedReference]:
    draft_list = list(_dedupe_drafts(drafts))
    if not draft_list and not clear_when_empty:
        return []
    return _replace_citations(
        matter_id=str(matter_id),
        workflow_id=int(workflow_id),
        drafts=draft_list,
        source=source,
    )


def replace_office_action_citations_from_text(
    *,
    matter_id: str,
    office_action_id: str,
    text_value: str | None,
    source: str = "manual",
) -> list[CitedReference]:
    return _replace_citations(
        matter_id=str(matter_id),
        office_action_id=str(office_action_id),
        drafts=parse_manual_citations(text_value),
        source=source,
    )


def rows_to_text(rows: Iterable[CitedReference]) -> str:
    lines: list[str] = []
    for row in rows or []:
        raw = _clean_text(getattr(row, "raw_text", None))
        if not raw:
            continue
        label = _clean_text(getattr(row, "label", None))
        if label and not raw.casefold().startswith(label.casefold()):
            lines.append(f"{label}: {raw}")
        else:
            lines.append(raw)
    return "\n".join(lines)


def _sort_rows(rows: Iterable[CitedReference]) -> list[CitedReference]:
    return sorted(
        list(rows or []),
        key=lambda r: (
            int(getattr(r, "sort_order", None) or 0),
            int(getattr(r, "id", None) or 0),
        ),
    )


def office_action_citation_rows(office_action_id: str) -> list[CitedReference]:
    return _sort_rows(
        CitedReference.query.filter_by(office_action_id=str(office_action_id))
        .filter(CitedReference.workflow_id.is_(None))
        .all()
    )


def is_notice_doc_for_citations(doc_name: str | None) -> bool:
    """Return True only for notice documents that may carry cited references."""
    normalized = _WS_RE.sub(" ", str(doc_name or "")).strip().casefold()
    if not normalized:
        return False
    excluded_tokens = (
        "response",
        "amendment",
        "argument",
        "reply",
        "decision",
        "allowance",
    )
    if any(token in normalized for token in excluded_tokens):
        return False
    notice_tokens = (
        "office action",
        "non-final",
        "final rejection",
        "restriction requirement",
        "notice",
    )
    return any(token in normalized for token in notice_tokens)


def matter_office_action_citation_groups(matter_id: str) -> list[dict[str, object]]:
    matter_id = str(matter_id or "").strip()
    if not matter_id:
        return []
    oa_rows = db.session.execute(
        text(
            """
            SELECT oa_id, doc_name, received_date, notified_date, due_date, extended_due_date
            FROM office_action
            WHERE matter_id = :mid
            ORDER BY
                COALESCE(notified_date, received_date, due_date, '') DESC,
                doc_name ASC,
                oa_id ASC
        """
        ).execution_options(policy_bypass=True),
        {"mid": matter_id},
    ).all()
    if not oa_rows:
        return []
    oa_rows = [row for row in oa_rows if is_notice_doc_for_citations(row[1])]
    if not oa_rows:
        return []

    oa_ids = [str(row[0]) for row in oa_rows if row[0]]
    citation_rows = (
        CitedReference.query.filter(CitedReference.matter_id == matter_id)
        .filter(CitedReference.office_action_id.in_(oa_ids))
        .filter(CitedReference.workflow_id.is_(None))
        .order_by(
            CitedReference.office_action_id.asc(),
            CitedReference.sort_order.asc(),
            CitedReference.id.asc(),
        )
        .all()
    )
    by_oa: dict[str, list[CitedReference]] = {}
    for row in citation_rows:
        by_oa.setdefault(str(row.office_action_id), []).append(row)

    groups: list[dict[str, object]] = []
    for row in oa_rows:
        oa_id = str(row[0])
        rows = _sort_rows(by_oa.get(oa_id, []))
        groups.append(
            {
                "oa_id": oa_id,
                "doc_name": row[1] or "Notice",
                "received_date": row[2] or "",
                "notified_date": row[3] or "",
                "due_date": row[4] or "",
                "extended_due_date": row[5] or "",
                "rows": rows,
                "text": rows_to_text(rows),
                "count": len(rows),
            }
        )
    return groups


def _connected_family_matter_ids(source_matter_id: str) -> set[str]:
    start = (source_matter_id or "").strip()
    if not start:
        return set()
    known_mids: set[str] = {start}
    known_fams: set[str] = set()
    for _ in range(64):
        changed = False
        if known_mids:
            fams = (
                db.session.query(MatterFamily.family_id)
                .filter(MatterFamily.matter_id.in_(sorted(known_mids)))
                .distinct()
                .all()
            )
            for (fam_id,) in fams or []:
                fid = (fam_id or "").strip()
                if fid and fid not in known_fams:
                    known_fams.add(fid)
                    changed = True
        if known_fams:
            mids = (
                db.session.query(MatterFamily.matter_id)
                .filter(MatterFamily.family_id.in_(sorted(known_fams)))
                .distinct()
                .all()
            )
            for (matter_id,) in mids or []:
                mid = (matter_id or "").strip()
                if mid and mid not in known_mids:
                    known_mids.add(mid)
                    changed = True
        if not changed:
            break
    return known_mids


def _country_text_for_matter(matter: Matter) -> str:
    values = [getattr(matter, "our_ref", None)]
    try:
        row = MatterCustomField.query.filter_by(
            matter_id=str(matter.matter_id),
            namespace="outgoing_patent",
        ).first()
        if row and isinstance(row.data, dict):
            for key in ("application_country", "country", "jurisdiction"):
                values.append(row.data.get(key))
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="cited_reference_service._country_text_for_matter",
            log_key="cited_reference_service._country_text_for_matter",
            log_window_seconds=300,
        )
    return " ".join(str(v or "") for v in values)


def _is_us_outgoing_patent(matter: Matter) -> bool:
    if not matter:
        return False
    if (getattr(matter, "right_group", "") or "").strip().upper() != "OUT":
        return False
    if (getattr(matter, "matter_type", "") or "").strip().upper() != "PATENT":
        return False
    country_text = _country_text_for_matter(matter).upper()
    return bool(
        re.search(r"(?:^|[^A-Z])US(?:[^A-Z]|$)", country_text)
        or "USA" in country_text
        or "UNITED STATES" in country_text
    )


def _source_due_date(*, office_action_id: str | None, workflow: Workflow | None):
    candidates = []
    if workflow is not None:
        candidates.extend(
            [getattr(workflow, "due_date", None), getattr(workflow, "legal_due_date", None)]
        )
    if office_action_id:
        row = db.session.execute(
            text(
                """
                SELECT due_date, extended_due_date
                FROM office_action
                WHERE oa_id = :oid
                """
            ).execution_options(policy_bypass=True),
            {"oid": str(office_action_id)},
        ).fetchone()
        if row:
            candidates.extend([row[1], row[0]])
    for raw in candidates:
        if hasattr(raw, "isoformat"):
            return raw
        token = str(raw or "").strip()
        if not token:
            continue
        try:
            from datetime import date

            return date.fromisoformat(token[:10])
        except ValueError:
            continue
    return None


def _case_role_user_ids(matter_id: str) -> dict[str, int | None]:
    out: dict[str, int | None] = {"handler": None, "attorney": None, "manager": None}
    try:
        rows = db.session.execute(
            text(
                """
                SELECT LOWER(TRIM(msa.staff_role_code)) AS role_code, u.id AS user_id, msa.msa_id
                FROM matter_staff_assignment msa
                JOIN users u ON u.staff_party_id = msa.staff_party_id
                WHERE msa.matter_id = :mid
                  AND LOWER(TRIM(msa.staff_role_code)) IN (
                    'manager','mgmt','attorney','retainer','handler','staff','draftsman'
                  )
                  AND COALESCE(u.is_active, FALSE) = TRUE
                ORDER BY msa.msa_id ASC, u.id ASC
                """
            ).execution_options(policy_bypass=True),
            {"mid": str(matter_id)},
        ).all()
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="cited_reference_service._case_role_user_ids",
            log_key="cited_reference_service._case_role_user_ids",
            log_window_seconds=300,
        )
        return out

    for role_code, user_id, _msa_id in rows or []:
        role = (role_code or "").strip().lower()
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            continue
        if role in {"handler", "staff", "draftsman"} and out["handler"] is None:
            out["handler"] = uid
        elif role in {"attorney", "retainer"} and out["attorney"] is None:
            out["attorney"] = uid
        elif role in {"manager", "mgmt"} and out["manager"] is None:
            out["manager"] = uid
    return out


def _ids_business_code(
    *,
    target_matter_id: str,
    source_oa_id: str | None,
    source_workflow_id: int | None,
    source_matter_id: str,
) -> str:
    target_token = str(target_matter_id or "").strip()[:10]
    if source_oa_id:
        return f"IDS:{str(source_oa_id).strip()}:{target_token}"[:50]
    if source_workflow_id:
        return f"IDS:WF{int(source_workflow_id)}:{target_token}"[:50]
    return f"IDS:M{str(source_matter_id).strip()[:16]}:{target_token}"[:50]


def _merge_ids_note(existing_note: str | None, generated_note: str) -> str:
    existing = (existing_note or "").strip()
    generated = (generated_note or "").strip()
    if not generated:
        return existing
    if not existing:
        return generated

    marker = "Family OA cited references for IDS review."
    marker_idx = existing.find(marker)
    if marker_idx == 0:
        return generated
    if marker_idx > 0:
        return f"{existing[:marker_idx].rstrip()}\n\n{generated}".strip()
    return f"{existing}\n\n{generated}".strip()


def create_ids_tasks_for_us_family(
    *,
    source_matter_id: str,
    citations: Iterable[CitationDraft] | Iterable[CitedReference],
    source_oa_id: str | None = None,
    source_workflow: Workflow | None = None,
    source_doc_name: str | None = None,
    actor_id: int | None = None,
) -> IdsTaskResult:
    if source_workflow is not None:
        business_code = (getattr(source_workflow, "business_code", None) or "").strip().upper()
        if business_code.startswith("IDS:"):
            return IdsTaskResult()

    draft_list: list[CitationDraft] = []
    for item in citations or []:
        if isinstance(item, CitationDraft):
            draft_list.append(item)
        elif isinstance(item, CitedReference):
            draft_list.append(
                CitationDraft(
                    raw_text=item.raw_text,
                    label=item.label,
                    ref_type=item.ref_type,
                    country=item.country,
                    publication_number=item.publication_number,
                    published_date=item.published_date,
                    title=item.title,
                )
            )
    draft_list = _dedupe_drafts(draft_list)
    if not draft_list:
        return IdsTaskResult()

    source_mid = str(source_matter_id or "").strip()
    family_mids = _connected_family_matter_ids(source_mid)
    family_mids.discard(source_mid)
    if not family_mids:
        return IdsTaskResult()

    targets = (
        Matter.query.filter(Matter.matter_id.in_(sorted(family_mids)))
        .filter(or_(Matter.is_deleted.is_(None), Matter.is_deleted.is_(False)))
        .order_by(Matter.our_ref.asc(), Matter.matter_id.asc())
        .all()
    )
    us_targets = [matter for matter in targets if _is_us_outgoing_patent(matter)]
    if not us_targets:
        return IdsTaskResult()

    default_days = ConfigService.get_int("IDS_DEFAULT_DUE_DAYS", 14, min_value=0)
    default_due = today_local() + timedelta(days=int(default_days or 14))
    source_due = _source_due_date(
        office_action_id=source_oa_id,
        workflow=source_workflow,
    )
    due_date = min(default_due, source_due) if source_due else default_due

    source_matter = db.session.get(Matter, source_mid)
    source_ref = (getattr(source_matter, "our_ref", None) or source_mid or "").strip()
    citation_text = rows_to_text(
        [
            CitedReference(
                matter_id=source_mid,
                raw_text=d.raw_text,
                label=d.label,
                ref_type=d.ref_type,
                country=d.country,
                publication_number=d.publication_number,
                published_date=d.published_date,
                title=d.title,
                sort_order=i,
            )
            for i, d in enumerate(draft_list, start=1)
        ]
    )
    note = "\n".join(
        line
        for line in (
            "Family OA cited references for IDS review.",
            f"- Source matter: {source_ref}" if source_ref else "",
            (
                f"- Source document: {(source_doc_name or '').strip()}"
                if (source_doc_name or "").strip()
                else ""
            ),
            "- Cited references:",
            citation_text,
        )
        if line
    )

    workflow_ids: list[int] = []
    created = 0
    updated = 0
    source_workflow_id = (
        getattr(source_workflow, "id", None) if source_workflow is not None else None
    )
    for target in us_targets:
        code = _ids_business_code(
            target_matter_id=str(target.matter_id),
            source_oa_id=source_oa_id,
            source_workflow_id=source_workflow_id,
            source_matter_id=source_mid,
        )
        wf = Workflow.query.filter_by(business_code=code).first()
        role_ids = _case_role_user_ids(str(target.matter_id))
        if wf is None:
            wf = Workflow(
                case_id=str(target.matter_id),
                name=f"IDS review - {source_ref}" if source_ref else "IDS review",
                business_code=code,
                category=derive_workflow_category(
                    case_id=str(target.matter_id),
                    handler_id=role_ids.get("handler"),
                    attorney_id=role_ids.get("attorney"),
                    manager_id=role_ids.get("manager"),
                    manual_category="WORK",
                    hint_name_ref=code,
                    hint_name_free="IDS ",
                ),
                request_start_date=today_local(),
                due_date=due_date,
                legal_due_date=due_date,
                assignee_id=role_ids.get("handler") or actor_id,
                attorney_assignee_id=role_ids.get("attorney"),
                inspector_id=role_ids.get("manager"),
                created_by_id=actor_id,
                note=note,
            )
            db.session.add(wf)
            try:
                with db.session.begin_nested():
                    db.session.flush()
                created += 1
            except IntegrityError:
                wf = Workflow.query.filter_by(business_code=code).first()
        else:
            merged_note = _merge_ids_note(wf.note, note)
            if merged_note != (wf.note or "").strip():
                wf.note = merged_note
            if not wf.due_date:
                wf.due_date = due_date
            if not wf.legal_due_date:
                wf.legal_due_date = due_date
            updated += 1

        if wf and wf.id:
            workflow_ids.append(int(wf.id))
            try:
                from app.services.workflow.sync_requests import enqueue_workflow_sync

                enqueue_workflow_sync(workflow_id=int(wf.id))
            except (RuntimeError, SQLAlchemyError) as exc:
                report_swallowed_exception(
                    exc,
                    context="cited_reference_service.create_ids_tasks.enqueue",
                    log_key="cited_reference_service.create_ids_tasks.enqueue",
                    log_window_seconds=300,
                )

    return IdsTaskResult(
        created_count=created,
        updated_count=updated,
        workflow_ids=tuple(workflow_ids),
    )
