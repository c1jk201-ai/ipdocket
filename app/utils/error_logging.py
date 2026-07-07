from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback
from datetime import datetime

from flask import current_app, g, has_app_context, has_request_context, request
from flask_login import current_user
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException

from app.extensions import db
from app.models.error_report import ErrorReport

_LOG_THROTTLE: dict[str, float] = {}
logger = logging.getLogger(__name__)


def _safe_debug(message: str, *args) -> None:
    try:
        logger.debug(message, *args, exc_info=True)
    except Exception:
        return


def _truncate(value: str, max_len: int):
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _resolve_request_id(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    try:
        return getattr(g, "request_id", None)
    except Exception:
        return None


def _coerce_context_id(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _pick_context_id(keys: list[str], sources: list[object]) -> str | None:
    for src in sources:
        if src is None:
            continue
        try:
            getter = src.get  # type: ignore[attr-defined]
        except Exception:
            getter = None
        if getter:
            for key in keys:
                try:
                    value = getter(key)
                except Exception:
                    value = None
                val = _coerce_context_id(value)
                if val:
                    return val
    return None


def _extract_context_ids() -> tuple[str | None, str | None, str | None]:
    if not has_request_context():
        return None, None, None

    matter_keys = ["matter_id", "case_id", "matterId", "caseId", "mid"]
    invoice_keys = ["invoice_id", "invoiceId", "inv_id", "external_invoice_ref"]
    workflow_keys = ["workflow_id", "workflowId", "wf_id", "work_id"]

    matter_id = _coerce_context_id(getattr(g, "matter_id", None)) or _coerce_context_id(
        getattr(g, "case_id", None)
    )
    invoice_id = _coerce_context_id(getattr(g, "invoice_id", None))
    workflow_id = _coerce_context_id(getattr(g, "workflow_id", None))

    sources: list[object] = []
    try:
        sources.append(request.view_args or {})
    except Exception:
        sources.append({})
    try:
        sources.append(request.args or {})
    except Exception:
        sources.append({})
    try:
        form = request.form
    except Exception:
        form = None
    if form:
        sources.append(form)
    try:
        is_json = bool(request.is_json)
    except Exception:
        is_json = False
    if is_json:
        try:
            sources.append(request.get_json(silent=True) or {})
        except Exception:
            sources.append({})

    if not matter_id:
        matter_id = _pick_context_id(matter_keys, sources)
    if not invoice_id:
        invoice_id = _pick_context_id(invoice_keys, sources)
    if not workflow_id:
        workflow_id = _pick_context_id(workflow_keys, sources)

    return matter_id, invoice_id, workflow_id


def _should_log(key: str, window_seconds: int) -> bool:
    if not key or window_seconds <= 0:
        return True
    now = time.monotonic()
    last = _LOG_THROTTLE.get(key)
    if last is not None and (now - last) < window_seconds:
        return False
    _LOG_THROTTLE[key] = now
    return True


def _create_error_report_session():
    try:
        engine = db.get_engine()
    except Exception:
        try:
            engine = db.engine
        except Exception:
            return None
    try:
        return sessionmaker(bind=engine)()
    except Exception:
        return None


def _capture_dedupe_key(
    *,
    error_type: str,
    status_code: int,
    method: str | None,
    path: str | None,
    endpoint: str | None,
    message: str,
    request_id: str | None,
) -> str:
    raw = "|".join(
        [
            str(error_type or ""),
            str(status_code or 0),
            str(method or ""),
            str(path or ""),
            str(endpoint or ""),
            str(request_id or ""),
            str(message or "")[:300],
        ]
    )
    digest = hashlib.sha1(raw.encode("utf-8", "ignore"), usedforsecurity=False).hexdigest()
    return f"capture:{digest}"


def capture_exception(
    exc: Exception,
    *,
    context: str | None = None,
    request_id: str | None = None,
) -> None:
    if not has_app_context():
        return
    in_request = has_request_context()
    if not in_request and not context:
        return

    try:
        enabled = current_app.config.get("ERROR_REPORTING_ENABLED", True)
    except Exception:
        enabled = True
    if not enabled:
        return

    try:
        if isinstance(exc, HTTPException):
            code = getattr(exc, "code", None)
            if code is not None and int(code) < 500:
                return
            status_code = int(code or 500)
        else:
            status_code = 500
    except Exception:
        status_code = 500

    try:
        user_id = None
        try:
            if in_request:
                snapshot = getattr(g, "current_user_snapshot", None)
                if isinstance(snapshot, dict) and snapshot.get("id") not in (None, ""):
                    user_id = int(snapshot["id"])
                elif current_user and current_user.is_authenticated:
                    user_id = int(current_user.get_id())
        except Exception:
            user_id = None

        max_text = int(current_app.config.get("ERROR_REPORTING_MAX_TEXT", 20000) or 20000)

        query_string = None
        if in_request:
            try:
                if request.query_string:
                    query_string = request.query_string.decode("utf-8", "replace")
            except Exception:
                query_string = None

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        req_id_value = _resolve_request_id(request_id)
        message = str(exc)
        if context:
            message = f"{context}: {message}"

        matter_id, invoice_id, workflow_id = _extract_context_ids()

        if in_request:
            method = request.method
            path = request.path
            endpoint = _truncate(request.endpoint, 255) if request.endpoint else None
            blueprint = _truncate(request.blueprint, 255) if request.blueprint else None
            remote_addr = _truncate(
                request.headers.get("X-Forwarded-For", request.remote_addr), 255
            )
            user_agent = _truncate(request.headers.get("User-Agent"), 512)
        else:
            method = "SYSTEM"
            path = context or "background"
            endpoint = _truncate(context, 255) if context else None
            blueprint = None
            remote_addr = None
            user_agent = None

        dedupe_window = int(current_app.config.get("ERROR_REPORTING_DEDUP_WINDOW_SECONDS", 2) or 2)
        dedupe_key = _capture_dedupe_key(
            error_type=type(exc).__name__,
            status_code=status_code,
            method=method,
            path=path,
            endpoint=endpoint,
            message=message,
            request_id=req_id_value,
        )
        if dedupe_window > 0 and not _should_log(dedupe_key, dedupe_window):
            return

        report = ErrorReport(
            created_at=datetime.utcnow(),
            user_id=user_id,
            method=method,
            path=path,
            query_string=_truncate(query_string, max_text),
            endpoint=endpoint,
            blueprint=blueprint,
            remote_addr=remote_addr,
            user_agent=user_agent,
            status_code=status_code,
            request_id=_truncate(req_id_value, 255),
            matter_id=_truncate(matter_id, 255),
            invoice_id=_truncate(invoice_id, 255),
            workflow_id=_truncate(workflow_id, 255),
            error_type=_truncate(type(exc).__name__, 255),
            message=_truncate(message, max_text),
            traceback=_truncate(tb, max_text),
        )
        report_session = _create_error_report_session()
        if report_session is None:
            return
        try:
            report_session.add(report)
            report_session.commit()
        except Exception:
            try:
                report_session.rollback()
            except Exception as rollback_exc:
                _safe_debug("ErrorReport rollback failed: %s", rollback_exc)
        finally:
            report_session.close()
    except Exception as exc:
        _safe_debug("capture_exception failed: %s", exc)


def report_swallowed_exception(
    exc: Exception,
    *,
    context: str,
    request_id: str | None = None,
    log_key: str | None = None,
    log_window_seconds: int = 300,
) -> None:
    req_id_value = _resolve_request_id(request_id)
    key = log_key or f"{context}:{type(exc).__name__}"
    try:
        should_log = _should_log(key, log_window_seconds)
    except Exception as throttle_exc:
        _safe_debug("report_swallowed_exception throttle failed: %s", throttle_exc)
        should_log = True

    if should_log:
        try:
            if has_app_context():
                current_app.logger.warning(
                    "Swallowed exception in %s (request_id=%s): %s",
                    context,
                    req_id_value or "-",
                    exc,
                )
            else:
                logger.warning(
                    "Swallowed exception in %s (request_id=%s): %s",
                    context,
                    req_id_value or "-",
                    exc,
                    exc_info=True,
                )
        except Exception as log_exc:
            _safe_debug("report_swallowed_exception logger failed: %s", log_exc)
        try:
            # Throttle DB error report writes the same way we throttle logs.
            capture_exception(exc, context=f"swallowed:{context}", request_id=req_id_value)
        except Exception as capture_exc:
            _safe_debug("report_swallowed_exception capture failed: %s", capture_exc)


class _ErrorReportCaptureHandler(logging.Handler):
    """Capture ERROR logs with exception info into ErrorReport."""

    _emit_guard = threading.local()

    def __init__(self, app):
        super().__init__(level=logging.ERROR)
        self._app = app

    def bind_app(self, app) -> None:
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        if int(getattr(record, "levelno", 0) or 0) < logging.ERROR:
            return

        exc_info = getattr(record, "exc_info", None)
        if not exc_info or len(exc_info) < 2:
            return
        exc = exc_info[1]
        if not isinstance(exc, Exception):
            return

        if getattr(self._emit_guard, "active", False):
            return

        logger_name = str(getattr(record, "name", "") or "").strip()
        if logger_name == __name__:
            return

        context = f"logged:{logger_name or 'unknown'}"
        request_id = None
        try:
            rid = getattr(record, "request_id", None)
            if rid:
                request_id = str(rid)
        except Exception:
            request_id = None

        app = self._app
        if app is None:
            return

        self._emit_guard.active = True
        try:
            if has_app_context():
                capture_exception(exc, context=context, request_id=request_id)
            else:
                with app.app_context():
                    capture_exception(exc, context=context, request_id=request_id)
        except Exception as hook_exc:
            _safe_debug("error report log handler emit failed: %s", hook_exc)
        finally:
            self._emit_guard.active = False


def install_error_report_logging_hook(app) -> None:
    """
    Install a log handler that captures logged exceptions into ErrorReport.
    This complements request exception hooks and catches handled exceptions
    that are only emitted via logger.exception(...).
    """
    try:
        enabled = bool(app.config.get("ERROR_REPORT_CAPTURE_LOGGED_EXCEPTIONS", True))
    except Exception:
        enabled = True
    if not enabled:
        return

    root_logger = logging.getLogger()
    handler = None
    for existing in root_logger.handlers:
        if isinstance(existing, _ErrorReportCaptureHandler):
            handler = existing
            break

    if handler is None:
        handler = _ErrorReportCaptureHandler(app)
        root_logger.addHandler(handler)
    else:
        handler.bind_app(app)

    if not any(isinstance(h, _ErrorReportCaptureHandler) for h in app.logger.handlers):
        app.logger.addHandler(handler)

    try:
        ext = app.extensions.setdefault("error_reporting", {})
        ext["log_capture_handler"] = handler
    except Exception:
        return
