"""
Unified Case Matching Service

   + LLM  Matter Matching 
- (our_ref) Matching 
- Application No.(app_no) Matching 
- LLM  (API  Settings)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field, fields
from email import policy
from email.parser import BytesParser
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.ip_records import Matter, MatterCustomField, MatterIdentifier
from app.services.assistant.llm_document_parser import (
    extract_text_from_pdf,
    parse_foreign_identifiers,
)
from app.services.uspto.uspto_form_parser import (
    UsptoFormParseResult,
    looks_like_uspto_form,
    parse_uspto_form,
    parse_uspto_form_rule_based,
)
from app.services.uploads.zip_safety import ZipLimits as ZipSafetyLimits
from app.services.uploads.zip_safety import (
    ZipSafetyError,
    get_limits,
    safe_extract_bytes,
    safe_list,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.mime_headers import decode_mime_encoded_words, normalize_uploaded_filename
from app.utils.policy_sql import policy_bypass_text

logger = logging.getLogger(__name__)

_APP_NO_MIN_MATCH_LENGTH = 8
_IDENTIFIER_FIELD_KEYS = (
    "our_ref",
    "your_ref",
    "app_no",
    "publication_no",
    "registration_no",
    "pct_no",
    "agent_ref",
    "client_ref",
)
_IDENTIFIER_SECONDARY_KEYS = (
    "your_ref",
    "publication_no",
    "registration_no",
    "pct_no",
    "agent_ref",
    "client_ref",
)
_CANDIDATE_SCORE_MIN_MARGIN = 20


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class CaseMatchResult:
    """Matching  table """

    matter_id: Optional[str] = None
    match_method: str = "failed"  # "rule_ref", "rule_app", "llm_ref", "llm_app", "failed"
    confidence: str = "LOW"  # "HIGH", "MEDIUM", "LOW"
    extracted_info: Dict[str, Any] = field(default_factory=dict)
    candidate_ids: list[str] = field(default_factory=list)
    candidate_reasons: list[str] = field(default_factory=list)
    error: Optional[str] = None
    text_snippet: str = ""

    @property
    def matched(self) -> bool:
        return self.matter_id is not None and self.match_method != "failed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "match": self.matched,
            "matter_id": self.matter_id,
            "method": self.match_method,
            "confidence": self.confidence,
            "extracted": self.extracted_info,
            "candidate_ids": self.candidate_ids,
            "candidate_reasons": self.candidate_reasons,
            "error": self.error,
            "text_snippet": self.text_snippet,
        }


@dataclass
class ExtractedIdentifiers:
    """ Identifiers """

    our_ref: Optional[str] = None
    your_ref: Optional[str] = None
    app_no: Optional[str] = None
    publication_no: Optional[str] = None
    registration_no: Optional[str] = None
    pct_no: Optional[str] = None
    agent_ref: Optional[str] = None
    client_ref: Optional[str] = None
    doc_type: Optional[str] = None
    dispatch_date: Optional[str] = None
    due_date: Optional[str] = None
    right_name: Optional[str] = None
    applicant_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


_EXTRACTED_IDENTIFIER_FIELDS = {f.name for f in fields(ExtractedIdentifiers)}


def _normalize_extracted_payload(payload: dict) -> dict:
    data = dict(payload or {})
    identifiers = data.get("identifiers")
    if isinstance(identifiers, dict):
        data = _merge_foreign_identifiers(data)
    if data.get("application_no") and not data.get("app_no"):
        data["app_no"] = data.get("application_no")
    if data.get("pct_application_no") and not data.get("pct_no"):
        data["pct_no"] = data.get("pct_application_no")
    return {k: v for k, v in data.items() if k in _EXTRACTED_IDENTIFIER_FIELDS}


def normalize_app_no(app_no: str) -> str:
    """Normalize application number to standard format (10-2024-1234567)."""
    if not app_no:
        return ""
    # Remove non-digits
    digits = re.sub(r"\D", "", app_no)
    if len(digits) == 13:  # e.g. 1020241234567
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    if len(digits) == 12:  # e.g. 20241234567 (missing type code?) - ambiguous
        return app_no
    return app_no


def _normalize_ref_like(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z-]", "", (value or "").strip().upper())


def _app_no_min_match_length() -> int:
    # Best-effort config/env parsing; fall back to defaults on failures.
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            value = current_app.config.get("APP_NO_MIN_MATCH_LENGTH", _APP_NO_MIN_MATCH_LENGTH)
            return int(value or _APP_NO_MIN_MATCH_LENGTH)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="auto_match_service._app_no_min_match_length.flask_config",
            log_key="auto_match_service._app_no_min_match_length.flask_config",
            log_window_seconds=300,
        )
    try:
        raw = os.environ.get("APP_NO_MIN_MATCH_LENGTH")
        if raw is not None and str(raw).strip():
            return int(raw)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="auto_match_service._app_no_min_match_length.env",
            log_key="auto_match_service._app_no_min_match_length.env",
            log_window_seconds=300,
        )
    return _APP_NO_MIN_MATCH_LENGTH


def _unique_matter_from_ids(
    matter_ids: list[str],
    *,
    context: str,
) -> Optional[Matter]:
    unique: dict[str, Matter] = {}
    for mid in matter_ids:
        if not mid:
            continue
        matter = Matter.query.get(str(mid))
        if not matter or getattr(matter, "is_deleted", False):
            continue
        unique[str(matter.matter_id)] = matter
        if len(unique) > 1:
            break
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        try:
            logger.warning(
                "Ambiguous app_no match (%s): %s",
                context,
                ", ".join(sorted(unique.keys())),
            )
        except Exception as exc:
            # Logging failure should not block matching.
            report_swallowed_exception(
                exc,
                context="auto_match_service._unique_matter_from_ids.warning",
                log_key="auto_match_service._unique_matter_from_ids.warning",
                log_window_seconds=300,
            )
    return None


def find_case_by_ref(ref: str) -> Optional[Matter]:
    """Find matter by our_ref (exact or close match)."""
    from app.services.matter.matter_identity_service import MatterIdentityService

    return MatterIdentityService.find_by_reference(ref, allow_normalized=True)


def find_case_by_app_no(app_no: str) -> Optional[Matter]:
    """Find matter by application number with tolerant normalization."""
    if not app_no:
        return None

    app_no_norm = re.sub(r"[^0-9]", "", app_no)
    if not app_no_norm:
        return None
    # Enforce strict length check early
    if len(app_no_norm) < _app_no_min_match_length():
        return None
    try:
        dialect = (db.session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect = ""

    try:
        # Prefer matter_identifier match (normalized digits), scoped to application-number id_types.
        if (dialect or "").lower() == "postgresql":
            norm_expr = "regexp_replace(COALESCE(id_value, ''), '[^0-9]', '', 'g')"
        else:
            norm_expr = (
                "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
                "REPLACE(REPLACE(COALESCE(id_value, ''), '-', ''), ' ', ''), '.', ''), '_', ''),"
                " '/', ''), '(', ''), ')', ''), ':', ''), ',', ''), ';', '')"
            )

        with db.session.begin_nested():
            rows = db.session.execute(
                policy_bypass_text(
                    sql=f"""
                    SELECT matter_id
                    FROM matter_identifier
                    WHERE {norm_expr} = :app_no
                      AND id_type IN ('Application No.', 'APP_NO', 'application_no', 'app_no')
                    LIMIT 2
                    """,
                    reason="case auto-match normalized application-number lookup",
                    scope="matter_identifier:read:matter_id,id_type,id_value",
                ),
                {"app_no": app_no_norm},
            ).fetchall()

        mids = [str(row[0]) for row in rows or [] if row and row[0]]
        unique = _unique_matter_from_ids(mids, context="matter_identifier_app_no")
        if unique:
            return unique

        # Fallback to broader search ONLY if input is long enough to be specific
        # (e.g. at least 10 digits/chars) to avoid "123" matching everything.
        if len(app_no_norm) >= 10:
            # Logic for broader search (omitted/simplified for safety in this robust version)
            # We rely primarily on exact normalized match for app numbers now.
            pass

    except Exception as exc:
        # Best-effort: skip failing lookup and try the next source.
        report_swallowed_exception(
            exc,
            context="auto_match_service.find_case_by_app_no.matter_identifier_exact",
            log_key="auto_match_service.find_case_by_app_no.matter_identifier_exact",
            log_window_seconds=300,
        )

    try:
        if (dialect or "").lower() == "postgresql":
            with db.session.begin_nested():
                rows = db.session.execute(
                    policy_bypass_text(
                        sql="""
                        SELECT DISTINCT matter_id
                        FROM case_flat_index
                        WHERE regexp_replace(COALESCE(application_no, ''), '[^0-9]', '', 'g') = :app_no
                        LIMIT 2
                        """,
                        reason="case auto-match normalized application-number lookup",
                        scope="case_flat_index:read:matter_id,application_no",
                    ),
                    {"app_no": app_no_norm},
                ).fetchall()
        else:
            with db.session.begin_nested():
                rows = db.session.execute(
                    policy_bypass_text(
                        sql="""
                        SELECT DISTINCT matter_id
                        FROM case_flat_index
                        WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                            REPLACE(REPLACE(COALESCE(application_no, ''), '-', ''), ' ', ''), '.', ''), '_', ''),
                            '/', ''), '(', ''), ')', ''), ':', ''), ',', ''), ';', '') = :app_no
                        LIMIT 2
                        """,
                        reason="case auto-match normalized application-number lookup",
                        scope="case_flat_index:read:matter_id,application_no",
                    ),
                    {"app_no": app_no_norm},
                ).fetchall()
        mids = [str(r[0]) for r in rows or [] if r and r[0]]
        unique = _unique_matter_from_ids(mids, context="case_flat_index")
        if unique:
            return unique
    except Exception as exc:
        # Best-effort: skip failing lookup and try the next source.
        report_swallowed_exception(
            exc,
            context="auto_match_service.find_case_by_app_no.case_flat_index",
            log_key="auto_match_service.find_case_by_app_no.case_flat_index",
            log_window_seconds=300,
        )

    try:
        if (dialect or "").lower() == "postgresql":
            val_expr = (
                "regexp_replace(COALESCE(data->>'application_no', data->>'app_no', "
                "data->>'Application No.', ''), '[^0-9]', '', 'g')"
            )
        else:
            val_expr = (
                "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
                "REPLACE(REPLACE(COALESCE(json_extract(data, '$.application_no'), "
                "json_extract(data, '$.app_no'), json_extract(data, '$.Application No.'), ''),"
                " '-', ''), ' ', ''), '.', ''), '_', ''), '/', ''), '(', ''), ')', ''),"
                " ':', ''), ',', ''), ';', '')"
            )

        with db.session.begin_nested():
            rows = db.session.execute(
                policy_bypass_text(
                    sql=f"""
                    SELECT matter_id
                    FROM matter_custom_field
                    WHERE {val_expr} = :app_no
                    GROUP BY matter_id
                    ORDER BY MAX(updated_at) DESC
                    LIMIT 2
                    """,
                    reason="case auto-match normalized application-number lookup",
                    scope="matter_custom_field:read:matter_id,data,updated_at",
                ),
                {"app_no": app_no_norm},
            ).fetchall()
        mids = [str(r[0]) for r in rows or [] if r and r[0]]
        unique = _unique_matter_from_ids(mids, context="matter_custom_field")
        if unique:
            return unique
    except Exception as exc:
        # Best-effort: skip failing lookup and try the next source.
        report_swallowed_exception(
            exc,
            context="auto_match_service.find_case_by_app_no.matter_custom_field",
            log_key="auto_match_service.find_case_by_app_no.matter_custom_field",
            log_window_seconds=300,
        )

    return None


_LABELLED_ID_RE = re.compile(
    r"(?i)\b("
    r"our\s*ref|your\s*ref|agent\s*ref|client\s*ref|"
    r"application\s*no\.?|app\s*no\.?|"
    r"publication\s*no\.?|pub\s*no\.?|"
    r"registration\s*no\.?|reg\s*no\.?|patent\s*no\.?|"
    r"pct(?:\s*application)?\s*no\.?"
    r")\b\s*[:#]?\s*([A-Za-z0-9/\-]{4,40})"
)

_PCT_NO_RE = re.compile(
    r"\bPCT\s*/?\s*[A-Z]{2}\s*\d{4}\s*/?\s*\d{3,8}\b",
    re.IGNORECASE,
)
_OUR_REF_SUFFIX_RE = r"(?:[A-Z]{0,2}|PCT)"
_OUR_REF_PATTERN_RE = re.compile(
    rf"\b((?:\d{{2}}[A-Z]{{1,3}}[-]?\d{{3,4}}[-]?{_OUR_REF_SUFFIX_RE}|"
    rf"[A-Z]{{1,2}}\d{{2}}[-]?\d{{3,4}}[-]?{_OUR_REF_SUFFIX_RE}))\b",
    re.IGNORECASE,
)
_REPLY_HISTORY_SPLIT_PATTERNS = (
    re.compile(r"(?mi)^\s*on .+ wrote:\s*$"),
    re.compile(r"(?mi)^\s*\d{4}.+ :\s*$"),
    re.compile(r"(Newmi)^\s*-{2,}\s*original message\s*-{2,}\s*$"),
    re.compile(r"(?mi)^\s*(?:>\s*)*(?:\*+\s*)?from:\*?\s+.+$"),
    re.compile(r"(?mi)^\s*(?:>\s*)*(?:\*+\s*)? :\*?\s+.+$"),
    re.compile(r"(?mis)^\s*from:\s*.+?\n\s*sent:\s*.+?\n\s*to:\s*.+?\n\s*subject:\s*.+?$"),
    re.compile(
        r"(?mis)^\s* :\s*.+?\n\s* :\s*.+?\n\s* :\s*.+?\n\s*Title:\s*.+?$"
    ),
)


def _trim_reply_history_text(text: str) -> str:
    raw = _safe_text(text).strip()
    if not raw:
        return ""
    cut_at: int | None = None
    for pattern in _REPLY_HISTORY_SPLIT_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        start = int(match.start() or 0)
        if start <= 0:
            continue
        cut_at = start if cut_at is None else min(cut_at, start)
    if cut_at is None:
        return raw
    return raw[:cut_at].rstrip()


def _extract_identifiers_with_reply_history_fallback(text: str) -> Dict[str, str]:
    raw = _safe_text(text)
    trimmed = _trim_reply_history_text(raw)
    basic = extract_identifiers_rule_based(trimmed)
    if trimmed != raw.strip() and not any(basic.get(key) for key in _IDENTIFIER_FIELD_KEYS):
        basic = extract_identifiers_rule_based(raw)
    return basic


def extract_identifiers_rule_based(text: str) -> Dict[str, str]:
    """
    Extract potential identifiers (Our Ref, App No) from text using Regex.
    """
    identifiers = {
        "our_ref": None,
        "your_ref": None,
        "app_no": None,
        "publication_no": None,
        "registration_no": None,
        "pct_no": None,
        "agent_ref": None,
        "client_ref": None,
        "doc_type": None,
    }

    # Our Ref Pattern:
    # - legacy: 25PD0123US / 25-PD-0123
    # - prefix style: C250006US
    ref_match = _OUR_REF_PATTERN_RE.search(text or "")
    if ref_match:
        raw_ref = ref_match.group(1)
        # Normalize: Remove dashes/spaces for standard format check if needed
        identifiers["our_ref"] = raw_ref.replace("-", "").upper()

    # App No Pattern: 10-2024-1234567 or similar
    app_match = re.search(r"(\d{2}-\d{4}-\d{7})", text)
    if app_match:
        identifiers["app_no"] = app_match.group(1)
        if str(app_match.group(1)).startswith("40-"):
            identifiers["registration_no"] = identifiers["registration_no"] or app_match.group(1)
    else:
        # Try without hyphens: 1020241234567
        app_match_raw = re.search(r"\b(10|20|30|40)(\d{4})(\d{7})\b", text)
        if app_match_raw:
            identifiers["app_no"] = (
                f"{app_match_raw.group(1)}-{app_match_raw.group(2)}-{app_match_raw.group(3)}"
            )
            if app_match_raw.group(1) == "40":
                identifiers["registration_no"] = identifiers["registration_no"] or (
                    f"{app_match_raw.group(1)}-{app_match_raw.group(2)}-{app_match_raw.group(3)}"
                )

    # Labeled identifiers (your_ref / agent_ref / client_ref / publication / registration / pct)
    for label, value in _LABELLED_ID_RE.findall(text or ""):
        key = label.lower().replace(" ", "")
        if key in ("ourref",):
            identifiers["our_ref"] = identifiers["our_ref"] or value
        elif key in ("yourref",):
            identifiers["your_ref"] = identifiers["your_ref"] or value
        elif key in ("agentref",):
            identifiers["agent_ref"] = identifiers["agent_ref"] or value
        elif key in ("clientref",):
            identifiers["client_ref"] = identifiers["client_ref"] or value
        elif key in ("applicationno", "appno"):
            identifiers["app_no"] = identifiers["app_no"] or value
        elif key in ("publicationno", "pubno"):
            identifiers["publication_no"] = identifiers["publication_no"] or value
        elif key in ("registrationno", "regno", "patentno"):
            identifiers["registration_no"] = identifiers["registration_no"] or value
        elif key.startswith("pct"):
            identifiers["pct_no"] = identifiers["pct_no"] or value

    if not identifiers["pct_no"]:
        pct_match = _PCT_NO_RE.search(text or "")
        if pct_match:
            identifiers["pct_no"] = pct_match.group(0).replace(" ", "").upper()

    return identifiers


def _looks_like_uspto(text: str, filename: str) -> bool:
    return looks_like_uspto_form(text or "", filename=filename or "")


def _uspto_result_to_match_payload(result: UsptoFormParseResult) -> dict[str, str]:
    payload: dict[str, str] = {
        "doc_type": result.doc_type,
        "app_no": result.app_no,
        "agent_ref": result.attorney_docket_no,
        "client_ref": result.attorney_docket_no,
        "right_name": result.title or result.mark_name,
        "applicant_name": result.applicant_name,
    }
    return {key: value for key, value in payload.items() if value}


def _apply_uspto_identifiers(
    target: ExtractedIdentifiers,
    text: str,
    filename: str,
    *,
    overwrite: bool = False,
) -> None:
    if not _looks_like_uspto(text, filename):
        return
    parsed = parse_uspto_form_rule_based(text or "", filename=filename or "")
    payload = _uspto_result_to_match_payload(parsed)
    _apply_identifiers(
        target,
        payload,
        keys=("app_no", "agent_ref", "client_ref", "doc_type", "right_name", "applicant_name"),
        overwrite=overwrite,
    )


def _merge_foreign_identifiers(llm_res: dict) -> dict:
    identifiers = llm_res.get("identifiers") or {}
    if identifiers.get("our_ref") and not llm_res.get("our_ref"):
        llm_res["our_ref"] = identifiers.get("our_ref")
    if identifiers.get("application_no") and not llm_res.get("app_no"):
        llm_res["app_no"] = identifiers.get("application_no")
    if identifiers.get("publication_no") and not llm_res.get("publication_no"):
        llm_res["publication_no"] = identifiers.get("publication_no")
    if identifiers.get("registration_no") and not llm_res.get("registration_no"):
        llm_res["registration_no"] = identifiers.get("registration_no")
    if identifiers.get("pct_no") and not llm_res.get("pct_no"):
        llm_res["pct_no"] = identifiers.get("pct_no")
    if identifiers.get("agent_ref") and not llm_res.get("agent_ref"):
        llm_res["agent_ref"] = identifiers.get("agent_ref")
    if identifiers.get("client_ref") and not llm_res.get("client_ref"):
        llm_res["client_ref"] = identifiers.get("client_ref")
    return llm_res


def _release_read_only_session_before_external_call(*, context: str, log_key: str) -> None:
    try:
        session = db.session()
        if session.new or session.dirty or session.deleted:
            return
        tx = session.get_transaction()
        if tx is None:
            return
        session.rollback()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=log_key,
            log_window_seconds=300,
        )


_IDENTIFIER_TYPE_MAP = {
    "app_no": {
        "id_types": ["APP_NO", "Application No.", "application_no", "app_no"],
        "normalize": "digits",
    },
    "publication_no": {
        "id_types": ["PUB_NO", "publication_no", "Publication No.", "Publication No.(PUB)"],
        "normalize": "digits",
    },
    "registration_no": {
        "id_types": ["REG_NO", "registration_no", "Registration No."],
        "normalize": "digits",
    },
    "pct_no": {
        "id_types": [
            "PCT_NO",
            "pct_no",
            "pct_application_no",
            "Application No.",
            "PCTApplication No.",
            "Application No.",
        ],
        "normalize": "compact",
    },
    "agent_ref": {
        "id_types": ["AGENT_REF", "agent_ref", "Representative", "your_ref"],
        "normalize": "compact",
    },
    "client_ref": {
        "id_types": ["CLIENT_REF", "client_ref", "Client"],
        "normalize": "compact",
    },
}


def _normalize_identifier_value(value: str, mode: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if mode == "digits":
        return re.sub(r"\D", "", raw)
    if mode == "compact":
        return _normalize_ref_like(raw)
    return raw.strip()


def _normalized_identifier_expr(column, mode: str):
    try:
        dialect = (db.session.get_bind().dialect.name or "").lower()
    except Exception:
        dialect = ""
    if mode == "digits":
        if dialect == "postgresql":
            return func.regexp_replace(func.coalesce(column, ""), r"[^0-9]", "", "g")
        expr = func.coalesce(column, "")
        for token in (
            "-",
            " ",
            "/",
            ".",
            "_",
            "(",
            ")",
            ":",
            ",",
            ";",
            "\\",
            "#",
            "[",
            "]",
            "{",
            "}",
            "+",
        ):
            expr = func.replace(expr, token, "")
        return expr
    if mode == "compact":
        if dialect == "postgresql":
            return func.regexp_replace(
                func.upper(func.coalesce(column, "")), r"[^A-Z0-9-]", "", "g"
            )
        expr = func.upper(func.coalesce(column, ""))
        for token in (
            " ",
            "-",
            "/",
            ".",
            "_",
            "(",
            ")",
            ":",
            ",",
            ";",
            "\\",
            "#",
            "[",
            "]",
            "{",
            "}",
            "+",
        ):
            expr = func.replace(expr, token, "")
        return expr
    return func.upper(func.trim(column))


def _find_matter_ids_by_identifier(
    *,
    id_types: list[str],
    value: str,
    mode: str,
    min_length: int | None = None,
) -> list[str]:
    norm = _normalize_identifier_value(value, mode)
    if not norm:
        return []
    if min_length is not None and len(norm) < int(min_length):
        return []

    expr = _normalized_identifier_expr(MatterIdentifier.id_value, mode)
    q = (
        db.session.query(MatterIdentifier.matter_id)
        .join(Matter, Matter.matter_id == MatterIdentifier.matter_id)
        .filter(MatterIdentifier.id_type.in_(id_types))
        .filter(expr == norm)
    )
    if hasattr(Matter, "is_deleted"):
        q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
    rows = q.distinct().all()
    return [str(mid) for (mid,) in rows if mid]


def _find_matter_ids_by_your_ref(value: str) -> list[str]:
    norm = _normalize_identifier_value(value, "compact")
    if not norm:
        return []
    expr = _normalized_identifier_expr(Matter.your_ref, "compact")
    q = Matter.query.with_entities(Matter.matter_id).filter(expr == norm)
    if hasattr(Matter, "is_deleted"):
        q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
    rows = q.all()
    return [str(mid) for (mid,) in rows if mid]


_MATCH_SCORE_WEIGHTS = {
    "our_ref": 100,
    "app_no": 90,
    "publication_no": 70,
    "registration_no": 70,
    "pct_no": 60,
    "agent_ref": 50,
    "client_ref": 50,
    "your_ref": 40,
}


def score_matter_candidates(extracted: ExtractedIdentifiers | dict) -> list[dict]:
    """
    Score matter candidates using identifier matches.
    Returns list of {"matter_id": str, "score": int, "reasons": [str]} sorted by score.
    """
    if isinstance(extracted, dict):
        extracted_ids = ExtractedIdentifiers(**_normalize_extracted_payload(extracted))
    else:
        extracted_ids = extracted

    scores: dict[str, dict] = {}

    def add_score(matter_id: str, weight: int, reason: str) -> None:
        if not matter_id:
            return
        entry = scores.setdefault(matter_id, {"score": 0, "reasons": []})
        entry["score"] += weight
        entry["reasons"].append(reason)

    # Our ref direct lookup (exact/alias)
    if extracted_ids.our_ref:
        raw = (extracted_ids.our_ref or "").strip()
        from app.services.matter.matter_identity_service import MatterIdentityService

        for match in MatterIdentityService.match_references([raw]):
            reason = "our_ref exact match"
            if match.normalized:
                reason = f"our_ref normalized match ({match.matched_value})"
            add_score(str(match.matter_id), _MATCH_SCORE_WEIGHTS["our_ref"], reason)

    # Identifier-based matches
    for key in ("app_no", "publication_no", "registration_no", "pct_no", "agent_ref", "client_ref"):
        value = getattr(extracted_ids, key, None)
        if not value:
            continue
        config = _IDENTIFIER_TYPE_MAP.get(key)
        if not config:
            continue
        min_length = _app_no_min_match_length() if key == "app_no" else None
        mids = _find_matter_ids_by_identifier(
            id_types=config["id_types"],
            value=value,
            mode=config["normalize"],
            min_length=min_length,
        )
        for mid in mids:
            add_score(mid, _MATCH_SCORE_WEIGHTS[key], f"{key} identifier match")

    if extracted_ids.your_ref:
        mids = _find_matter_ids_by_your_ref(extracted_ids.your_ref)
        for mid in mids:
            add_score(mid, _MATCH_SCORE_WEIGHTS["your_ref"], "your_ref match")

    ranked = [
        {"matter_id": mid, "score": payload["score"], "reasons": payload["reasons"]}
        for mid, payload in scores.items()
    ]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def _merge_identifier_candidates(
    *, extracted_ids: ExtractedIdentifiers
) -> tuple[Optional[str], list[str], list[str], str, str]:
    """
    Combine candidate matches from multiple identifiers.
    Returns (matter_id, candidate_ids, reasons, method, confidence).
    """
    candidate_ids: set[str] = set()
    reasons: list[str] = []
    has_ambiguous_hits = False
    scores: dict[str, int] = {}
    matched_keys_by_mid: dict[str, set[str]] = {}

    # App No first (strongest after our_ref)
    for key in ("app_no", "publication_no", "registration_no", "pct_no", "agent_ref", "client_ref"):
        value = getattr(extracted_ids, key, None)
        if not value:
            continue
        config = _IDENTIFIER_TYPE_MAP.get(key)
        if not config:
            continue
        min_length = _app_no_min_match_length() if key == "app_no" else None
        mids = _find_matter_ids_by_identifier(
            id_types=config["id_types"],
            value=value,
            mode=config["normalize"],
            min_length=min_length,
        )
        if not mids:
            continue
        uniq_mids = sorted({str(mid) for mid in mids if mid})
        if not uniq_mids:
            continue
        if len(uniq_mids) == 1:
            reasons.append(f"{key} matched uniquely")
        else:
            has_ambiguous_hits = True
            reasons.append(f"{key} matched multiple matters ({len(uniq_mids)})")
        candidate_ids.update(uniq_mids)
        weight = int(_MATCH_SCORE_WEIGHTS.get(key) or 0)
        for mid in uniq_mids:
            scores[mid] = int(scores.get(mid) or 0) + weight
            matched_keys_by_mid.setdefault(mid, set()).add(key)

    # your_ref on Matter table (not in MatterIdentifier)
    if extracted_ids.your_ref:
        mids = _find_matter_ids_by_your_ref(extracted_ids.your_ref)
        if mids:
            uniq_mids = sorted({str(mid) for mid in mids if mid})
            if len(uniq_mids) == 1:
                reasons.append("your_ref matched uniquely")
            else:
                has_ambiguous_hits = True
                reasons.append(f"your_ref matched multiple matters ({len(uniq_mids)})")
            candidate_ids.update(uniq_mids)
            weight = int(_MATCH_SCORE_WEIGHTS.get("your_ref") or 0)
            for mid in uniq_mids:
                scores[mid] = int(scores.get(mid) or 0) + weight
                matched_keys_by_mid.setdefault(mid, set()).add("your_ref")

    if not candidate_ids:
        return None, [], [], "failed", "LOW"

    ranked = sorted(scores.items(), key=lambda item: (item[1], item[0]), reverse=True)
    if not ranked:
        return None, sorted(candidate_ids), reasons, "ambiguous", "LOW"

    top_mid, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else None
    tied_top = [mid for mid, score in ranked if score == top_score]
    if len(tied_top) > 1:
        reasons.append(f"score tie at top ({top_score})")
        return None, sorted(candidate_ids), reasons, "ambiguous", "LOW"

    margin = top_score if second_score is None else (top_score - second_score)
    if second_score is not None and margin < _CANDIDATE_SCORE_MIN_MARGIN:
        reasons.append(f"top score margin too small ({margin})")
        return None, sorted(candidate_ids), reasons, "ambiguous", "LOW"

    top_keys = matched_keys_by_mid.get(top_mid, set())
    method = "rule_app" if "app_no" in top_keys else "rule_identifier"
    confidence = "LOW" if has_ambiguous_hits else "MEDIUM"
    reasons.append(f"selected by weighted score {top_score} (margin={margin})")
    return top_mid, sorted(candidate_ids), reasons, method, confidence


def _finalize_match_result(
    result: CaseMatchResult,
    *,
    text: str = "",
    filename: str = "",
    source_type: str = "generic",
) -> CaseMatchResult:
    """Normalize the result object before returning it from the unified matcher."""
    result.candidate_ids = sorted({str(mid) for mid in (result.candidate_ids or []) if mid})
    result.candidate_reasons = [
        str(reason) for reason in (result.candidate_reasons or []) if str(reason or "").strip()
    ]
    if text and not result.text_snippet:
        result.text_snippet = text[:200]
    if filename:
        result.extracted_info.setdefault("source_filename", filename)
    if source_type:
        result.extracted_info.setdefault("source_type", source_type)
    return result


def match_case_from_text(
    text: str, filename: str = ""
) -> Tuple[Optional[str], Dict[str, Any], str]:
    """
    Main logic to match a case from text and filename.
    Returns: (matter_id, extracted_info, method)
    method: "rule_ref", "rule_app", "llm"
    """

    # 0. Extract from Filename
    ids_filename = extract_identifiers_rule_based(filename)

    # 1. Extract from Text
    ids_text = _extract_identifiers_with_reply_history_fallback(text)

    # Merge identifiers (Filename takes precedence as it's often more explicit? Or text?)
    # Usually text is more reliable for App No, Filename for Ref.
    ids = {
        "our_ref": ids_filename["our_ref"] or ids_text["our_ref"],
        "your_ref": ids_text.get("your_ref") or ids_filename.get("your_ref"),
        "app_no": ids_text["app_no"] or ids_filename["app_no"],  # Text app no usually better
        "publication_no": ids_text.get("publication_no") or ids_filename.get("publication_no"),
        "registration_no": ids_text.get("registration_no") or ids_filename.get("registration_no"),
        "pct_no": ids_text.get("pct_no") or ids_filename.get("pct_no"),
        "agent_ref": ids_text.get("agent_ref") or ids_filename.get("agent_ref"),
        "client_ref": ids_text.get("client_ref") or ids_filename.get("client_ref"),
    }
    extracted = ExtractedIdentifiers(**ids)

    # 2. Try Match by Our Ref
    if extracted.our_ref:
        m = find_case_by_ref(extracted.our_ref)
        if m:
            return m.matter_id, extracted.to_dict(), "rule_ref"

    # 3. Try Match by App No
    if extracted.app_no:
        c = find_case_by_app_no(extracted.app_no)
        if c:
            return c.matter_id, extracted.to_dict(), "rule_app"

    candidate_id, candidate_ids, reasons, method, _confidence = _merge_identifier_candidates(
        extracted_ids=extracted
    )
    if candidate_id:
        info = extracted.to_dict()
        info["candidate_ids"] = candidate_ids
        info["candidate_reasons"] = reasons
        return candidate_id, info, method

    # 4. Fallback to LLM (if configured)
    return None, extracted.to_dict(), "failed"


def process_upload_auto_match(
    file_content: bytes, filename: str, api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Process an uploaded file to find its matching case and parameters.
    """

    # 1. Extract Text
    text = ""
    if filename.lower().endswith(".pdf"):
        text = extract_text_from_pdf(file_content, cache_key=_sha256_bytes(file_content))
    elif filename.lower().endswith(".txt"):
        text = file_content.decode("utf-8", errors="ignore")
    elif filename.lower().endswith(".eml"):
        _, text = _extract_from_eml(file_content)

    if not text:
        # Even if text extraction failed, try matching by filename!
        pass

    # 2. Try Rule-Based Match (Text + Filename)
    matter_id, info, method = match_case_from_text(text, filename)

    if matter_id:
        return {
            "match": True,
            "matter_id": matter_id,
            "method": method,
            "extracted": info,
            "text_snippet": text[:200],
        }

    # 3. Try LLM Match if Rule Failed and API Key provided
    if api_key:
        try:
            if _looks_like_uspto(text, filename):
                parsed = parse_uspto_form(text, filename=filename, api_key=api_key)
                llm_res = _uspto_result_to_match_payload(parsed)
                llm_method = parsed.parser
            else:
                llm_res, llm_method = parse_foreign_identifiers(text, api_key=api_key)
                llm_res = _merge_foreign_identifiers(llm_res)

            # Check if LLM found something new
            if llm_res.get("our_ref"):
                m = find_case_by_ref(llm_res["our_ref"])
                if m:
                    return {
                        "match": True,
                        "matter_id": m.matter_id,
                        "method": "llm_ref",
                        "extracted": llm_res,
                    }

            if llm_res.get("app_no"):
                c = find_case_by_app_no(llm_res["app_no"])
                if c:
                    return {
                        "match": True,
                        "matter_id": c.matter_id,
                        "method": "llm_app",
                        "extracted": llm_res,
                    }

            return {
                "match": False,
                "reason": "LLM extracted info but no DB match",
                "extracted": llm_res,
            }

        except Exception as e:
            return {"error": f"LLM Error: {str(e)}", "match": False}

    return {"match": False, "reason": "No identifiers found by rules", "extracted": info}


# =============================================================================
# Source-Type Specific Parsers
# =============================================================================


_EML_MAX_DEPTH = 2
_EML_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
_EML_MAX_PDF_ATTACHMENTS = 3
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _zip_safety_limits() -> ZipSafetyLimits:
    return get_limits()


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _apply_identifiers(
    target: ExtractedIdentifiers,
    payload: dict[str, str] | None,
    *,
    keys: tuple[str, ...] = _IDENTIFIER_FIELD_KEYS,
    overwrite: bool = False,
) -> None:
    if not isinstance(payload, dict):
        return
    for key in keys:
        if key not in _EXTRACTED_IDENTIFIER_FIELDS:
            continue
        raw = payload.get(key)
        if raw is None:
            continue
        value = str(raw).strip() if isinstance(raw, str) else raw
        if not value:
            continue
        current = getattr(target, key, None)
        if current and not overwrite:
            continue
        setattr(target, key, value)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_pdf_attachment(content_type: str, filename: str) -> bool:
    if content_type == "application/pdf":
        return True
    return filename.lower().endswith(".pdf")


def _is_zip_attachment(content_type: str, filename: str) -> bool:
    if content_type in ("application/zip", "application/x-zip-compressed"):
        return True
    return filename.lower().endswith(".zip")


def _extract_pdf_text_from_zip(zip_bytes: bytes, *, remaining_pdfs: int) -> tuple[str, int]:
    if remaining_pdfs <= 0:
        return "", remaining_pdfs
    text_parts: list[str] = []
    limits = _zip_safety_limits()
    try:
        entries = safe_list(zip_bytes, limits)
    except ZipSafetyError as exc:
        logger.warning("Unsafe ZIP attachment rejected: %s", exc)
        return "", remaining_pdfs
    except Exception:
        return "", remaining_pdfs

    for name in entries:
        if remaining_pdfs <= 0:
            break
        if not name.lower().endswith(".pdf"):
            continue
        try:
            pdf_data = safe_extract_bytes(zip_bytes, name, limits)
        except ZipSafetyError as exc:
            logger.warning("Unsafe ZIP member rejected (%s): %s", name, exc)
            continue
        except Exception:
            continue
        try:
            text = extract_text_from_pdf(pdf_data, cache_key=_sha256_bytes(pdf_data))
        except Exception:
            text = ""
        if text:
            text_parts.append(text)
        remaining_pdfs -= 1
    return "\n".join(text_parts), remaining_pdfs


def _build_eml_search_text(eml_bytes: bytes) -> str:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(eml_bytes)
    except Exception:
        return eml_bytes.decode("utf-8", errors="ignore")

    fragments: list[str] = []
    remaining_pdfs = _EML_MAX_PDF_ATTACHMENTS

    def add_fragment(value: str) -> None:
        if value:
            fragments.append(value)

    def handle_part(part, depth: int) -> None:
        nonlocal remaining_pdfs
        content_type = (part.get_content_type() or "").lower()
        filename = normalize_uploaded_filename(part.get_filename(), default="")
        disposition = (part.get("Content-Disposition") or "").lower()
        is_attachment = "attachment" in disposition or bool(filename)

        if content_type in ("text/plain", "text/html") and not is_attachment:
            try:
                payload = _safe_text(part.get_content())
            except Exception:
                payload = _safe_text(part.get_payload(decode=True))
            if content_type == "text/html":
                payload = _strip_html(payload)
            payload = _trim_reply_history_text(payload)
            add_fragment(payload)
            return

        if filename:
            add_fragment(filename)

        if not is_attachment or remaining_pdfs <= 0:
            return

        data = part.get_payload(decode=True) or b""
        if not data or len(data) > _EML_MAX_ATTACHMENT_BYTES:
            return

        if _is_pdf_attachment(content_type, filename):
            remaining_pdfs -= 1
            add_fragment(extract_text_from_pdf(data, cache_key=_sha256_bytes(data)))
            return

        if _is_zip_attachment(content_type, filename):
            zip_text, remaining_pdfs = _extract_pdf_text_from_zip(
                data, remaining_pdfs=remaining_pdfs
            )
            add_fragment(zip_text)

    def handle_message(message, depth: int) -> None:
        if depth > _EML_MAX_DEPTH:
            return
        add_fragment(decode_mime_encoded_words(message.get("Subject") or "").strip())

        if message.is_multipart():
            for part in message.iter_parts():
                part_type = (part.get_content_type() or "").lower()
                if part_type == "message/rfc822":
                    filename = normalize_uploaded_filename(part.get_filename(), default="")
                    if filename:
                        add_fragment(filename)
                    payload = part.get_payload()
                    if isinstance(payload, list):
                        for nested in payload:
                            handle_message(nested, depth + 1)
                    elif hasattr(payload, "get"):
                        handle_message(payload, depth + 1)
                    else:
                        raw = part.get_payload(decode=True) or b""
                        if raw:
                            try:
                                nested = BytesParser(policy=policy.default).parsebytes(raw)
                                handle_message(nested, depth + 1)
                            except Exception as exc:
                                # Best-effort: skip malformed nested message parts.
                                report_swallowed_exception(
                                    exc,
                                    context="auto_match_service._build_eml_search_text.parse_nested_rfc822",
                                    log_key="auto_match_service._build_eml_search_text.parse_nested_rfc822",
                                    log_window_seconds=300,
                                )
                    continue
                handle_part(part, depth)
        else:
            handle_part(message, depth)

    handle_message(msg, 0)
    return "\n".join(fragments)


def _extract_from_eml(eml_bytes: bytes) -> tuple[ExtractedIdentifiers, str]:
    """Email /from Identifiers """
    search_text = _build_eml_search_text(eml_bytes)
    basic = _extract_identifiers_with_reply_history_fallback(search_text)
    ids = ExtractedIdentifiers(**_normalize_extracted_payload(basic))

    return ids, search_text


# =============================================================================
# Unified Matching Function
# =============================================================================


def match_case_unified(
    text: str = "",
    filename: str = "",
    file_content: Optional[bytes] = None,
    source_type: str = "generic",  # "uspto", "email", "response", "application"
    use_llm: bool = False,
    api_key: Optional[str] = None,
) -> CaseMatchResult:
    """
     Matter Matching 

    Matching :
    1.    to Identifiers 
    2. File Namefrom   → DB Matching (HIGH confidence)
    3. from   → DB Matching (HIGH confidence)
    4. from Application No.  → DB Matching (MEDIUM confidence)
    5. () LLMto Identifiers  → DB Matching (LOW-MEDIUM confidence)

    Args:
        text: Document  (  )
        filename: File Name
        file_content: Source file bytes
        source_type:  Type ("uspto", "email", "response", "application")
        use_llm: LLM   
        api_key: OpenAI API 

    Returns:
        CaseMatchResult: table Matching 
    """
    result = CaseMatchResult()
    extracted_ids = ExtractedIdentifiers()

    # ==========================================================================
    # Step 1: Source-Type Specific Parsing
    # ==========================================================================

    if file_content:
        ext = os.path.splitext(filename)[1].lower() if filename else ""

        if ext == ".pdf" and not text:
            text = extract_text_from_pdf(file_content, cache_key=_sha256_bytes(file_content))

        elif ext in (".txt", ".text") and not text:
            text = file_content.decode("utf-8", errors="ignore")

        elif ext == ".eml":
            try:
                extracted_ids, text = _extract_from_eml(file_content)
            except Exception as exc:
                # Best-effort: keep matching with filename/text if EML parse fails.
                report_swallowed_exception(
                    exc,
                    context="auto_match_service.match_case_from_file.extract_eml",
                    log_key="auto_match_service.match_case_from_file.extract_eml",
                    log_window_seconds=300,
                )

    if text:
        _apply_uspto_identifiers(extracted_ids, text, filename, overwrite=False)

    # ==========================================================================
    # Step 2: Rule-Based Matching from Filename
    # ==========================================================================

    filename_ids = extract_identifiers_rule_based(filename)
    _apply_identifiers(extracted_ids, filename_ids)

    # ==========================================================================
    # Step 3: Rule-Based Matching from Text
    # ==========================================================================

    if text:
        text_ids = _extract_identifiers_with_reply_history_fallback(text)
        # Keep filename our_ref precedence (often explicit in structured file names).
        if text_ids.get("our_ref") and not extracted_ids.our_ref:
            extracted_ids.our_ref = text_ids["our_ref"]
        # Prefer structured parser output, then in-document app_no over filename guess.
        if text_ids.get("app_no") and not extracted_ids.app_no:
            extracted_ids.app_no = text_ids["app_no"]
        # Other secondary identifiers should prefer in-document values when present.
        _apply_identifiers(
            extracted_ids,
            text_ids,
            keys=_IDENTIFIER_SECONDARY_KEYS,
            overwrite=True,
        )

    # Update result with extracted info
    result.extracted_info = extracted_ids.to_dict()
    result.text_snippet = text[:200] if text else ""

    # ==========================================================================
    # Step 4: Database Matching - Our Ref (HIGH confidence)
    # ==========================================================================

    if extracted_ids.our_ref:
        m = find_case_by_ref(extracted_ids.our_ref)
        if m:
            result.matter_id = m.matter_id
            result.match_method = "rule_ref"
            result.confidence = "HIGH"
            return _finalize_match_result(
                result, text=text, filename=filename, source_type=source_type
            )

    # ==========================================================================
    # Step 5: Database Matching - App No (MEDIUM confidence)
    # ==========================================================================

    if extracted_ids.app_no:
        c = find_case_by_app_no(extracted_ids.app_no)
        if c:
            result.matter_id = c.matter_id
            result.match_method = "rule_app"
            result.confidence = "MEDIUM"
            return _finalize_match_result(
                result, text=text, filename=filename, source_type=source_type
            )

    candidate_id, candidate_ids, reasons, method, confidence = _merge_identifier_candidates(
        extracted_ids=extracted_ids
    )
    if candidate_ids:
        result.candidate_ids = candidate_ids
        result.candidate_reasons = reasons
    if candidate_id:
        result.matter_id = candidate_id
        result.match_method = method
        result.confidence = confidence
        return _finalize_match_result(result, text=text, filename=filename, source_type=source_type)
    if candidate_ids and not result.error:
        result.error = "Ambiguous identifier match"

    # ==========================================================================
    # Step 6: LLM Fallback (if enabled)
    # ==========================================================================

    if use_llm and api_key and text:
        _release_read_only_session_before_external_call(
            context="auto_match_service.match_case_unified.pre_llm",
            log_key="auto_match_service.match_case_unified.pre_llm",
        )
        try:
            if _looks_like_uspto(text, filename):
                parsed = parse_uspto_form(text, filename=filename, api_key=api_key)
                llm_res = _uspto_result_to_match_payload(parsed)
                llm_method = parsed.parser
            else:
                llm_res, llm_method = parse_foreign_identifiers(text, api_key=api_key)
                identifiers = llm_res.get("identifiers") or {}
                if identifiers:
                    if identifiers.get("our_ref") and not llm_res.get("our_ref"):
                        llm_res["our_ref"] = identifiers.get("our_ref")
                    if identifiers.get("application_no") and not llm_res.get("app_no"):
                        llm_res["app_no"] = identifiers.get("application_no")
                    if identifiers.get("publication_no") and not llm_res.get("publication_no"):
                        llm_res["publication_no"] = identifiers.get("publication_no")
                    if identifiers.get("registration_no") and not llm_res.get("registration_no"):
                        llm_res["registration_no"] = identifiers.get("registration_no")
                    if identifiers.get("pct_no") and not llm_res.get("pct_no"):
                        llm_res["pct_no"] = identifiers.get("pct_no")
                    if identifiers.get("agent_ref") and not llm_res.get("agent_ref"):
                        llm_res["agent_ref"] = identifiers.get("agent_ref")
                    if identifiers.get("client_ref") and not llm_res.get("client_ref"):
                        llm_res["client_ref"] = identifiers.get("client_ref")
                    if identifiers.get("title") and not llm_res.get("right_name"):
                        llm_res["right_name"] = identifiers.get("title")

            # Merge LLM results
            if llm_res.get("our_ref"):
                m = find_case_by_ref(llm_res["our_ref"])
                if m:
                    result.matter_id = m.matter_id
                    result.match_method = "llm_ref"
                    result.confidence = "MEDIUM"
                    result.extracted_info.update(llm_res)
                    return _finalize_match_result(
                        result, text=text, filename=filename, source_type=source_type
                    )

            if llm_res.get("app_no"):
                c = find_case_by_app_no(llm_res["app_no"])
                if c:
                    result.matter_id = c.matter_id
                    result.match_method = "llm_app"
                    result.confidence = "LOW"
                    result.extracted_info.update(llm_res)
                    return _finalize_match_result(
                        result, text=text, filename=filename, source_type=source_type
                    )

            # Try secondary identifiers from LLM output
            llm_extracted = ExtractedIdentifiers(
                our_ref=llm_res.get("our_ref"),
                your_ref=llm_res.get("your_ref"),
                app_no=llm_res.get("app_no"),
                publication_no=llm_res.get("publication_no"),
                registration_no=llm_res.get("registration_no"),
                pct_no=llm_res.get("pct_no"),
                agent_ref=llm_res.get("agent_ref"),
                client_ref=llm_res.get("client_ref"),
            )
            candidate_id, candidate_ids, reasons, method, confidence = _merge_identifier_candidates(
                extracted_ids=llm_extracted
            )
            if candidate_ids:
                result.candidate_ids = candidate_ids
                result.candidate_reasons = reasons
            if candidate_id:
                result.matter_id = candidate_id
                result.match_method = method
                result.confidence = "LOW" if confidence == "MEDIUM" else confidence
                result.extracted_info.update(llm_res)
                return _finalize_match_result(
                    result, text=text, filename=filename, source_type=source_type
                )

            # LLM extracted info but no match
            result.extracted_info.update(llm_res)
            result.error = "LLM extracted info but no DB match"

        except Exception as e:
            result.error = f"LLM Error: {str(e)}"

    # ==========================================================================
    # No match found
    # ==========================================================================

    result.match_method = "failed"
    result.confidence = "LOW"
    if not result.error:
        result.error = "No identifiers found or no DB match"

    return _finalize_match_result(result, text=text, filename=filename, source_type=source_type)


# =============================================================================
# Helper Functions for Upload Routes
# =============================================================================


def get_matter_by_match_result(result: CaseMatchResult) -> Optional[Matter]:
    """CaseMatchResultfrom Matter  """
    if result.matter_id:
        matter = Matter.query.get(result.matter_id)
        if not matter or getattr(matter, "is_deleted", False):
            return None
        return matter
    return None
