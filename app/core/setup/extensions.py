from __future__ import annotations

import os
import re

from flask import Flask
from sqlalchemy.pool import StaticPool

from app.core.setup.logging_setup import _log_swallowed
from app.extensions import csrf, db, limiter, login_manager


def configure_db_engine(app: Flask) -> None:
    uri = (app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip().lower()
    opts = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})

    if uri.startswith("sqlite"):
        # In-memory SQLite creates a separate database per connection. For tests we want a single
        # shared connection across app/request contexts.
        is_testing = bool(app.config.get("TESTING")) or (os.environ.get("TESTING") == "1")
        is_memory = ":memory:" in uri or "mode=memory" in uri
        if is_testing and is_memory:
            connect_args = dict(opts.get("connect_args") or {})
            connect_args.setdefault("check_same_thread", False)
            opts["connect_args"] = connect_args
            opts.setdefault("poolclass", StaticPool)

        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = opts
        return

    if uri:
        pool_pre_ping = bool(app.config.get("DB_POOL_PRE_PING", True))
        opts.setdefault("pool_pre_ping", pool_pre_ping)
        # Better behavior under bursty traffic: reuse recently-returned connections first.
        # (Safe default; can be overridden via SQLALCHEMY_ENGINE_OPTIONS.)
        opts.setdefault("pool_use_lifo", True)

        pool_size = app.config.get("DB_POOL_SIZE")
        try:
            pool_size = int(pool_size)
        except Exception:
            pool_size = None
        if pool_size and pool_size > 0:
            opts.setdefault("pool_size", pool_size)

        max_overflow = app.config.get("DB_MAX_OVERFLOW")
        try:
            max_overflow = int(max_overflow)
        except Exception:
            max_overflow = None
        if max_overflow is not None and max_overflow >= 0:
            opts.setdefault("max_overflow", max_overflow)

        pool_timeout = app.config.get("DB_POOL_TIMEOUT")
        try:
            pool_timeout = int(pool_timeout)
        except Exception:
            pool_timeout = None
        if pool_timeout and pool_timeout > 0:
            opts.setdefault("pool_timeout", pool_timeout)

        pool_recycle = app.config.get("DB_POOL_RECYCLE_SECONDS")
        try:
            pool_recycle = int(pool_recycle)
        except Exception:
            pool_recycle = None
        if pool_recycle and pool_recycle > 0:
            opts.setdefault("pool_recycle", pool_recycle)

        statement_timeout_ms = app.config.get("DB_STATEMENT_TIMEOUT_MS")
        try:
            statement_timeout_ms = int(statement_timeout_ms)
        except Exception:
            statement_timeout_ms = None

        lock_timeout_ms = app.config.get("DB_LOCK_TIMEOUT_MS")
        try:
            lock_timeout_ms = int(lock_timeout_ms)
        except Exception:
            lock_timeout_ms = None

        idle_in_tx_timeout_ms = app.config.get("DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS")
        try:
            idle_in_tx_timeout_ms = int(idle_in_tx_timeout_ms)
        except Exception:
            idle_in_tx_timeout_ms = None

        env = (
            (
                os.environ.get("FLASK_ENV")
                or os.environ.get("ENV")
                or os.environ.get("APP_ENV")
                or ""
            )
            .strip()
            .lower()
        )
        cfg_name = (app.config.get("CONFIG_NAME") or "").strip().lower()
        is_prod = (env in {"prod", "production"}) or (cfg_name in {"prod", "production"})

        if is_prod:
            if statement_timeout_ms is None:
                statement_timeout_ms = 60000
            if lock_timeout_ms is None:
                lock_timeout_ms = 10000
            if idle_in_tx_timeout_ms is None:
                idle_in_tx_timeout_ms = 60000
        else:
            if statement_timeout_ms is None:
                statement_timeout_ms = 0
            if lock_timeout_ms is None:
                lock_timeout_ms = 0
            if idle_in_tx_timeout_ms is None:
                idle_in_tx_timeout_ms = 0

        def _set_pg_guc(options: str, key: str, value: int) -> str:
            """
            Ensure Postgres connection option `-c key=value` is present exactly once.
            Uses a simple regex replace to avoid duplicated flags across reloads.
            """
            try:
                v = int(value or 0)
            except Exception:
                v = 0
            if v <= 0:
                return (options or "").strip()
            options = (options or "").strip()
            try:
                pattern = re.compile(r"(?:^|\s)-c\s+%s=[^\s]+" % re.escape(key))
                options = pattern.sub("", options).strip()
            except Exception:
                # Best-effort: if regex fails for any reason, keep original options.
                options = (options or "").strip()
            flag = f"-c {key}={v}"
            return f"{options} {flag}".strip() if options else flag

        is_postgres = uri.startswith("postgresql") or uri.startswith("postgres")
        if is_postgres:
            connect_args = dict(opts.get("connect_args") or {})
            options = str(connect_args.get("options") or "").strip()
            options = _set_pg_guc(options, "statement_timeout", statement_timeout_ms)
            options = _set_pg_guc(options, "lock_timeout", lock_timeout_ms)
            options = _set_pg_guc(
                options, "idle_in_transaction_session_timeout", idle_in_tx_timeout_ms
            )
            if options:
                connect_args["options"] = options
                opts["connect_args"] = connect_args

    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = opts


def validate_upload_limits(app: Flask) -> None:
    try:
        req_limit = app.config.get("MAX_CONTENT_LENGTH")
        file_limit = app.config.get("FILE_ASSET_MAX_BYTES")
        if req_limit and file_limit and int(req_limit) != int(file_limit):
            app.logger.warning(
                "Upload limits mismatch: MAX_CONTENT_LENGTH=%s, FILE_ASSET_MAX_BYTES=%s",
                req_limit,
                file_limit,
            )
    except Exception as exc:
        _log_swallowed("validate_upload_limits", exc)


def init_extensions(app: Flask) -> None:
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    storage_uri = (app.config.get("RATELIMIT_STORAGE_URI") or "").strip().lower()
    is_memory_storage = (not storage_uri) or storage_uri.startswith("memory://")
    if is_memory_storage and not app.debug:
        msg = (
            "Rate limiting is using in-memory storage in a non-debug environment. "
            "Set RATELIMIT_STORAGE_URI to a shared backend (e.g., Redis or Memcached) for production."
        )
        if app.config.get("RATELIMIT_REQUIRE_SHARED_STORAGE", False):
            raise RuntimeError(msg)
        app.logger.warning(msg)

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, int(user_id))
        except Exception:
            return None
