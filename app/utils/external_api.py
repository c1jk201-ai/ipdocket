from __future__ import annotations

import errno
import logging
import random
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

from flask import current_app, has_app_context

T = TypeVar("T")
logger = logging.getLogger(__name__)


class CircuitBreakerOpen(RuntimeError):
    pass


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0
    last_alert_at: float = 0.0


_LOCK = threading.Lock()
_STATE: dict[str, _CircuitState] = {}
_RETRYABLE_OS_ERRNOS = {
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
}


def _now() -> float:
    return time.monotonic()


def _log_debug(context: str, exc: Exception) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Swallowed exception in %s: %s", context, exc, exc_info=True)


def _cfg_int(key: str, default: int) -> int:
    try:
        if has_app_context():
            return int(current_app.config.get(key, default) or default)
    except Exception as exc:
        _log_debug("external_api._cfg_int", exc)
    return int(default)


def _cfg_float(key: str, default: float) -> float:
    try:
        if has_app_context():
            return float(current_app.config.get(key, default) or default)
    except Exception as exc:
        _log_debug("external_api._cfg_float", exc)
    return float(default)


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        if has_app_context():
            v = current_app.config.get(key, default)
            if isinstance(v, bool):
                return v
            if v is None:
                return bool(default)
            if isinstance(v, (int, float)):
                return bool(v)
            s = str(v).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            return bool(default)
    except Exception as exc:
        _log_debug("external_api._cfg_bool", exc)
    return bool(default)


def _status_code_from_exc(exc: Exception) -> int | None:
    # googleapiclient
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            return getattr(exc.resp, "status", None)
    except Exception as exc_info:
        _log_debug("external_api._status_code_from_exc:googleapiclient", exc_info)

    # requests
    try:
        import requests

        if isinstance(exc, requests.exceptions.HTTPError):
            if exc.response is not None:
                return int(exc.response.status_code)
    except Exception as exc_info:
        _log_debug("external_api._status_code_from_exc:requests", exc_info)

    return None


def _header_value(response: object | None, name: str) -> str | None:
    if response is None:
        return None
    candidates = (name, name.lower())
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            for candidate in candidates:
                value = headers.get(candidate)
                if value is not None:
                    return str(value)
        except Exception as exc:
            _log_debug("external_api._header_value.headers", exc)
    try:
        for candidate in candidates:
            value = response.get(candidate)  # type: ignore[attr-defined]
            if value is not None:
                return str(value)
    except Exception as exc:
        _log_debug("external_api._header_value.mapping", exc)
    return None


def _retry_after_seconds_from_value(raw_value: str | None) -> float | None:
    raw = (raw_value or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        return seconds if seconds > 0 else None
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
        return seconds if seconds > 0 else None
    except Exception as exc:
        _log_debug("external_api._retry_after_seconds_from_value", exc)
        return None


def _retry_after_seconds_from_exc(exc: Exception) -> float | None:
    try:
        response = getattr(exc, "response", None)
        value = _header_value(response, "Retry-After")
        if value:
            return _retry_after_seconds_from_value(value)
    except Exception as exc_info:
        _log_debug("external_api._retry_after_seconds_from_exc:response", exc_info)

    try:
        resp = getattr(exc, "resp", None)
        value = _header_value(resp, "Retry-After")
        if value:
            return _retry_after_seconds_from_value(value)
    except Exception as exc_info:
        _log_debug("external_api._retry_after_seconds_from_exc:resp", exc_info)

    return None


def _is_retryable(status: int | None, exc: Exception) -> bool:
    if status in (408, 425, 429):
        return True
    if status is not None and 500 <= int(status) < 600:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _RETRYABLE_OS_ERRNOS:
        return True
    if isinstance(exc, ssl.SSLError):
        msg = str(exc).lower()
        if any(marker in msg for marker in ("timed out", "timeout", "connection reset")):
            return True
    try:
        import requests

        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
    except Exception as exc_info:
        _log_debug("external_api._is_retryable:requests", exc_info)
    return False


def _report_failure(context: str, exc: Exception) -> None:
    try:
        from app.utils.error_logging import report_swallowed_exception

        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=120,
        )
    except Exception as exc_info:
        _log_debug("external_api._report_failure", exc_info)


def is_circuit_open(key: str, *, include_prefix: bool = False) -> bool:
    now = _now()
    with _LOCK:
        st = _STATE.get(key)
        if st and st.open_until and now < st.open_until:
            return True
        if include_prefix:
            prefix = f"{key}:"
            for state_key, st in _STATE.items():
                if not state_key.startswith(prefix):
                    continue
                if st.open_until and now < st.open_until:
                    return True
    return False


def external_api_call(
    service: str,
    operation: str,
    fn: Callable[[], T],
    *,
    breaker_key: str | None = None,
    ignore_statuses: set[int] | None = None,
) -> T:
    """
    External API wrapper:
    - retry w/ backoff for transient failures (429/5xx/network)
    - circuit breaker to avoid cascading failures
    - best-effort failure recording + optional alert on breaker-open
    """
    key = breaker_key or f"{service}:{operation}"
    attempts = max(1, _cfg_int("EXTERNAL_API_MAX_ATTEMPTS", 3))
    base_delay = max(0.0, _cfg_float("EXTERNAL_API_RETRY_BASE_DELAY_SECONDS", 0.5))
    max_delay = max(base_delay, _cfg_float("EXTERNAL_API_RETRY_MAX_DELAY_SECONDS", 8.0))
    fail_threshold = max(1, _cfg_int("EXTERNAL_API_CB_FAIL_THRESHOLD", 5))
    open_seconds = max(5, _cfg_int("EXTERNAL_API_CB_OPEN_SECONDS", 300))
    alerts_enabled = _cfg_bool("EXTERNAL_API_ALERTS_ENABLED", False)

    now = _now()
    with _LOCK:
        st = _STATE.get(key)
        if st is None:
            st = _CircuitState()
            _STATE[key] = st
        if st.open_until and now < st.open_until:
            raise CircuitBreakerOpen(f"circuit open for {key} (until {st.open_until:.0f})")

    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            out = fn()
            with _LOCK:
                st = _STATE.get(key)
                if st:
                    st.failures = 0
                    st.open_until = 0.0
            return out
        except Exception as exc:
            last_exc = exc
            status = _status_code_from_exc(exc)

            # caller-declared "expected errors" (e.g., stale link 404/410)
            if ignore_statuses and status in ignore_statuses:
                raise

            should_retry = (i < attempts) and _is_retryable(status, exc)
            if should_retry:
                retry_after = _retry_after_seconds_from_exc(exc)
                if retry_after is not None:
                    delay = min(max_delay, retry_after)
                else:
                    delay = min(max_delay, base_delay * (2 ** (i - 1)))
                    delay = delay * (1.0 + random.uniform(-0.15, 0.15))
                try:
                    time.sleep(max(0.0, delay))
                except Exception as exc:
                    _log_debug("external_api.sleep", exc)
                continue

            opened = False
            with _LOCK:
                st = _STATE.get(key)
                if st is None:
                    st = _CircuitState()
                    _STATE[key] = st
                st.failures += 1
                if st.failures >= fail_threshold:
                    st.failures = 0
                    st.open_until = _now() + float(open_seconds)
                    # throttle alerts while open
                    if (_now() - st.last_alert_at) >= float(open_seconds):
                        st.last_alert_at = _now()
                        opened = True

            ctx = f"external_api:{key}"
            _report_failure(ctx, exc)
            raise

    raise last_exc or RuntimeError(f"external_api_call failed: {key}")
