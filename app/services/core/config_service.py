from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from flask import current_app, has_app_context
from sqlalchemy.exc import DBAPIError, InvalidRequestError, PendingRollbackError

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

_TRUE_SET = {"1", "true", "yes", "on"}
_FALSE_SET = {"0", "false", "no", "off", ""}

_SYSTEM_CONFIG_CACHE: dict[str, tuple[float, Any]] = {}
_SYSTEM_CONFIG_CACHE_LOCK = threading.Lock()
_CACHE_MISS = object()


def _truncate(value: object, max_len: int = 200) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _is_failed_transaction_error(exc: Exception) -> bool:
    # Typical PostgreSQL SQLSTATE for "current transaction is aborted"
    # - 25P02: in_failed_sql_transaction
    if isinstance(exc, PendingRollbackError):
        return True
    if isinstance(exc, InvalidRequestError):
        try:
            msg = str(exc).lower()
        except Exception:
            msg = ""
        return any(
            marker in msg
            for marker in (
                "session is in 'committed' state",
                "session is in 'prepared' state",
                "no further sql can be emitted within this transaction",
            )
        )
    if isinstance(exc, DBAPIError):
        try:
            pgcode = getattr(exc.orig, "pgcode", None)
        except Exception:
            pgcode = None
        if pgcode == "25P02":
            return True
    try:
        msg = str(exc).lower()
    except Exception:
        msg = ""
    return ("infailedsqltransaction" in msg) or ("current transaction is aborted" in msg)


def _is_connection_invalidated_error(exc: Exception) -> bool:
    if isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False)):
        return True
    try:
        msg = str(exc).lower()
    except Exception:
        msg = ""
    return any(
        marker in msg
        for marker in (
            "server closed the connection unexpectedly",
            "connection reset by peer",
            "connection refused",
            "could not connect to server",
            "terminating connection",
        )
    )


def _best_effort_session_rollback() -> bool:
    try:
        sess = getattr(db, "session", None)
    except Exception:
        sess = None
    if sess is None:
        return False
    try:
        actual_sess = sess() if callable(sess) else sess
    except Exception:
        actual_sess = sess
    try:
        # Never rollback a live unit-of-work during flush or while pending changes
        # are staged. Callers can fall back to an out-of-band read instead.
        if bool(getattr(actual_sess, "_flushing", False)):
            return False
    except Exception:
        return False
    try:
        if actual_sess.new or actual_sess.dirty or actual_sess.deleted:
            return False
    except Exception:
        return False
    try:
        get_nested_tx = getattr(actual_sess, "get_nested_transaction", None)
        if callable(get_nested_tx) and get_nested_tx() is not None:
            return False
    except Exception:
        return False
    try:
        sess.rollback()
        return True
    except Exception:
        return False


def _read_system_config_out_of_band(stmt, params: dict[str, Any], *, key: str) -> Any:
    try:
        with db.engine.connect() as conn:
            return conn.execute(stmt, params).scalar()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"config_service.system_config_out_of_band[{key}]",
            log_key=f"config_service.system_config_out_of_band.{key}",
            log_window_seconds=300,
        )
        return _CACHE_MISS


def _cache_ttl_seconds() -> int:
    """
    Small in-process cache for system_config reads.

    Goal: reduce DB load under bursty traffic (e.g., 100 concurrent users) where templates
    and request hooks read the same config keys repeatedly.
    """
    try:
        if current_app.config.get("TESTING") or (os.environ.get("TESTING") == "1"):
            return 0
    except Exception:
        return 0

    try:
        ttl = int(current_app.config.get("CONFIG_SERVICE_CACHE_TTL_SECONDS", 30) or 0)
    except Exception:
        ttl = 30
    if ttl <= 0:
        return 0
    return min(3600, ttl)


def _cache_get(key: str) -> Any:
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return _CACHE_MISS
    now = time.monotonic()
    hit = _SYSTEM_CONFIG_CACHE.get(key)
    if not hit:
        return _CACHE_MISS
    expires_at, value = hit
    if expires_at <= now:
        try:
            with _SYSTEM_CONFIG_CACHE_LOCK:
                # Remove expired entry (best-effort; may be concurrently refreshed).
                cur = _SYSTEM_CONFIG_CACHE.get(key)
                if cur and cur[0] <= now:
                    _SYSTEM_CONFIG_CACHE.pop(key, None)
        except Exception:
            return _CACHE_MISS
        return _CACHE_MISS
    return value


def _cache_set(key: str, value: Any) -> None:
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return
    now = time.monotonic()
    expires_at = now + ttl
    max_keys = 2048
    try:
        max_keys = int(current_app.config.get("CONFIG_SERVICE_CACHE_MAX_KEYS", 2048) or 2048)
    except Exception:
        max_keys = 2048
    max_keys = max(64, min(20000, max_keys))

    with _SYSTEM_CONFIG_CACHE_LOCK:
        _SYSTEM_CONFIG_CACHE[key] = (expires_at, value)
        if len(_SYSTEM_CONFIG_CACHE) <= max_keys:
            return
        # Best-effort pruning: drop expired entries first, then trim arbitrarily.
        expired = [k for k, (exp, _v) in _SYSTEM_CONFIG_CACHE.items() if exp <= now]
        for k in expired:
            _SYSTEM_CONFIG_CACHE.pop(k, None)
        while len(_SYSTEM_CONFIG_CACHE) > max_keys:
            _SYSTEM_CONFIG_CACHE.pop(next(iter(_SYSTEM_CONFIG_CACHE)), None)


class ConfigService:
    @staticmethod
    def clear_cache() -> None:
        with _SYSTEM_CONFIG_CACHE_LOCK:
            _SYSTEM_CONFIG_CACHE.clear()

    @staticmethod
    def _normalize_blank(value: Any, *, allow_blank: bool) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            if value.strip() == "" and not allow_blank:
                return None
        return value

    @staticmethod
    def _read_system_config(key: str, *, allow_blank: bool) -> Any:
        if not has_app_context():
            return None
        key = (key or "").strip()
        if not key:
            return None

        cached = _cache_get(key)
        if cached is not _CACHE_MISS:
            # Cache stores raw DB value (including None); allow_blank normalization happens here.
            return ConfigService._normalize_blank(cached, allow_blank=allow_blank)

        stmt = text("SELECT value FROM system_config WHERE key = :key")
        params = {"key": key}

        # If the request/session is in a failed transaction state (PendingRollbackError),
        # recover here to avoid cascading failures from subsequent config reads.
        try:
            sess = getattr(db, "session", None)
            if sess is not None and hasattr(sess, "is_active") and not bool(sess.is_active):
                if not _best_effort_session_rollback():
                    value = _read_system_config_out_of_band(stmt, params, key=key)
                    if value is _CACHE_MISS:
                        return None
                    _cache_set(key, value)
                    return ConfigService._normalize_blank(value, allow_blank=allow_blank)
        except Exception as exc:
            # Best-effort: never let config reads raise while trying to heal the session.
            report_swallowed_exception(
                exc,
                context="config_service.session_heal",
                log_key="config_service.session_heal",
                log_window_seconds=300,
            )
        try:
            value = db.session.execute(stmt, params).scalar()
        except Exception as exc:
            # Failed transaction / invalidated connection can leave the shared request
            # session poisoned; rollback and retry once to prevent cascaded errors.
            should_retry = _is_failed_transaction_error(exc) or _is_connection_invalidated_error(
                exc
            )
            if should_retry:
                healed = _best_effort_session_rollback()
                if healed:
                    try:
                        value = db.session.execute(stmt, params).scalar()
                    except Exception as exc2:
                        value = _read_system_config_out_of_band(stmt, params, key=key)
                        if value is _CACHE_MISS:
                            report_swallowed_exception(
                                exc2,
                                context=f"config_service.system_config[{key}]",
                                log_key=f"config_service.system_config.{key}",
                                log_window_seconds=300,
                            )
                            return None
                else:
                    value = _read_system_config_out_of_band(stmt, params, key=key)
                    if value is _CACHE_MISS:
                        report_swallowed_exception(
                            exc,
                            context=f"config_service.system_config[{key}]",
                            log_key=f"config_service.system_config.{key}",
                            log_window_seconds=300,
                        )
                        return None
            else:
                if isinstance(exc, DBAPIError):
                    _best_effort_session_rollback()
                report_swallowed_exception(
                    exc,
                    context=f"config_service.system_config[{key}]",
                    log_key=f"config_service.system_config.{key}",
                    log_window_seconds=300,
                )
                return None
        _cache_set(key, value)
        return ConfigService._normalize_blank(value, allow_blank=allow_blank)

    @staticmethod
    def _read_app_config(key: str, *, allow_blank: bool) -> Any:
        if not has_app_context():
            return None
        try:
            if key not in current_app.config:
                return None
            value = current_app.config.get(key)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="config_service.app_config",
                log_key=f"config_service.app_config.{key}",
                log_window_seconds=300,
            )
            return None
        return ConfigService._normalize_blank(value, allow_blank=allow_blank)

    @staticmethod
    def _read_env(key: str, *, allow_blank: bool) -> Any:
        if key not in os.environ:
            return None
        return ConfigService._normalize_blank(os.environ.get(key), allow_blank=allow_blank)

    @staticmethod
    def get_raw(
        key: str,
        default: Any = None,
        *,
        allow_blank: bool = False,
        prefer_env: bool = False,
    ) -> Any:
        readers = (
            (
                ConfigService._read_env,
                ConfigService._read_system_config,
                ConfigService._read_app_config,
            )
            if prefer_env
            else (
                ConfigService._read_system_config,
                ConfigService._read_app_config,
                ConfigService._read_env,
            )
        )
        for reader in readers:
            value = reader(key, allow_blank=allow_blank)
            if value is not None or (allow_blank and value == ""):
                return value
        return default

    @staticmethod
    def get_str(
        key: str,
        default: str | None = None,
        *,
        strip: bool = True,
        allow_blank: bool = True,
        prefer_env: bool = False,
    ) -> str | None:
        raw = ConfigService.get_raw(key, default, allow_blank=allow_blank, prefer_env=prefer_env)
        if raw is None:
            return default
        if isinstance(raw, str):
            value = raw.strip() if strip else raw
        else:
            value = str(raw).strip() if strip else str(raw)
        if not value and not allow_blank:
            return default
        return value

    @staticmethod
    def _log_parse_error(key: str, raw: Any, message: str, exc: Exception | None = None) -> None:
        detail = f"{message} (raw={_truncate(raw)})"
        err = exc or ValueError(f"{key}: {detail}")
        report_swallowed_exception(
            err,
            context="config_service.parse",
            log_key=f"config_service.parse.{key}",
            log_window_seconds=300,
        )

    @staticmethod
    def get_bool(key: str, default: bool = False, *, prefer_env: bool = False) -> bool:
        # Treat blank strings as a valid "false" value (common in envs/DB toggles).
        raw = ConfigService.get_raw(key, default, allow_blank=True, prefer_env=prefer_env)
        if raw is None:
            return bool(default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        try:
            value = str(raw).strip().lower()
        except Exception as exc:
            ConfigService._log_parse_error(key, raw, "invalid bool", exc)
            return bool(default)
        if value in _TRUE_SET:
            return True
        if value in _FALSE_SET:
            return False
        ConfigService._log_parse_error(key, raw, "invalid bool")
        return bool(default)

    @staticmethod
    def get_int(
        key: str,
        default: int | None = None,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        prefer_env: bool = False,
    ) -> int | None:
        raw = ConfigService.get_raw(key, default, allow_blank=False, prefer_env=prefer_env)
        if raw is None or raw == "":
            return default
        try:
            value = int(str(raw).strip())
        except Exception as exc:
            ConfigService._log_parse_error(key, raw, "invalid int", exc)
            return default
        if min_value is not None and value < min_value:
            ConfigService._log_parse_error(key, raw, f"int below min {min_value}")
            return default
        if max_value is not None and value > max_value:
            ConfigService._log_parse_error(key, raw, f"int above max {max_value}")
            return default
        return value

    @staticmethod
    def _coerce_json_default(default: Any) -> Any:
        if isinstance(default, str):
            try:
                return json.loads(default)
            except Exception:
                return default
        return default

    @staticmethod
    def _parse_json_value(raw: Any, default_value: Any, key: str) -> Any:
        if raw is None:
            return default_value
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, str):
            if raw.strip() == "":
                return default_value
            try:
                return json.loads(raw)
            except Exception as exc:
                ConfigService._log_parse_error(key, raw, "invalid json", exc)
                return default_value
        return raw

    @staticmethod
    def get_json(key: str, default: Any = None, *, prefer_env: bool = False) -> Any:
        default_value = ConfigService._coerce_json_default(default)
        raw = ConfigService.get_raw(key, default, allow_blank=False, prefer_env=prefer_env)
        return ConfigService._parse_json_value(raw, default_value, key)

    @staticmethod
    def get_json_schema(
        key: str,
        default: Any,
        *,
        schema: dict | Callable[[Any], bool] | None = None,
        schema_name: str | None = None,
        prefer_env: bool = False,
    ) -> Any:
        default_value = ConfigService._coerce_json_default(default)
        value = ConfigService.get_json(key, default_value, prefer_env=prefer_env)
        if schema is None:
            return value

        if callable(schema):
            try:
                ok = bool(schema(value))
            except Exception as exc:
                ConfigService._log_parse_error(
                    key,
                    value,
                    f"schema {schema_name or 'callable'} error",
                    exc,
                )
                return default_value
            if not ok:
                ConfigService._log_parse_error(
                    key,
                    value,
                    f"schema {schema_name or 'callable'} mismatch",
                )
                return default_value
            return value

        if isinstance(schema, dict):
            try:
                import jsonschema  # type: ignore

                jsonschema.validate(value, schema)
                return value
            except ImportError as exc:
                ConfigService._log_parse_error(
                    key,
                    value,
                    f"schema {schema_name or 'jsonschema'} missing dependency",
                    exc,
                )
                return value
            except Exception as exc:
                ConfigService._log_parse_error(
                    key,
                    value,
                    f"schema {schema_name or 'jsonschema'} mismatch",
                    exc,
                )
                return default_value

        return value
