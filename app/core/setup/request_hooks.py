from __future__ import annotations

import logging
import time
import uuid

from flask import Flask, abort, current_app, g, jsonify, redirect, request, session, url_for
from flask_login import current_user

from app.core.setup.access_logging import register_user_access_logging
from app.core.setup.logging_setup import _log_swallowed
from app.extensions import db

logger = logging.getLogger(__name__)

_SESSION_COOKIE_SOFT_LIMIT_BYTES = 3000
_RESPONSE_HEADER_SOFT_LIMIT_BYTES = 3600
_SESSION_FLASH_LIMIT = 5
_SESSION_FLASH_MESSAGE_MAX_CHARS = 300
_SESSION_CRITICAL_KEYS = frozenset(
    {
        "_fresh",
        "_id",
        "_permanent",
        "_remember",
        "_user_id",
        "csrf_token",
        "next",
        "remember",
        "remember_seconds",
    }
)
_SESSION_BULKY_KEYS = frozenset(
    {
        "assistant_last_plan",
        "matter_create_contexts",
        "matter_create_results",
        "matter_create_used_keys",
    }
)
_SESSION_BULKY_PREFIXES = ("assistant_last_",)


def request_path() -> str:
    return (request.path or "").strip() or "/"


def is_static_asset_request(path: str) -> bool:
    endpoint = (getattr(request, "endpoint", None) or "").strip()
    return endpoint == "static" or endpoint.endswith(".static") or path.startswith("/static/")


def is_low_overhead_path(path: str) -> bool:
    return (
        path == "/favicon.ico"
        or path == "/health"
        or path == "/ready"
        or path == "/internal/ready"
        or is_static_asset_request(path)
    )


def strip_cookie_vary(response) -> None:
    vary = response.headers.get("Vary")
    if not vary:
        return
    values = [part.strip() for part in vary.split(",") if part.strip()]
    values = [part for part in values if part.lower() != "cookie"]
    if values:
        response.headers["Vary"] = ", ".join(values)
    else:
        response.headers.pop("Vary", None)


def _session_cookie_limit_bytes(app_obj: Flask) -> int:
    for key in ("SESSION_COOKIE_SOFT_LIMIT_BYTES", "SESSION_COOKIE_MAX_BYTES"):
        try:
            raw = app_obj.config.get(key)
            if raw is None:
                continue
            value = int(raw)
            if value > 0:
                return value
        except Exception:
            continue
    return _SESSION_COOKIE_SOFT_LIMIT_BYTES


def _response_header_limit_bytes(app_obj: Flask) -> int:
    for key in ("RESPONSE_HEADER_SOFT_LIMIT_BYTES", "UPSTREAM_HEADER_SOFT_LIMIT_BYTES"):
        try:
            raw = app_obj.config.get(key)
            if raw is None:
                continue
            value = int(raw)
            if value > 0:
                return value
        except Exception:
            continue
    return _RESPONSE_HEADER_SOFT_LIMIT_BYTES


def _session_cookie_payload_size(app_obj: Flask, session_obj) -> int | None:
    try:
        get_serializer = getattr(app_obj.session_interface, "get_signing_serializer", None)
        if not callable(get_serializer):
            return None
        serializer = get_serializer(app_obj)
        if serializer is None:
            return None
        value = serializer.dumps(dict(session_obj))
        cookie_name = str(app_obj.config.get("SESSION_COOKIE_NAME") or "session")
        return len(f"{cookie_name}={value}".encode("utf-8"))
    except Exception as exc:
        _log_swallowed("session_cookie_payload_size", exc)
        return None


def _response_header_size(response) -> int | None:
    try:
        status = str(getattr(response, "status", "") or "")
        total = len(f"HTTP/1.1 {status}\r\n".encode("utf-8"))
        for key, value in response.headers.to_wsgi_list():
            total += len(f"{key}: {value}\r\n".encode("utf-8"))
        total += 2
        return total
    except Exception as exc:
        _log_swallowed("response_header_size", exc)
        return None


def _session_cookie_name(app_obj: Flask) -> str:
    return str(app_obj.config.get("SESSION_COOKIE_NAME") or "session")


def _remove_session_set_cookie(app_obj: Flask, response) -> int:
    try:
        cookie_name = _session_cookie_name(app_obj)
        values = response.headers.getlist("Set-Cookie")
        if not values:
            return 0
        prefix = f"{cookie_name}="
        kept = [value for value in values if not str(value).startswith(prefix)]
        removed = len(values) - len(kept)
        if removed:
            response.headers.setlist("Set-Cookie", kept)
        return removed
    except Exception as exc:
        _log_swallowed("remove_session_set_cookie", exc)
        return 0


def _trim_session_flashes(session_obj) -> tuple[bool, list[str]]:
    try:
        flashes = session_obj.get("_flashes")
    except Exception:
        return False, []
    if flashes is None:
        return False, []
    if not isinstance(flashes, (list, tuple)):
        session_obj.pop("_flashes", None)
        return True, ["_flashes"]

    cleaned = []
    changed = False
    for item in flashes:
        try:
            category, message = item
        except Exception:
            changed = True
            continue
        category_text = str(category or "message")[:40]
        message_text = str(message or "")
        if len(message_text) > _SESSION_FLASH_MESSAGE_MAX_CHARS:
            message_text = message_text[:_SESSION_FLASH_MESSAGE_MAX_CHARS] + "..."
            changed = True
        cleaned.append((category_text, message_text))
    if len(cleaned) > _SESSION_FLASH_LIMIT:
        cleaned = cleaned[-_SESSION_FLASH_LIMIT:]
        changed = True

    if changed or list(flashes) != cleaned:
        if cleaned:
            session_obj["_flashes"] = cleaned
        else:
            session_obj.pop("_flashes", None)
        return True, ["_flashes"]
    return False, []


def _drop_known_bulky_session_keys(session_obj) -> list[str]:
    dropped: list[str] = []
    try:
        keys = list(session_obj.keys())
    except Exception:
        return dropped
    for key in keys:
        key_text = str(key)
        if key_text in _SESSION_BULKY_KEYS or key_text.startswith(_SESSION_BULKY_PREFIXES):
            session_obj.pop(key, None)
            dropped.append(key_text)
    return dropped


def _drop_noncritical_session_keys(session_obj, *, include_flashes: bool) -> list[str]:
    dropped: list[str] = []
    try:
        keys = list(session_obj.keys())
    except Exception:
        return dropped
    for key in keys:
        key_text = str(key)
        if key_text in _SESSION_CRITICAL_KEYS:
            continue
        if key_text == "_flashes" and not include_flashes:
            continue
        session_obj.pop(key, None)
        dropped.append(key_text)
    return dropped


def _shrink_session_cookie_if_needed(app_obj: Flask, session_obj) -> None:
    if session_obj is None:
        return

    changed, dropped = _trim_session_flashes(session_obj)
    limit = _session_cookie_limit_bytes(app_obj)
    before_size = _session_cookie_payload_size(app_obj, session_obj)
    if before_size is None or before_size <= limit:
        if changed:
            session_obj.modified = True
        return

    dropped.extend(_drop_known_bulky_session_keys(session_obj))
    size = _session_cookie_payload_size(app_obj, session_obj)
    if size is not None and size > limit:
        dropped.extend(_drop_noncritical_session_keys(session_obj, include_flashes=False))
        size = _session_cookie_payload_size(app_obj, session_obj)
    if size is not None and size > limit and "_flashes" in session_obj:
        session_obj.pop("_flashes", None)
        dropped.append("_flashes")
        size = _session_cookie_payload_size(app_obj, session_obj)

    session_obj.modified = True
    try:
        app_obj.logger.warning(
            "session_cookie_trimmed path=%s before_bytes=%s after_bytes=%s "
            "limit_bytes=%s dropped_keys=%s",
            request_path(),
            before_size,
            size,
            limit,
            ",".join(sorted(set(dropped))) or "-",
        )
    except Exception as exc:
        _log_swallowed("session_cookie_trimmed.log", exc)


def _resave_shrunk_session_for_response_headers(
    app_obj: Flask,
    session_obj,
    response,
    original_save_session,
) -> None:
    header_limit = _response_header_limit_bytes(app_obj)
    header_size = _response_header_size(response)
    if header_size is None or header_size <= header_limit:
        return

    before_cookie_size = _session_cookie_payload_size(app_obj, session_obj)
    dropped: list[str] = []
    dropped.extend(_drop_known_bulky_session_keys(session_obj))
    dropped.extend(_drop_noncritical_session_keys(session_obj, include_flashes=True))
    if not dropped:
        try:
            app_obj.logger.warning(
                "response_header_large path=%s header_bytes=%s limit_bytes=%s "
                "session_cookie_bytes=%s",
                request_path(),
                header_size,
                header_limit,
                before_cookie_size,
            )
        except Exception as exc:
            _log_swallowed("response_header_large.log", exc)
        return

    session_obj.modified = True
    removed = _remove_session_set_cookie(app_obj, response)
    original_save_session(app_obj, session_obj, response)
    after_cookie_size = _session_cookie_payload_size(app_obj, session_obj)
    after_header_size = _response_header_size(response)
    try:
        app_obj.logger.warning(
            "response_header_session_resaved path=%s before_header_bytes=%s after_header_bytes=%s "
            "limit_bytes=%s before_cookie_bytes=%s after_cookie_bytes=%s "
            "removed_set_cookie=%s dropped_keys=%s",
            request_path(),
            header_size,
            after_header_size,
            header_limit,
            before_cookie_size,
            after_cookie_size,
            removed,
            ",".join(sorted(set(dropped))) or "-",
        )
    except Exception as exc:
        _log_swallowed("response_header_session_resaved.log", exc)


def _coerce_user_id(value) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def snapshot_request_user() -> dict:
    snapshot = {
        "is_authenticated": False,
        "id": None,
        "role": "",
        "staff_party_id": "",
        "username": "",
        "email": "",
    }
    try:
        authenticated = bool(getattr(current_user, "is_authenticated", False))
        snapshot["is_authenticated"] = authenticated
        if authenticated:
            snapshot["id"] = _coerce_user_id(getattr(current_user, "id", None))
            snapshot["role"] = str(getattr(current_user, "role", "") or "").strip()
            snapshot["staff_party_id"] = str(
                getattr(current_user, "staff_party_id", "") or ""
            ).strip()
            snapshot["username"] = str(getattr(current_user, "username", "") or "").strip()
            snapshot["email"] = str(getattr(current_user, "email", "") or "").strip()
    except Exception as exc:
        _log_swallowed("before_request.snapshot_current_user", exc)

    if snapshot["id"] is None:
        snapshot["id"] = _coerce_user_id(session.get("_user_id"))
    if snapshot["id"] is not None and not snapshot["role"]:
        try:
            from app.models.user import User

            user = db.session.get(User, int(snapshot["id"]))
            if user is not None:
                snapshot["is_authenticated"] = True
                snapshot["role"] = str(getattr(user, "role", "") or "").strip()
                snapshot["staff_party_id"] = str(getattr(user, "staff_party_id", "") or "").strip()
                snapshot["username"] = str(getattr(user, "username", "") or "").strip()
                snapshot["email"] = str(getattr(user, "email", "") or "").strip()
        except Exception as exc:
            _log_swallowed("before_request.snapshot_user_db_fallback", exc)
    try:
        g.current_user_snapshot = snapshot
    except Exception as exc:
        _log_swallowed("before_request.store_current_user_snapshot", exc)
    return snapshot


def request_user_snapshot() -> dict:
    snapshot = getattr(g, "current_user_snapshot", None)
    if isinstance(snapshot, dict):
        return snapshot
    return snapshot_request_user()


def register_request_hooks(app: Flask) -> None:
    if not getattr(app.session_interface, "_ipm_static_vary_wrapped", False):
        original_save_session = app.session_interface.save_session

        def _save_session_without_static_cookie_vary(app_obj, session_obj, response):
            _shrink_session_cookie_if_needed(app_obj, session_obj)
            original_save_session(app_obj, session_obj, response)
            _resave_shrunk_session_for_response_headers(
                app_obj,
                session_obj,
                response,
                original_save_session,
            )
            try:
                if is_static_asset_request(request_path()):
                    strip_cookie_vary(response)
            except RuntimeError:
                return None
            return None

        app.session_interface.save_session = _save_session_without_static_cookie_vary
        app.session_interface._ipm_static_vary_wrapped = True

    @app.before_request
    def _assign_request_id():
        g.request_id = uuid.uuid4().hex
        try:
            g._perf_start = time.perf_counter()
        except Exception:
            g._perf_start = None
        # Tests may hold a long-lived app context via fixtures. In that case `g` can
        # persist across request contexts and cache an anonymous `current_user`,
        # causing `login_required` to misbehave even when the session has `_user_id`.
        try:
            if current_app.config.get("TESTING") and "_login_user" in g:
                del g._login_user
        except Exception as exc:
            _log_swallowed("before_request.clear_login_user", exc)
        if not is_low_overhead_path(request_path()):
            snapshot_request_user()

    @app.before_request
    def _guard_limited_user_role_access():
        """
        Role 'user' is a login-only (pending approval) role.
        - Can access: / (landing), /auth/*, /settings/*, /help/*
        - Must not see or use service modules.
        """
        try:
            path = request_path()
            if is_low_overhead_path(path):
                return None
            user_snapshot = request_user_snapshot()
            if not user_snapshot.get("is_authenticated"):
                return None
            role = str(user_snapshot.get("role") or "").strip().lower()
            if role != "user":
                return None

            if path in {"/", "/dashboard/", "/health", "/ready", "/favicon.ico"}:
                return None
            if (
                path.startswith("/auth")
                or path.startswith("/settings")
                or path.startswith("/help")
                or path.startswith("/static")
            ):
                return None

            msg = " user . Administrator ."
            if path.startswith("/uploads") or path.startswith("/files/"):
                abort(403, msg)

            wants_json = bool(path.startswith("/api")) or bool(request.is_json)
            if wants_json or (request.method or "GET").upper() not in {"GET", "HEAD", "OPTIONS"}:
                return jsonify({"ok": False, "error": "insufficient_role", "message": msg}), 403

            return redirect(url_for("main.index"))
        except Exception as exc:
            _log_swallowed("before_request.guard_limited_user_role_access", exc)
            return None

    @app.after_request
    def _apply_static_asset_cache_headers(response):
        try:
            path = request_path()
            if not is_static_asset_request(path):
                return response
            if int(getattr(response, "status_code", 0) or 0) >= 400:
                return response

            versioned = bool((request.args.get("v") or "").strip())
            key = (
                "STATIC_ASSET_MAX_AGE_SECONDS"
                if versioned
                else "STATIC_ASSET_UNVERSIONED_MAX_AGE_SECONDS"
            )
            try:
                max_age = int(current_app.config.get(key) or 0)
            except Exception:
                max_age = 0
            if max_age > 0:
                cache_control = f"public, max-age={max_age}"
                if versioned and current_app.config.get("STATIC_ASSET_IMMUTABLE", True):
                    cache_control = f"{cache_control}, immutable"
                response.headers["Cache-Control"] = cache_control
                response.headers.pop("Pragma", None)
                strip_cookie_vary(response)
        except Exception as exc:
            _log_swallowed("after_request.static_asset_cache_headers", exc)
        return response

    # Security headers are handled by app.security in init_security(app).

    @app.after_request
    def add_request_id_header(response):
        try:
            req_id = getattr(g, "request_id", None)
            if req_id:
                response.headers["X-Request-ID"] = req_id
        except Exception as exc:
            _log_swallowed("add_request_id_header", exc)

        try:
            if current_app.config.get("PERF_HEADERS_ENABLED"):
                start = getattr(g, "_perf_start", None)
                if start:
                    dur_ms = (time.perf_counter() - float(start)) * 1000.0
                    response.headers["X-Response-Time-ms"] = f"{dur_ms:.1f}"
                    existing = (response.headers.get("Server-Timing") or "").strip()
                    val = f"app;dur={dur_ms:.1f}"
                    response.headers["Server-Timing"] = f"{existing}, {val}" if existing else val
        except Exception as exc:
            _log_swallowed("add_perf_headers", exc)
        return response

    register_user_access_logging(
        app,
        request_path=request_path,
        request_user_snapshot=request_user_snapshot,
    )

    @app.teardown_request
    def _cleanup_request_db(_exc=None):
        try:
            raw = getattr(g, "_invoice_db_raw", None)
            if raw is not None:
                try:
                    raw.close()
                except Exception as exc:
                    _log_swallowed("cleanup_request_db.raw_close", exc)
                try:
                    g._invoice_db_raw = None
                    g._invoice_db_wrapped = None
                except Exception as exc:
                    _log_swallowed("cleanup_request_db.clear_invoice_db", exc)
        except Exception as exc:
            _log_swallowed("cleanup_request_db", exc)

        try:
            db.session.rollback()
        except Exception:
            logger.warning("db.session.rollback failed", exc_info=True)
        if not app.config.get("TESTING"):
            try:
                db.session.remove()
            except Exception:
                logger.warning("db.session.remove failed", exc_info=True)
