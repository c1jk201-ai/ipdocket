from __future__ import annotations

from typing import Any

from flask import current_app, has_app_context

from config import Config


def runtime_config_get(key: str, default: Any = None) -> Any:
    """Read config from the active Flask app when available, then fall back to Config."""
    if has_app_context():
        return current_app.config.get(key, getattr(Config, key, default))
    return getattr(Config, key, default)


def runtime_config_str(key: str, default: str = "", *, strip: bool = True) -> str:
    value = runtime_config_get(key, default)
    if value is None:
        value = default
    text = str(value)
    return text.strip() if strip else text


def runtime_config_int(key: str, default: int = 0) -> int:
    value = runtime_config_get(key, default)
    try:
        text = str(value).strip()
        if not text:
            return int(default)
        return int(text)
    except Exception:
        return int(default)


def runtime_storage_type(default: str = "local") -> str:
    return runtime_config_str("STORAGE_TYPE", default).lower() or default


def runtime_upload_folder(default: str | None = None) -> str:
    fallback = default if default is not None else str(getattr(Config, "UPLOAD_FOLDER", "") or "")
    return runtime_config_str("UPLOAD_FOLDER", fallback)
