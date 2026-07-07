from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from flask import current_app, has_app_context

from app.utils.error_logging import report_swallowed_exception


class UploadTooLarge(Exception):
    """Raised when an upload exceeds the configured size limit."""


def _read_positive_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    if parsed > 0:
        return parsed
    return None


def resolve_first_positive_int(
    keys: Iterable[str],
    *,
    default: int = 0,
    env_fallback: bool = False,
) -> int:
    """
    Resolve the first positive int from Flask config (and optionally env) using the given key order.
    Returns `default` if none found.
    """
    for key in keys:
        raw = None
        if has_app_context():
            try:
                raw = current_app.config.get(key)
            except Exception:
                raw = None
        if (raw is None or raw == "") and env_fallback:
            try:
                raw = os.environ.get(key)
            except Exception:
                raw = None
        parsed = _read_positive_int(raw)
        if parsed is not None:
            return parsed
    return int(default or 0)


def resolve_max_positive_int(
    keys: Iterable[str],
    *,
    default: int = 0,
    env_fallback: bool = False,
) -> int:
    """
    Resolve the maximum positive int from Flask config (and optionally env) across the given keys.
    Returns `default` if none found.
    """
    best = None
    for key in keys:
        raw = None
        if has_app_context():
            try:
                raw = current_app.config.get(key)
            except Exception:
                raw = None
        if (raw is None or raw == "") and env_fallback:
            try:
                raw = os.environ.get(key)
            except Exception:
                raw = None
        parsed = _read_positive_int(raw)
        if parsed is None:
            continue
        best = parsed if best is None else max(best, parsed)
    return int(best or default or 0)


def save_upload_stream(
    file_obj,
    dst: str | Path,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
    too_large_exc: type[Exception] = UploadTooLarge,
    too_large_message: str = "File  .",
    context_prefix: str | None = None,
    report_seek_errors: bool = True,
    report_cleanup_errors: bool = True,
    cleanup_context_suffix: str = "cleanup_remove",
    seek_context_suffix: str = "seek_start",
    log_window_seconds: int = 300,
) -> int:
    """
    Stream an upload to `dst` while enforcing `max_bytes`.

    - `file_obj` can be a Werkzeug FileStorage or any object with `.stream`.
    - On write failure, best-effort removes the partially-written file.
    """
    dst_path = Path(dst)
    size = 0
    stream = getattr(file_obj, "stream", file_obj)

    # Best-effort rewind (non-seekable streams are acceptable).
    try:
        stream.seek(0)
    except Exception as exc:
        if context_prefix and report_seek_errors:
            ctx = f"{context_prefix}.{seek_context_suffix}"
            report_swallowed_exception(
                exc,
                context=ctx,
                log_key=ctx,
                log_window_seconds=log_window_seconds,
            )

    try:
        with dst_path.open("wb") as out:
            while True:
                chunk = stream.read(int(chunk_size))
                if not chunk:
                    break
                size += len(chunk)
                if max_bytes and size > max_bytes:
                    raise too_large_exc(too_large_message)
                out.write(chunk)
    except Exception:
        try:
            if dst_path.exists():
                dst_path.unlink()
        except Exception as exc:
            if context_prefix and report_cleanup_errors:
                ctx = f"{context_prefix}.{cleanup_context_suffix}"
                report_swallowed_exception(
                    exc,
                    context=ctx,
                    log_key=ctx,
                    log_window_seconds=log_window_seconds,
                )
        raise

    return size


def read_upload_bytes(
    file_obj,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
    too_large_exc: type[Exception] = UploadTooLarge,
    too_large_message: str = "File  .",
    context_prefix: str | None = None,
    report_seek_errors: bool = True,
    report_reset_errors: bool = True,
    seek_start_suffix: str = "seek_start",
    seek_reset_suffix: str = "seek_reset",
    log_window_seconds: int = 300,
) -> bytes:
    """Read an upload stream into memory with a hard size limit."""
    stream = getattr(file_obj, "stream", file_obj)

    # Best-effort rewind.
    try:
        stream.seek(0)
    except Exception as exc:
        if context_prefix and report_seek_errors:
            ctx = f"{context_prefix}.{seek_start_suffix}"
            report_swallowed_exception(
                exc,
                context=ctx,
                log_key=ctx,
                log_window_seconds=log_window_seconds,
            )

    buf = bytearray()
    size = 0
    while True:
        chunk = stream.read(int(chunk_size))
        if not chunk:
            break
        size += len(chunk)
        if max_bytes and size > max_bytes:
            raise too_large_exc(too_large_message)
        buf.extend(chunk)

    # Best-effort rewind for callers that reuse the stream.
    try:
        stream.seek(0)
    except Exception as exc:
        if context_prefix and report_reset_errors:
            ctx = f"{context_prefix}.{seek_reset_suffix}"
            report_swallowed_exception(
                exc,
                context=ctx,
                log_key=ctx,
                log_window_seconds=log_window_seconds,
            )

    return bytes(buf)
