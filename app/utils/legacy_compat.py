"""Helpers for explicitly marked legacy compatibility endpoints."""

from __future__ import annotations

from functools import wraps

from flask import current_app


def mark_legacy_response(result, *, compat_id: str, successor: str | None = None):
    response = current_app.make_response(result)
    response.headers.setdefault("Deprecation", "true")
    if successor:
        response.headers.setdefault("Link", f'<{successor}>; rel="successor-version"')
    response.headers.setdefault("X-IPM-Legacy-Compat", compat_id)
    return response


def legacy_compat_endpoint(*, compat_id: str, successor: str | None = None):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            return mark_legacy_response(
                view_func(*args, **kwargs),
                compat_id=compat_id,
                successor=successor,
            )

        return wrapped

    return decorator
