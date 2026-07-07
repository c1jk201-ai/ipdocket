from __future__ import annotations

import os

from flask import current_app, has_app_context

from app.models.system_config import SystemConfig
from app.services.core.config_service import ConfigService

DEFAULT_LLM_MODEL = "gpt-4o-mini"
LLM_DEFAULT_KEY = "LLM_DEFAULT_MODEL"
LLM_DEFAULT_SLUG = "default"
_KNOWN_LLM_SLUGS = {
    LLM_DEFAULT_SLUG,
    "billing_invoice",
    "client_tagging",
    "foreign_email",
    "notice_due_policy",
}


def _normalize_nonblank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def resolve_llm_model(slug: str) -> str:
    if slug not in _KNOWN_LLM_SLUGS:
        raise KeyError(f"Unknown LLM model spec: {slug}")
    value = _normalize_nonblank(ConfigService.get_raw(LLM_DEFAULT_KEY, None, allow_blank=False))
    if value:
        return value
    return DEFAULT_LLM_MODEL


def _read_key_source(key: str) -> tuple[str | None, str | None]:
    if has_app_context():
        try:
            row = SystemConfig.query.filter_by(key=key).first()
        except Exception:
            row = None
        value = _normalize_nonblank(getattr(row, "value", None))
        if value:
            return value, "system_config"

        try:
            value = _normalize_nonblank(current_app.config.get(key))
        except Exception:
            value = None
        if value:
            return value, "app_config"

    value = _normalize_nonblank(os.environ.get(key))
    if value:
        return value, "env"
    return None, None


def describe_llm_model_settings() -> list[dict[str, str]]:
    configured_value, configured_source = _read_key_source(LLM_DEFAULT_KEY)
    effective_value = configured_value or DEFAULT_LLM_MODEL
    effective_source = configured_source or "default"
    return [
        {
            "slug": LLM_DEFAULT_SLUG,
            "key": LLM_DEFAULT_KEY,
            "label": "Default LLM model",
            "description": "Shared fallback model for LLM-backed parsing and enrichment.",
            "configured_value": configured_value or "",
            "configured_source": configured_source or "",
            "effective_value": effective_value,
            "effective_source": effective_source,
            "resolved_from_key": LLM_DEFAULT_KEY,
            "fallback_chain": f"{LLM_DEFAULT_KEY} -> {DEFAULT_LLM_MODEL}",
        }
    ]
