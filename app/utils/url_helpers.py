from __future__ import annotations

from urllib.parse import urlparse

from flask import request as _request


def safe_next_url(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.startswith("//"):
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    if not raw.startswith("/"):
        return None
    return raw


def safe_referrer_path(req=None) -> str | None:
    req = req or _request
    try:
        ref = (req.referrer or "").strip()
    except Exception:
        return None
    if not ref:
        return None
    try:
        parsed = urlparse(ref)
    except Exception:
        return None
    if parsed.netloc and not parsed.scheme:
        return None
    if parsed.scheme or parsed.netloc:
        try:
            host = getattr(req, "host", None)
        except Exception:
            host = None
        if not host or parsed.netloc != host:
            return None
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return None
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path
