from __future__ import annotations

import re

_ASCII_ALPHA_RE = re.compile(r"[A-Za-z]")


def _collapse_spaces(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _has_ascii_alpha(value: str) -> bool:
    return bool(_ASCII_ALPHA_RE.search(value or ""))


def normalize_client_name(
    name: str | None,
    *,
    api_key: str | None = None,
    use_llm: bool = True,
) -> dict[str, str]:
    """Return normalized client-name fields for the U.S. workflow."""

    original = _collapse_spaces(name)
    if not original:
        return {"name": "", "client_name": "", "name_en": ""}

    return {
        "name": original,
        "client_name": original,
        "name_en": original if _has_ascii_alpha(original) else "",
    }
