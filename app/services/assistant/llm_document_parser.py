"""LLM-based document identifier extraction and PDF text extraction helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

try:
    from openai import OpenAI, OpenAIError
except ImportError:
    OpenAI = None
    OpenAIError = Exception

from app.services.core.llm_model_registry import DEFAULT_LLM_MODEL, resolve_llm_model
from app.services.core.llm_runtime import get_openai_api_key
from app.services.mail.foreign_email_schema import (
    FOREIGN_EMAIL_EXTRACT_SYSTEM_PROMPT,
    FOREIGN_EMAIL_EXTRACT_V1_SCHEMA,
    FOREIGN_EMAIL_IDENTIFIERS_SCHEMA,
    FOREIGN_EMAIL_IDENTIFIERS_SYSTEM_PROMPT,
)
from app.utils.docket_dates import normalize_date_str
from app.utils.error_logging import report_swallowed_exception

MIN_LLM_TEXT_CHARS = 200

_LLM_MAX_CHARS = 12000
_PDF_PRIMARY_PAGES = 8
_PDF_FALLBACK_PAGES = 30
_PDF_MAX_CHARS = 12000
_PDF_FALLBACK_MIN_CHARS = 800
_PDF_STOP_HINT_RE = re.compile(
    r"(\b\d{2}-\d{4}-\d{7}\b|"
    r"\b(10|20|30|40)\d{4}\d{7}\b|"
    r"\b\d{2}[A-Z]{1,2}[-]?\d{3,4}[-]?[A-Z]{0,2}\b|"
    r"\b(our\s*ref|your\s*ref|agent\s*ref|client\s*ref|app\s*no\.?|application\s*no\.?)\b|"
    r"\b(reply by|due date|deadline|respond by|response due|date of mailing|mailing date|dispatch date|notification date)\b|"
    r"(Text|Text|Text|期限|期日))",
    re.IGNORECASE,
)
_VISION_OCR_SYSTEM_PROMPT = (
    "You are a document transcription assistant. "
    "Extract all visible text from the image. "
    "Return only the text with line breaks preserved where possible."
)
_VISION_OCR_MAX_SIDE = 1600
_DEFAULT_TEXT_CACHE_SUBDIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "pdf_text_cache")
)
DEFAULT_OPENAI_MODEL = DEFAULT_LLM_MODEL


def get_default_openai_model() -> str:
    return resolve_llm_model("default")


def _usage_to_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="llm_document_parser._usage_to_dict.model_dump",
                log_key="llm_document_parser._usage_to_dict.model_dump",
                log_window_seconds=300,
            )
    out = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if hasattr(usage, key):
            try:
                out[key] = int(getattr(usage, key))
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="llm_document_parser._usage_to_dict.int",
                    log_key=f"llm_document_parser._usage_to_dict.int.{key}",
                    log_window_seconds=300,
                )
    return out


def _extract_first_json_object(raw: str) -> str:
    start = raw.find("{")
    if start < 0:
        raise ValueError("JSON object not found")
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : idx + 1]
    raise ValueError("unterminated JSON object")


def _json_object_from_content(content: Any) -> dict:
    if isinstance(content, dict):
        return dict(content)
    raw = str(content or "").strip()
    if not raw:
        raise ValueError("empty LLM content")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = json.loads(_extract_first_json_object(raw))
    if not isinstance(parsed, dict):
        raise ValueError("LLM content is not a JSON object")
    for key in ("structured_json", "extraction", "result", "data", "payload"):
        inner = parsed.get(key)
        if isinstance(inner, dict) and "params" in inner:
            return dict(inner)
    return parsed


def _normalize_foreign_email_extract_payload(payload: dict | None) -> dict:
    out = dict(payload or {}) if isinstance(payload, dict) else {}
    evidence_map = out.get("evidence_map")
    if isinstance(evidence_map, dict):
        out["evidence_map"] = dict(evidence_map)
        return out
    if not isinstance(evidence_map, list):
        out["evidence_map"] = {}
        return out

    normalized: dict[str, dict[str, Any]] = {}
    for item in evidence_map:
        if not isinstance(item, dict):
            continue
        field_path = str(item.get("field_path") or "").strip()
        if not field_path:
            continue
        evidence = {k: v for k, v in item.items() if k != "field_path"}
        if field_path not in normalized:
            normalized[field_path] = evidence
            continue
        extra = normalized[field_path].setdefault("additional_evidence", [])
        if isinstance(extra, list):
            extra.append(evidence)
    out["evidence_map"] = normalized
    return out


def _first_regex_match(pattern: str, text: str, flags: int = re.IGNORECASE) -> re.Match | None:
    try:
        return re.search(pattern, text or "", flags)
    except Exception:
        return None


def _build_rule_based_foreign_email_extract(
    text: str,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    schema: dict | None = None,
    warning: str | None = None,
) -> dict:
    _ = attachments, case_context, schema
    raw = text or ""
    our_ref_match = _first_regex_match(
        r"\b\d{2}[A-Z]{1,3}[-]?\d{3,4}[-]?(?:[A-Z]{0,3}|PCT)\b",
        raw,
    )
    app_match = _first_regex_match(r"\b(?:10|20|30|40)-\d{4}-\d{7}\b", raw)
    if not app_match:
        app_match = _first_regex_match(r"\b((?:10|20|30|40)\d{4}\d{7})\b", raw)
    title_match = _first_regex_match(r"(?im)^\s*(?:title|invention|mark)\s*[:：]\s*(.+?)\s*$", raw)
    due_match = _first_regex_match(
        r"(?i)\b(?:response\s+due\s+by|due\s+date|deadline|reply\s+by|respond\s+by)"
        r"\s*[:：]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",
        raw,
    )

    our_ref = our_ref_match.group(0).replace("-", "").upper() if our_ref_match else ""
    app_no = ""
    if app_match:
        app_no = app_match.group(0)
        digits = re.sub(r"\D", "", app_no)
        if len(digits) == 13:
            app_no = f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    right_name = title_match.group(1).strip() if title_match else ""
    due_date = ""
    if due_match:
        due_date = normalize_date_str(due_match.group(1)) or due_match.group(1).replace("/", "-")

    params: dict[str, Any] = {}
    match_keys: dict[str, Any] = {}
    if our_ref:
        match_keys["our_ref"] = our_ref
        params["our_ref"] = our_ref
    if app_no:
        match_keys["application_no"] = app_no
        params["application_no"] = app_no
    if right_name:
        params["right_name"] = right_name
    if due_date:
        params["response_deadline"] = due_date

    dockets: list[dict[str, Any]] = []
    evidence_map: dict[str, dict[str, Any]] = {}
    if due_date:
        dockets.append({"name": "Response deadline", "due_date": due_date, "type": "response"})
        start, end = due_match.span(1) if due_match else (0, 0)
        snippet_start = max(0, start - 40)
        snippet_end = min(len(raw), end + 40)
        evidence = {
            "attachment_sha256": None,
            "page": None,
            "snippet": raw[snippet_start:snippet_end].strip(),
            "char_start": start,
            "char_end": end,
        }
        evidence_map["params.response_deadline"] = dict(evidence)
        evidence_map["dockets[0].due_date"] = dict(evidence)

    warnings: list[str] = []
    if warning:
        warnings.append(warning)
    if not match_keys:
        warnings.append("Matter match missing")

    return {
        "schema": "foreign_email_extract_v1",
        "meta": dict(meta or {}),
        "case_target": {"match_keys": match_keys},
        "params": params,
        "dockets": dockets,
        "evidence_map": evidence_map,
        "warnings": warnings,
    }


def estimate_openai_cost(model: str, usage: dict) -> float | None:
    if not usage:
        return None
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    try:
        prompt = int(prompt)
        completion = int(completion)
    except Exception:
        return None

    model_key = re.sub(r"[^A-Za-z0-9]+", "_", (model or "").upper()).strip("_")

    def _read_rate(suffix: str) -> float | None:
        env_key = f"OPENAI_COST_{model_key}_{suffix}"
        raw = os.environ.get(env_key)
        if raw is None:
            raw = os.environ.get(f"OPENAI_COST_DEFAULT_{suffix}")
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    in_rate = _read_rate("IN")
    out_rate = _read_rate("OUT")
    if in_rate is None and out_rate is None:
        return None
    if in_rate is None:
        in_rate = 0.0
    if out_rate is None:
        out_rate = 0.0

    return (prompt / 1000.0) * in_rate + (completion / 1000.0) * out_rate


def _resolve_cache_dir(cache_dir: str | None = None) -> Path:
    if cache_dir:
        return Path(cache_dir)
    try:
        from flask import current_app

        if current_app:
            configured = current_app.config.get("PDF_TEXT_CACHE_DIR") or ""
            if configured:
                return Path(configured)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="llm_document_parser._resolve_cache_dir.flask_config",
            log_key="llm_document_parser._resolve_cache_dir.flask_config",
            log_window_seconds=300,
        )
    configured = os.environ.get("PDF_TEXT_CACHE_DIR") or ""
    return Path(configured) if configured else Path(_DEFAULT_TEXT_CACHE_SUBDIR)


def _is_cache_enabled(cache_enabled: bool | None = None) -> bool:
    if cache_enabled is not None:
        return bool(cache_enabled)
    try:
        from flask import current_app

        if current_app:
            return bool(current_app.config.get("PDF_TEXT_CACHE_ENABLED", True))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="llm_document_parser._is_cache_enabled.flask_config",
            log_key="llm_document_parser._is_cache_enabled.flask_config",
            log_window_seconds=300,
        )
    raw = os.environ.get("PDF_TEXT_CACHE_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _cache_path(cache_dir: Path, cache_key: str) -> Path:
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "", cache_key)
    return cache_dir / f"{safe_key}.txt"


_CACHE_KEY_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _normalize_cache_key(cache_key: str) -> str:
    if not cache_key:
        return ""
    if _CACHE_KEY_RE.fullmatch(cache_key):
        return cache_key.lower()
    try:
        return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _read_cached_text(cache_dir: Path, cache_key: str) -> str | None:
    if not cache_key:
        return None
    try:
        path = _cache_path(cache_dir, cache_key)
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        return None
    return None


def _write_cached_text(cache_dir: Path, cache_key: str, text: str) -> None:
    if not cache_key or not text:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(cache_dir, cache_key)
        path.write_text(text, encoding="utf-8")
    except Exception:
        return


def _estimate_scanned_prob(text: str) -> float:
    if not text:
        return 1.0
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return 1.0
    meaningful = len(re.findall(r"[A-Za-z0-9Text-Text]", compact))
    ratio = meaningful / len(compact)
    return max(0.0, min(1.0, 1.0 - ratio))


def _resolve_vision_min_scanned_prob(value: float | None) -> float:
    if value is not None:
        try:
            return float(value)
        except Exception:
            return 0.6
    try:
        from flask import current_app

        if current_app:
            raw = current_app.config.get("OCR_VISION_MIN_SCANNED_PROB")
            if raw is not None:
                return float(raw)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="llm_document_parser._resolve_vision_min_scanned_prob.flask_config",
            log_key="llm_document_parser._resolve_vision_min_scanned_prob.flask_config",
            log_window_seconds=300,
        )
    raw = os.environ.get("OCR_VISION_MIN_SCANNED_PROB")
    if raw is None:
        return 0.6
    try:
        return float(raw)
    except Exception:
        return 0.6


def parse_foreign_identifiers_from_text(text: str, api_key: str) -> dict:
    """
    LLM-based identifier extraction for foreign emails/documents.
    Uses structured output to reduce hallucinations.
    """
    if OpenAI is None:
        raise RuntimeError("OpenAI package is not installed.")
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=get_default_openai_model(),
            messages=[
                {"role": "system", "content": FOREIGN_EMAIL_IDENTIFIERS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Extract foreign filing identifiers from this text:\n\n{text[:_LLM_MAX_CHARS]}",
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": FOREIGN_EMAIL_IDENTIFIERS_SCHEMA,
            },
            temperature=0,
        )
        return json.loads(response.choices[0].message.content)
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
        raise Exception(f"LLM identifier extraction failed: {str(e)}")


def parse_foreign_identifiers(text: str, api_key: Optional[str] = None) -> tuple[dict, str]:
    """
    Parse foreign identifiers with LLM-only fallback (rule-based handled elsewhere).
    """
    if api_key and text and len(text.strip()) >= MIN_LLM_TEXT_CHARS:
        try:
            llm_result = parse_foreign_identifiers_from_text(text, api_key)
            return llm_result, "llm"
        except Exception as exc:
            # Best-effort: allow rule-based pipeline to proceed without LLM identifiers.
            report_swallowed_exception(
                exc,
                context="llm_document_parser.parse_foreign_identifiers.llm_fallback",
                log_key="llm_document_parser.parse_foreign_identifiers.llm_fallback",
                log_window_seconds=300,
            )
    return {"identifiers": {}}, "rule"


def _parse_foreign_email_extract_from_text_impl(
    text: str,
    api_key: str,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
    schema: dict | None = None,
    return_usage: bool = True,
) -> tuple[dict, dict]:
    """
    LLM-based extraction for foreign email automation (foreign_email_extract_v1).
    Returns (parsed_json, usage_dict).
    """
    if OpenAI is None:
        raise RuntimeError("OpenAI package is not installed.")
    if not api_key:
        raise ValueError("OpenAI API key is not configured.")

    context_parts = []
    if meta:
        context_parts.append(f"Email meta:\n{json.dumps(meta, ensure_ascii=False)}")
    if attachments:
        context_parts.append(f"Attachments:\n{json.dumps(attachments, ensure_ascii=False)}")
    if case_context:
        context_parts.append(
            "Linked matter context (authoritative current case state; not email evidence; "
            "do not copy this into output meta):\n"
            f"{json.dumps(case_context, ensure_ascii=False)}"
        )
    context_parts.append(f"Content:\n{text[:_LLM_MAX_CHARS]}")
    content = "\n\n".join(context_parts)

    client = OpenAI(api_key=api_key)
    model = (model_name or "").strip() or get_default_openai_model()
    prompt = system_prompt or FOREIGN_EMAIL_EXTRACT_SYSTEM_PROMPT
    schema_payload = schema or FOREIGN_EMAIL_EXTRACT_V1_SCHEMA
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": schema_payload,
            },
            temperature=0,
        )
        payload = _normalize_foreign_email_extract_payload(
            _json_object_from_content(response.choices[0].message.content)
        )
        usage = _usage_to_dict(getattr(response, "usage", None))
        return payload, usage
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError) as e:
        raise Exception(f"LLM email extraction failed: {str(e)}")


def parse_foreign_email_extract_from_text(
    text: str,
    api_key: str,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
    schema: dict | None = None,
) -> dict:
    payload, _usage = _parse_foreign_email_extract_from_text_impl(
        text,
        api_key,
        meta=meta,
        attachments=attachments,
        case_context=case_context,
        model_name=model_name,
        system_prompt=system_prompt,
        schema=schema,
        return_usage=False,
    )
    return payload


def parse_foreign_email_extract_from_text_with_usage(
    text: str,
    api_key: str,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
    schema: dict | None = None,
) -> tuple[dict, dict]:
    payload, usage = _parse_foreign_email_extract_from_text_impl(
        text,
        api_key,
        meta=meta,
        attachments=attachments,
        case_context=case_context,
        model_name=model_name,
        system_prompt=system_prompt,
        schema=schema,
        return_usage=True,
    )
    return payload, usage


def parse_foreign_email_extract(
    text: str,
    api_key: Optional[str] = None,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
    schema: dict | None = None,
) -> tuple[dict, str]:
    """
    Parse foreign email/document using structured extraction schema.
    """
    if api_key and text and len(text.strip()) >= MIN_LLM_TEXT_CHARS:
        try:
            llm_result = parse_foreign_email_extract_from_text(
                text,
                api_key,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                model_name=model_name,
                system_prompt=system_prompt,
                schema=schema,
            )
            if llm_result:
                return llm_result, "llm"
            rule_result = _build_rule_based_foreign_email_extract(
                text,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                schema=schema,
                warning="rule_based_fallback:llm_empty",
            )
            if rule_result:
                return rule_result, "rule_after_llm_empty"
        except Exception as exc:
            # Best-effort: fall back to rule result if LLM extraction fails.
            report_swallowed_exception(
                exc,
                context="llm_document_parser.parse_foreign_email_extract.llm_fallback",
                log_key="llm_document_parser.parse_foreign_email_extract.llm_fallback",
                log_window_seconds=300,
            )
            rule_result = _build_rule_based_foreign_email_extract(
                text,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                schema=schema,
                warning="rule_based_fallback:llm_failed",
            )
            if rule_result:
                return rule_result, "rule_after_llm_failed"
    rule_result = _build_rule_based_foreign_email_extract(
        text,
        meta=meta,
        attachments=attachments,
        case_context=case_context,
        schema=schema,
        warning="rule_based_fallback:no_llm",
    )
    if rule_result:
        return rule_result, "rule"
    return {}, "rule"


def parse_foreign_email_extract_with_usage(
    text: str,
    api_key: Optional[str] = None,
    *,
    meta: dict | None = None,
    attachments: list[dict] | None = None,
    case_context: dict | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
    schema: dict | None = None,
) -> tuple[dict, str, dict]:
    if api_key and text and len(text.strip()) >= MIN_LLM_TEXT_CHARS:
        try:
            llm_result, usage = parse_foreign_email_extract_from_text_with_usage(
                text,
                api_key,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                model_name=model_name,
                system_prompt=system_prompt,
                schema=schema,
            )
            if llm_result:
                return llm_result, "llm", usage
            rule_result = _build_rule_based_foreign_email_extract(
                text,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                schema=schema,
                warning="rule_based_fallback:llm_empty",
            )
            if rule_result:
                return rule_result, "rule_after_llm_empty", usage
        except Exception as exc:
            # Best-effort: fall back to rule result if LLM extraction fails.
            report_swallowed_exception(
                exc,
                context="llm_document_parser.parse_foreign_email_extract_with_usage.llm_fallback",
                log_key="llm_document_parser.parse_foreign_email_extract_with_usage.llm_fallback",
                log_window_seconds=300,
            )
            rule_result = _build_rule_based_foreign_email_extract(
                text,
                meta=meta,
                attachments=attachments,
                case_context=case_context,
                schema=schema,
                warning="rule_based_fallback:llm_failed",
            )
            if rule_result:
                return rule_result, "rule_after_llm_failed", {}
    rule_result = _build_rule_based_foreign_email_extract(
        text,
        meta=meta,
        attachments=attachments,
        case_context=case_context,
        schema=schema,
        warning="rule_based_fallback:no_llm",
    )
    if rule_result:
        return rule_result, "rule", {}
    return {}, "rule", {}


def _needs_pdf_fallback(text: str) -> bool:
    if not text:
        return True
    compact = text.strip()
    if len(compact) < _PDF_FALLBACK_MIN_CHARS:
        return True
    return _PDF_STOP_HINT_RE.search(compact) is None


def _is_text_readable(text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 30:
        return False
    readable = len(re.findall(r"[^\W_]", compact, flags=re.UNICODE))
    alnum = len(re.findall(r"[A-Za-z0-9]", compact))
    return max(readable, alnum) / len(compact) > 0.3


def _resolve_openai_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key
    return get_openai_api_key()


def _get_vision_config(
    ocr_enabled: bool | None,
    ocr_dpi: int | None,
    ocr_langs: str | None,
    ocr_max_pages: int | None,
) -> tuple[bool, int, int]:
    # Uses OCR_* config keys for backward compatibility.
    _ = ocr_langs
    enabled = ocr_enabled
    dpi = ocr_dpi
    max_pages = ocr_max_pages
    try:
        from flask import current_app

        if current_app:
            if enabled is None:
                enabled = bool(current_app.config.get("OCR_ENABLED", True))
            if dpi is None:
                dpi = int(current_app.config.get("OCR_DPI", 200) or 200)
            if max_pages is None:
                max_pages = int(current_app.config.get("OCR_MAX_PAGES", 2) or 2)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="llm_document_parser._get_vision_config.flask_config",
            log_key="llm_document_parser._get_vision_config.flask_config",
            log_window_seconds=300,
        )

    if enabled is None:
        raw = os.environ.get("OCR_ENABLED")
        enabled = (
            str(raw).strip().lower() in ("1", "true", "yes", "on") if raw is not None else True
        )
    if dpi is None:
        try:
            dpi = int(os.environ.get("OCR_DPI", "200"))
        except Exception:
            dpi = 200
    if max_pages is None:
        try:
            max_pages = int(os.environ.get("OCR_MAX_PAGES", "2"))
        except Exception:
            max_pages = 2
    return bool(enabled), int(dpi), int(max_pages)


def _image_to_data_url(img, max_side: int = _VISION_OCR_MAX_SIDE) -> str | None:
    try:
        from PIL import Image

        if not isinstance(img, Image.Image):
            return None
    except Exception:
        return None

    try:
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
    except (AttributeError, ValueError, TypeError, OSError):
        pass
    try:
        w, h = img.size
        m = max(w, h)
        if max_side and m > max_side:
            scale = max_side / float(m)
            img = img.resize((int(w * scale), int(h * scale)))
    except (AttributeError, ValueError, TypeError, OSError):
        pass

    try:
        import base64
        from io import BytesIO

        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return None


def _extract_text_from_image_vision(img, api_key: str) -> str:
    if OpenAI is None or not api_key:
        return ""
    data_url = _image_to_data_url(img)
    if not data_url:
        return ""

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=get_default_openai_model(),
            messages=[
                {"role": "system", "content": _VISION_OCR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract the document text only."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            temperature=0,
        )
        return (response.choices[0].message.content or "").strip()
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError):
        return ""


def _extract_text_from_pdf_vision(
    pdf_bytes: bytes,
    *,
    max_pages: int,
    max_chars: int,
    dpi: int,
    api_key: str,
) -> str:
    try:
        import pypdfium2 as pdfium
    except Exception:
        return ""

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception:
        return ""

    text_parts: list[str] = []
    char_count = 0
    limit = min(len(pdf), max_pages)
    scale = max(dpi, 72) / 72.0

    for idx in range(limit):
        try:
            page = pdf[idx]
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            text = _extract_text_from_image_vision(img, api_key=api_key)
        except Exception:
            text = ""
        if text:
            text_parts.append(text)
            char_count += len(text)
        if char_count >= max_chars:
            break

    return "\n".join(text_parts)


def extract_text_from_pdf(
    pdf_bytes: bytes,
    *,
    max_pages: int = _PDF_PRIMARY_PAGES,
    fallback_max_pages: int = _PDF_FALLBACK_PAGES,
    max_chars: int = _PDF_MAX_CHARS,
    min_pages: int = 1,
    ocr_enabled: bool | None = None,
    ocr_dpi: int | None = None,
    ocr_langs: str | None = None,
    ocr_max_pages: int | None = None,
    api_key: str | None = None,
    cache_key: str | None = None,
    cache_dir: str | None = None,
    cache_enabled: bool | None = None,
    vision_min_scanned_prob: float | None = None,
) -> str:
    """
    Extract text from PDF bytes using pypdf.
    Falls back to LLM vision transcription when text is unreadable and API key exists.
    """
    try:
        enabled_cache = _is_cache_enabled(cache_enabled)
        resolved_cache_dir = _resolve_cache_dir(cache_dir)
        cache_key_value = _normalize_cache_key((cache_key or "").strip())
        if enabled_cache and cache_key_value:
            cached = _read_cached_text(resolved_cache_dir, cache_key_value)
            if cached:
                return cached[:max_chars]

        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(pdf_bytes))
        text_parts = []
        total_pages = len(reader.pages)
        char_count = 0
        stop_hint = False

        primary_limit = min(max_pages, total_pages)
        scan_limit = min(max(fallback_max_pages, max_pages), total_pages)

        for idx in range(scan_limit):
            try:
                page_text = reader.pages[idx].extract_text() or ""
            except Exception:
                page_text = ""
            if page_text:
                text_parts.append(page_text)
                char_count += len(page_text)
                if _PDF_STOP_HINT_RE.search(page_text):
                    stop_hint = True
            if char_count >= max_chars:
                break
            if stop_hint and (idx + 1) >= max(min_pages, 1):
                break
            if idx + 1 >= primary_limit:
                text = "\n".join(text_parts)
                if not _needs_pdf_fallback(text):
                    break

        text = "\n".join(text_parts)[:max_chars]

        vision_on, vision_dpi_value, vision_pages = _get_vision_config(
            ocr_enabled, ocr_dpi, ocr_langs, ocr_max_pages
        )
        scanned_prob = _estimate_scanned_prob(text)
        min_scanned_prob = _resolve_vision_min_scanned_prob(vision_min_scanned_prob)
        if vision_on and scanned_prob >= min_scanned_prob and not _is_text_readable(text):
            vision_key = _resolve_openai_api_key(api_key)
            if not vision_key:
                if enabled_cache and cache_key_value and text:
                    _write_cached_text(resolved_cache_dir, cache_key_value, text)
                return text
            vision_text = _extract_text_from_pdf_vision(
                pdf_bytes,
                max_pages=max(1, vision_pages),
                max_chars=max_chars,
                dpi=vision_dpi_value,
                api_key=vision_key,
            )
            if vision_text:
                if not text or _is_text_readable(vision_text):
                    text = vision_text[:max_chars]

        if enabled_cache and cache_key_value and text:
            _write_cached_text(resolved_cache_dir, cache_key_value, text)
        return text
    except Exception:
        return ""
