"""USPTO form parser for filing-package uploads.

The parser is intentionally rule-first because most USPTO filing receipts and
ADS exports expose stable labels. A narrow LLM fallback is available for OCR
text that clearly looks like a USPTO form but does not contain enough labelled
values for the rule parser.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

try:
    from openai import OpenAI, OpenAIError
except ImportError:  # pragma: no cover - optional runtime dependency
    OpenAI = None
    OpenAIError = Exception

from app.services.core.llm_model_registry import DEFAULT_LLM_MODEL, resolve_llm_model
from app.services.core.llm_runtime import get_openai_api_key

_MAX_LLM_CHARS = 12000

USPTO_FORM_SYSTEM_PROMPT = """
You extract docketing fields from USPTO forms for an IP matter upload workflow.

Return only fields that are explicitly present in the text. Use an empty string
when a field is missing. Normalize dates to YYYY-MM-DD. Preserve U.S. patent
application numbers in 17/123,456 style when possible. USPTO trademark serial
numbers may remain as eight digits.
"""

USPTO_FORM_JSON_SCHEMA = {
    "name": "UsptoFormFields",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "doc_type",
            "matter_kind",
            "app_no",
            "attorney_docket_no",
            "confirmation_no",
            "filing_date",
            "title",
            "applicant_name",
            "first_named_inventor",
            "mark_name",
        ],
        "properties": {
            "doc_type": {"type": "string"},
            "matter_kind": {"type": "string"},
            "app_no": {"type": "string"},
            "attorney_docket_no": {"type": "string"},
            "confirmation_no": {"type": "string"},
            "filing_date": {"type": "string"},
            "title": {"type": "string"},
            "applicant_name": {"type": "string"},
            "first_named_inventor": {"type": "string"},
            "mark_name": {"type": "string"},
        },
    },
    "strict": True,
}


@dataclass(frozen=True)
class UsptoFormParseResult:
    doc_type: str = ""
    matter_kind: str = ""
    app_no: str = ""
    attorney_docket_no: str = ""
    confirmation_no: str = ""
    filing_date: str = ""
    title: str = ""
    applicant_name: str = ""
    first_named_inventor: str = ""
    mark_name: str = ""
    parser: str = "rule"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def has_core_identifier(self) -> bool:
        return bool(self.app_no or self.attorney_docket_no)


def _compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _clean_value(value: str) -> str:
    value = _compact_spaces(value)
    value = re.sub(r"^[#:;,\-\s]+", "", value)
    value = re.sub(r"\s*(?:\|| {2,}).*$", "", value).strip()
    return value.strip(" .,:;")


def _lines(text: str) -> list[str]:
    return [_compact_spaces(line) for line in (text or "").splitlines() if _compact_spaces(line)]


def _normalize_date(value: str) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    raw = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    raw = raw.replace(",", " ")
    patterns = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%B %d %Y",
        "%b %d %Y",
    )
    for fmt in patterns:
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
            return datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            return ""

    m = re.search(r"\b(\d{4})[./-](\d{1,2})[./-](\d{1,2})\b", raw)
    if m:
        year, month, day = m.groups()
        try:
            return datetime(int(year), int(month), int(day)).date().isoformat()
        except ValueError:
            return ""

    return ""


def normalize_uspto_application_no(value: str, *, trademark_serial: bool = False) -> str:
    raw = _compact_spaces(value)
    if not raw:
        return ""

    m = re.search(r"(?<!\d)(\d{2})\s*/\s*(\d{3})\s*,?\s*(\d{3})(?!\d)", raw)
    if m:
        return f"{m.group(1)}/{m.group(2)},{m.group(3)}"

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        if trademark_serial:
            return digits
        return f"{digits[:2]}/{digits[2:5]},{digits[5:]}"

    return ""


def _looks_like_trademark(text: str) -> bool:
    return bool(
        re.search(
            r"\b(TEAS|trademark electronic application system|service mark|mark information|literal element|serial number)\b",
            text or "",
            re.IGNORECASE,
        )
    )


def extract_uspto_application_no(text: str) -> str:
    raw = text or ""
    is_trademark = _looks_like_trademark(raw)

    label_re = re.compile(
        r"(?:"
        r"application\s*(?:no\.?|number|#)"
        r"|appl(?:ication)?\s*no\.?"
        r"|u\.?\s*s\.?\s*application\s*(?:no\.?|number)"
        r"|serial\s*(?:no\.?|number)"
        r")",
        re.IGNORECASE,
    )
    for label in label_re.finditer(raw):
        window = raw[label.end() : label.end() + 140]
        serial_label = "serial" in label.group(0).lower()
        m = re.search(r"(?<!\d)(\d{2})\s*/\s*(\d{3})\s*,?\s*(\d{3})(?!\d)", window)
        if m:
            return normalize_uspto_application_no(m.group(0))
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", window)
        if m:
            return normalize_uspto_application_no(
                m.group(1), trademark_serial=(serial_label and is_trademark)
            )

    m = re.search(r"(?<!\d)(\d{2})\s*/\s*(\d{3})\s*,?\s*(\d{3})(?!\d)", raw)
    if m:
        return normalize_uspto_application_no(m.group(0))

    return ""


def _value_after_label(lines: list[str], label_patterns: list[str]) -> str:
    for idx, line in enumerate(lines):
        for pattern in label_patterns:
            same_line = re.search(
                rf"(?:^|\b)(?:{pattern})\s*(?:[:#-]|\s{{2,}})\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if same_line:
                value = _clean_value(same_line.group(1))
                if value and not re.fullmatch(pattern, value, flags=re.IGNORECASE):
                    return value
            if re.fullmatch(rf"(?:{pattern})\s*[:#-]?", line, flags=re.IGNORECASE):
                for nxt in lines[idx + 1 : idx + 4]:
                    value = _clean_value(nxt)
                    if value:
                        return value
    return ""


def _extract_filing_receipt_table(text: str) -> dict[str, str]:
    compact = _compact_spaces(text)
    header = re.search(
        r"APPLICATION\s+NO\.?\s+FILING\s+DATE.*ATTORNEY\s+DOCKET\s+NO\.?\s+CONFIRMATION\s+NO\.?",
        compact,
        re.IGNORECASE,
    )
    if not header:
        return {}
    window = compact[header.end() : header.end() + 360]
    app = extract_uspto_application_no(window)
    date_match = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", window)
    docket = ""
    confirmation = ""
    if date_match:
        after_date = window[date_match.end() :].strip()
        tokens = after_date.split()
        if len(tokens) >= 2:
            # In USPTO filing receipt tables the docket is usually the token
            # immediately before the four-digit confirmation number.
            for i, token in enumerate(tokens):
                if re.fullmatch(r"\d{4}", token) and i > 0:
                    docket = tokens[i - 1]
                    confirmation = token
                    break
    if not confirmation:
        matches = re.findall(r"\b(\d{4})\b", window)
        confirmation = matches[-1] if matches else ""
    return {
        "app_no": app,
        "filing_date": _normalize_date(date_match.group(0)) if date_match else "",
        "attorney_docket_no": _clean_value(docket),
        "confirmation_no": confirmation,
    }


def infer_uspto_doc_type(text: str, filename: str = "") -> str:
    haystack = f"{filename}\n{text}".lower()
    checks = (
        ("USPTO Filing Receipt", ("filing receipt",)),
        ("USPTO Application Data Sheet", ("application data sheet", "ads form")),
        ("USPTO Notice of Allowance", ("notice of allowance", "ptol-85")),
        ("USPTO Non-Final Office Action", ("non-final office action", "non final office action")),
        ("USPTO Final Office Action", ("final office action", "final rejection")),
        ("USPTO Office Action", ("office action",)),
        ("USPTO Issue Notification", ("issue notification",)),
        ("USPTO Payment Receipt", ("payment receipt", "fee payment", "payment confirmation")),
        ("USPTO TEAS Form", ("teas", "trademark electronic application system")),
    )
    for label, needles in checks:
        if any(needle in haystack for needle in needles):
            return label
    if looks_like_uspto_form(text, filename=filename):
        return "USPTO Form"
    return ""


def infer_uspto_matter_kind(text: str, filename: str = "") -> str:
    haystack = f"{filename}\n{text}"
    if _looks_like_trademark(haystack):
        return "trademark"
    if re.search(r"\b\d{2}\s*/\s*\d{3}\s*,?\s*\d{3}\b", haystack):
        return "patent"
    if re.search(r"\b(design patent|35\s+U\.?\s*S\.?\s*C\.?|inventor|invention)\b", haystack, re.I):
        return "patent"
    return ""


def looks_like_uspto_form(text: str, *, filename: str = "") -> bool:
    haystack = f"{filename}\n{text}"
    score = 0
    patterns = (
        r"United\s+States\s+Patent\s+and\s+Trademark\s+Office",
        r"\bUSPTO\b",
        r"\bPTO/(?:SB|AIA|PTOL)\b",
        r"\bPatent\s+Center\b",
        r"\bEFS-Web\b",
        r"\bFiling\s+Receipt\b",
        r"\bApplication\s+Data\s+Sheet\b",
        r"\bConfirmation\s+No\.?\b",
        r"\bAttorney\s+Docket\s+No\.?\b",
        r"\bTEAS\b",
        r"\bTrademark\s+Electronic\s+Application\s+System\b",
    )
    for pattern in patterns:
        if re.search(pattern, haystack, re.IGNORECASE):
            score += 1
    if extract_uspto_application_no(haystack):
        score += 1
    return score >= 2


def _normalize_llm_payload(payload: dict[str, Any], *, parser: str) -> UsptoFormParseResult:
    app_no = normalize_uspto_application_no(
        str(payload.get("app_no") or ""),
        trademark_serial=str(payload.get("matter_kind") or "").lower() == "trademark",
    )
    if not app_no:
        app_no = extract_uspto_application_no(str(payload.get("app_no") or ""))
    return UsptoFormParseResult(
        doc_type=_clean_value(str(payload.get("doc_type") or "")) or "USPTO Form",
        matter_kind=_clean_value(str(payload.get("matter_kind") or "")).lower(),
        app_no=app_no,
        attorney_docket_no=_clean_value(str(payload.get("attorney_docket_no") or "")),
        confirmation_no=_clean_value(str(payload.get("confirmation_no") or "")),
        filing_date=_normalize_date(str(payload.get("filing_date") or "")),
        title=_clean_value(str(payload.get("title") or "")),
        applicant_name=_clean_value(str(payload.get("applicant_name") or "")),
        first_named_inventor=_clean_value(str(payload.get("first_named_inventor") or "")),
        mark_name=_clean_value(str(payload.get("mark_name") or "")),
        parser=parser,
    )


def parse_uspto_form_rule_based(text: str, *, filename: str = "") -> UsptoFormParseResult:
    raw = text or ""
    all_lines = _lines(raw)
    table = _extract_filing_receipt_table(raw)
    matter_kind = infer_uspto_matter_kind(raw, filename=filename)
    trademark_serial = matter_kind == "trademark"

    app_no = table.get("app_no") or extract_uspto_application_no(raw)
    if not app_no:
        app_no = normalize_uspto_application_no(filename, trademark_serial=trademark_serial)

    filing_date = table.get("filing_date") or _normalize_date(
        _value_after_label(
            all_lines,
            [
                r"filing\s+date",
                r"filed",
                r"application\s+filing\s+date",
                r"international\s+filing\s+date",
            ],
        )
    )
    docket = table.get("attorney_docket_no") or _value_after_label(
        all_lines,
        [
            r"attorney\s+docket\s*(?:no\.?|number)?",
            r"applicant'?s\s+docket\s*(?:no\.?|number)?",
            r"docket\s*(?:no\.?|number)?",
        ],
    )
    confirmation = table.get("confirmation_no") or _value_after_label(
        all_lines,
        [r"confirmation\s*(?:no\.?|number)"],
    )
    applicant = _value_after_label(
        all_lines,
        [
            r"applicant(?:\(s\))?",
            r"first\s+named\s+applicant",
            r"owner",
        ],
    )
    inventor = _value_after_label(
        all_lines,
        [r"first\s+named\s+inventor", r"inventor(?:\(s\))?"],
    )
    title = _value_after_label(
        all_lines,
        [r"title\s+of\s+invention", r"invention\s+title", r"title"],
    )
    mark = _value_after_label(
        all_lines,
        [r"mark", r"literal\s+element", r"mark\s+information"],
    )

    if matter_kind == "trademark" and mark and not title:
        title = mark

    return UsptoFormParseResult(
        doc_type=infer_uspto_doc_type(raw, filename=filename),
        matter_kind=matter_kind,
        app_no=app_no,
        attorney_docket_no=_clean_value(docket),
        confirmation_no=_clean_value(confirmation),
        filing_date=filing_date,
        title=_clean_value(title),
        applicant_name=_clean_value(applicant),
        first_named_inventor=_clean_value(inventor),
        mark_name=_clean_value(mark),
        parser="rule",
    )


def _uspto_rule_score(result: UsptoFormParseResult, *, text: str, filename: str) -> int:
    score = 0
    if looks_like_uspto_form(text, filename=filename):
        score += 1
    for value in (
        result.app_no,
        result.attorney_docket_no,
        result.filing_date,
        result.title or result.mark_name,
        result.confirmation_no,
    ):
        if value:
            score += 1
    return score


def _merge_results(
    rule: UsptoFormParseResult,
    fallback: UsptoFormParseResult,
) -> UsptoFormParseResult:
    data = rule.to_dict()
    for key, value in fallback.to_dict().items():
        if key == "parser":
            continue
        if not data.get(key) and value:
            data[key] = value
    data["parser"] = fallback.parser if fallback.has_core_identifier else rule.parser
    return UsptoFormParseResult(**data)


def _default_llm_model() -> str:
    try:
        return resolve_llm_model("billing_invoice")
    except Exception:
        return DEFAULT_LLM_MODEL


def parse_uspto_form_from_text(text: str, api_key: str) -> UsptoFormParseResult:
    if OpenAI is None:
        raise RuntimeError("OpenAI package is not installed.")
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=_default_llm_model(),
            messages=[
                {"role": "system", "content": USPTO_FORM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Extract USPTO form fields from this text:\n\n{text[:_MAX_LLM_CHARS]}",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": USPTO_FORM_JSON_SCHEMA,
            },
            temperature=0,
        )
        payload = json.loads(response.choices[0].message.content)
        return _normalize_llm_payload(payload, parser="llm")
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise Exception(f"USPTO form LLM parsing failed: {exc}") from exc


def parse_uspto_form(
    text: str,
    *,
    filename: str = "",
    api_key: str | None = None,
) -> UsptoFormParseResult:
    rule = parse_uspto_form_rule_based(text or "", filename=filename)
    if _uspto_rule_score(rule, text=text or "", filename=filename) >= 3:
        return rule
    if not looks_like_uspto_form(text or "", filename=filename):
        return rule

    resolved_key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not resolved_key:
        try:
            resolved_key = get_openai_api_key()
        except Exception:
            resolved_key = ""
    if not resolved_key:
        return rule

    try:
        llm_result = parse_uspto_form_from_text(text or "", resolved_key)
    except Exception:
        return rule
    return _merge_results(rule, llm_result)


def uspto_result_to_matter_params(result: UsptoFormParseResult | dict[str, Any]) -> dict[str, Any]:
    data = result.to_dict() if isinstance(result, UsptoFormParseResult) else dict(result or {})
    params: dict[str, Any] = {}
    app_no = (data.get("app_no") or "").strip()
    if app_no:
        params["app_no"] = app_no
    filing_date = (data.get("filing_date") or "").strip()
    if filing_date:
        params["events"] = [{"event_key": "APP_DATE", "event_at": filing_date}]
    title = (data.get("title") or data.get("mark_name") or "").strip()
    matter_kind = (data.get("matter_kind") or "").strip().lower()
    if title:
        params["right_name"] = title
        if matter_kind == "trademark":
            params["tm_name"] = title
        else:
            params["title_en"] = title
    applicant_name = (data.get("applicant_name") or "").strip()
    if applicant_name:
        params["application_applicant_name"] = applicant_name
    doc_type = (data.get("doc_type") or "").strip()
    if doc_type:
        params["doc_type"] = doc_type
    return params
