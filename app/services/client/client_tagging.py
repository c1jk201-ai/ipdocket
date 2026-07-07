from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Set

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import openai as _openai

    OpenAIError = getattr(_openai, "OpenAIError", Exception)
except ImportError:
    OpenAIError = Exception

from app.services.core.llm_model_registry import resolve_llm_model

_ASCII_ALPHA_RE = re.compile(r"[A-Za-z]")
_PAREN_SPLIT_RE = re.compile(r"[()\[\]{}<>]")
_TOKEN_SPLIT_RE = re.compile(r"[\s/,&|]+")

_LLM_TAGS_SYSTEM_PROMPT = (
    "You generate search aliases for a company or client name used in CRM search.\n"
    "Return 2-10 short tags as JSON only.\n"
    "Rules:\n"
    "- Include the original name.\n"
    "- Add variants with and without spaces or punctuation.\n"
    "- Add common English spelling, acronym, or abbreviation variants when useful.\n"
    "- Lowercase English tags where that helps search.\n"
    "- Do not add translations or explanations."
)

_LLM_TAGS_JSON_SCHEMA = {
    "name": "CompanyTags",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["tags"],
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    },
    "strict": True,
}

_LLM_TAGS_CACHE: dict[tuple[str, str], List[str]] = {}
_LLM_TAGS_CACHE_MAX = 1024


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def _collapse_spaces(value: str) -> str:
    return " ".join(str(value).split())


def _compact_text(value: str) -> str:
    if not value:
        return ""
    compact = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return compact.replace("_", "")


def _has_ascii_alpha(value: str) -> bool:
    return bool(_ASCII_ALPHA_RE.search(value or ""))


def _ascii_acronym(value: str) -> str:
    if not value or not _has_ascii_alpha(value):
        return ""
    tokens = re.split(r"[\s/,&|._-]+", value)
    letters = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        match = re.search(r"[A-Za-z]", tok)
        if match:
            letters.append(tok[match.start()].upper())
    acronym = "".join(letters)
    return acronym if len(acronym) >= 2 else ""


def _extract_segments(value: str) -> List[str]:
    value = _normalize_text(value)
    if not value:
        return []

    segments: List[str] = []
    for part in _PAREN_SPLIT_RE.split(value):
        part = _collapse_spaces(part)
        if not part:
            continue
        segments.append(part)
        for token in _TOKEN_SPLIT_RE.split(part):
            token = _collapse_spaces(token)
            if token and token not in segments:
                segments.append(token)
    return segments


def _heuristic_tags(value: str) -> Set[str]:
    tags: Set[str] = set()
    for segment in _extract_segments(value):
        cleaned = _collapse_spaces(segment).strip("\"'`")
        if not cleaned:
            continue
        tags.add(cleaned)

        compact = _compact_text(cleaned)
        if compact:
            tags.add(compact)

        if _has_ascii_alpha(cleaned):
            tags.add(cleaned.lower())
            if compact:
                tags.add(compact.lower())
            acronym = _ascii_acronym(cleaned)
            if acronym:
                tags.add(acronym)
                tags.add(acronym.lower())

    return tags


def _generate_company_name_tags_llm(
    name: str, api_key: str, *, model: str | None = None
) -> List[str]:
    if not name or not api_key or OpenAI is None:
        return []
    model = (model or "").strip() or resolve_llm_model("client_tagging")
    cache_key = (model, _normalize_text(name).casefold())
    cached = _LLM_TAGS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _LLM_TAGS_SYSTEM_PROMPT},
                {"role": "user", "content": f"Company name: {name}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": _LLM_TAGS_JSON_SCHEMA,
            },
            temperature=0,
        )
        import json

        payload = json.loads(response.choices[0].message.content)
        tags = payload.get("tags", [])
        if isinstance(tags, list):
            out = [str(tag).strip() for tag in tags if str(tag).strip()]
            if out:
                if len(_LLM_TAGS_CACHE) >= _LLM_TAGS_CACHE_MAX:
                    _LLM_TAGS_CACHE.clear()
                _LLM_TAGS_CACHE[cache_key] = out
            return out
    except (OpenAIError, ValueError, TypeError, KeyError, AttributeError):
        return []
    return []


def build_client_search_tags(
    names: Iterable[str],
    *,
    api_key: str | None = None,
    use_llm: bool = False,
    model: str | None = None,
) -> List[str]:
    tags: Set[str] = set()
    normalized_names: List[str] = []
    for name in names:
        normalized = _normalize_text(name)
        if normalized:
            normalized_names.append(normalized)

    for name in normalized_names:
        tags.update(_heuristic_tags(name))

    if use_llm and api_key:
        for name in normalized_names:
            llm_tags = _generate_company_name_tags_llm(name, api_key, model=model)
            for tag in llm_tags:
                tags.update(_heuristic_tags(tag))
            if llm_tags:
                break

    return sorted(tags, key=lambda item: (len(item), item.lower()))


def build_client_search_tags_text(
    names: Iterable[str],
    *,
    api_key: str | None = None,
    use_llm: bool = False,
    model: str | None = None,
) -> str:
    tags = build_client_search_tags(names, api_key=api_key, use_llm=use_llm, model=model)
    return " ".join(tags)
