from __future__ import annotations

from app.services.core.config_service import ConfigService

DEFAULT_LLM_INPUT_MAX_CHARS = 12000
LLM_INPUT_MAX_CHARS_KEY = "LLM_INPUT_MAX_CHARS"

OPENAI_API_KEY_CANDIDATES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ASSISTANT_OPENAI_API_KEY",
)


def get_openai_api_key(*, allow_legacy: bool = True) -> str:
    keys = OPENAI_API_KEY_CANDIDATES if allow_legacy else OPENAI_API_KEY_CANDIDATES[:1]
    for key in keys:
        value = ConfigService.get_str(key, "", strip=True, allow_blank=False) or ""
        if value:
            return value
    return ""


def has_openai_api_key(*, allow_legacy: bool = True) -> bool:
    return bool(get_openai_api_key(allow_legacy=allow_legacy))


def get_llm_input_max_chars(default: int = DEFAULT_LLM_INPUT_MAX_CHARS) -> int:
    value = ConfigService.get_int(LLM_INPUT_MAX_CHARS_KEY, default, min_value=0)
    if value is None:
        return int(default)
    return int(value)
