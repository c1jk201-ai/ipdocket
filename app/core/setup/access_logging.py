from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from flask import Flask, current_app, g, request, session
from sqlalchemy.orm import sessionmaker

from app.core.setup.logging_setup import _log_swallowed
from app.extensions import db
from app.utils.policy_sql import policy_text as text


def _write_user_access_log_row(payload: dict) -> None:
    stmt = text(
        """
        INSERT INTO user_access_log
            (created_at, user_id, request_id, method, path, endpoint, blueprint,
             status_code, duration_ms, remote_addr, user_agent, referer)
        VALUES
            (:created_at, :user_id, :request_id, :method, :path, :endpoint, :blueprint,
             :status_code, :duration_ms, :remote_addr, :user_agent, :referer)
        """
    )
    bind = db.session.get_bind() or db.engine
    session_factory = sessionmaker(bind=bind)
    write_session = session_factory()
    try:
        write_session.execute(stmt, payload)
        write_session.commit()
    except Exception:
        write_session.rollback()
        raise
    finally:
        write_session.close()


def register_user_access_logging(
    app: Flask,
    *,
    request_path: Callable[[], str],
    request_user_snapshot: Callable[[], dict],
) -> None:
    @app.after_request
    def _log_user_access(response):
        """
        Best-effort user activity logging for admins/ops.

        Defaults:
        - Log all state-changing requests (POST/PUT/PATCH/DELETE)
        - Log HTML page views (GET/HEAD where response mimetype is text/html)
        - Skip static/health endpoints

        Never blocks the response on logging failures.
        """
        try:
            if not current_app.config.get("USER_ACCESS_LOG_ENABLED", True):
                return response

            path = request_path()
            default_excludes = (
                "/static",
                "/health",
                "/ready",
                "/favicon.ico",
                "/uploads",
                "/files/",
            )
            raw_excludes = (
                current_app.config.get("USER_ACCESS_LOG_EXCLUDE_PREFIXES") or ""
            ).strip()
            extra_excludes = tuple(
                p.strip() for p in raw_excludes.split(",") if isinstance(p, str) and p.strip()
            )
            excludes = default_excludes + extra_excludes
            if any(path.startswith(prefix) for prefix in excludes if prefix):
                return response

            user_snapshot = request_user_snapshot()
            user_id = user_snapshot.get("id") if user_snapshot.get("is_authenticated") else None
            if user_id is None:
                session_user_id = session.get("_user_id")
                if session_user_id not in (None, ""):
                    try:
                        user_id = int(session_user_id)
                    except (TypeError, ValueError):
                        user_id = None
            if user_id is None:
                return response

            method = (request.method or "GET").strip().upper()
            if method == "OPTIONS":
                return response

            if method in {"GET", "HEAD"}:
                mimetype = (getattr(response, "mimetype", None) or "").lower()
                if not mimetype.startswith("text/html") and not current_app.config.get(
                    "USER_ACCESS_LOG_INCLUDE_API_GET", False
                ):
                    return response
            else:
                raw_methods = (
                    current_app.config.get("USER_ACCESS_LOG_METHODS") or "POST,PUT,PATCH,DELETE"
                )
                allowed = {
                    m.strip().upper()
                    for m in str(raw_methods).split(",")
                    if isinstance(m, str) and m.strip()
                }
                if allowed and method not in allowed:
                    return response

            status_code = getattr(response, "status_code", None)

            dur_ms = None
            try:
                start = getattr(g, "_perf_start", None)
                if start:
                    dur_ms = int((time.perf_counter() - float(start)) * 1000.0)
            except Exception:
                dur_ms = None

            remote_addr = None
            try:
                remote_addr = request.remote_addr
            except Exception:
                remote_addr = None

            user_agent = (request.headers.get("User-Agent") or "")[:512]
            referer = (request.headers.get("Referer") or "")[:512]

            endpoint = getattr(request, "endpoint", None)
            blueprint = getattr(request, "blueprint", None)
            request_id = getattr(g, "request_id", None)

            payload = {
                "created_at": datetime.utcnow(),
                "user_id": int(user_id),
                "request_id": str(request_id) if request_id else None,
                "method": method,
                "path": path,
                "endpoint": str(endpoint) if endpoint else None,
                "blueprint": str(blueprint) if blueprint else None,
                "status_code": int(status_code) if status_code is not None else None,
                "duration_ms": int(dur_ms) if dur_ms is not None else None,
                "remote_addr": str(remote_addr) if remote_addr else None,
                "user_agent": str(user_agent) if user_agent else None,
                "referer": str(referer) if referer else None,
            }

            async_enabled = bool(current_app.config.get("USER_ACCESS_LOG_ASYNC_ENABLED", True))
            if current_app.config.get("TESTING", False):
                async_enabled = bool(
                    current_app.config.get("USER_ACCESS_LOG_ASYNC_ENABLED_IN_TEST", False)
                )
            if async_enabled:
                try:
                    from app.services.ops.background import BackgroundService

                    BackgroundService.run_async(
                        _write_user_access_log_row,
                        payload,
                        _context="after_request.user_access_log.insert",
                    )
                except Exception as exc:
                    _log_swallowed("after_request.user_access_log.async", exc)
                    _write_user_access_log_row(payload)
            else:
                _write_user_access_log_row(payload)
        except Exception as exc:
            _log_swallowed("after_request.user_access_log", exc)
        return response
