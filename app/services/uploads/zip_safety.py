"""ZIP safety utilities for bomb prevention."""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from app.utils.error_logging import report_swallowed_exception


@dataclass
class ZipLimits:
    """Safety limits for ZIP extraction."""

    max_entries: int = 50
    max_total_size: int = 100 * 1024 * 1024  # 100MB
    max_single_file: int = 20 * 1024 * 1024  # 20MB
    # (uncompressed / compressed) ratio guard. Too-high ratios are typical zip-bomb signals.
    # Keep generous default to avoid false positives for normal documents.
    max_compression_ratio: float = 100.0


def _cfg_int(cfg, *keys: str, default: int) -> int:
    for k in keys:
        v = None
        try:
            v = cfg.get(k) if cfg is not None else None
        except Exception:
            v = None
        if v is None or v == "":
            try:
                v = os.environ.get(k)
            except Exception:
                v = None
        if v is None or v == "":
            continue
        try:
            return int(v)
        except Exception:
            continue
    return default


def _cfg_float(cfg, *keys: str, default: float) -> float:
    for k in keys:
        v = None
        try:
            v = cfg.get(k) if cfg is not None else None
        except Exception:
            v = None
        if v is None or v == "":
            try:
                v = os.environ.get(k)
            except Exception:
                v = None
        if v is None or v == "":
            continue
        try:
            return float(v)
        except Exception:
            continue
    return default


def get_limits(limits: ZipLimits | None = None) -> ZipLimits:
    """Return ZIP limits resolved from config/env (or a provided override)."""
    return _get_limits(limits)


def _get_limits(limits: ZipLimits | None) -> ZipLimits:
    if limits is not None:
        return limits
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            cfg = current_app.config
            return ZipLimits(
                max_entries=_cfg_int(cfg, "ZIP_MAX_ENTRIES", default=50),
                # Backward/Doc-compatible: .env.example uses *_UNCOMPRESSED
                max_total_size=_cfg_int(
                    cfg,
                    "ZIP_MAX_TOTAL_SIZE",
                    "ZIP_MAX_TOTAL_UNCOMPRESSED",
                    default=(100 * 1024 * 1024),
                ),
                max_single_file=_cfg_int(
                    cfg,
                    "ZIP_MAX_SINGLE_FILE",
                    "ZIP_MAX_SINGLE_UNCOMPRESSED",
                    default=(20 * 1024 * 1024),
                ),
                max_compression_ratio=_cfg_float(cfg, "ZIP_MAX_COMPRESSION_RATIO", default=100.0),
            )
    except Exception as exc:
        # Best-effort: fall back to defaults if Flask/app-config access fails.
        report_swallowed_exception(
            exc,
            context="uploads.zip_safety.get_limits",
            log_key="uploads.zip_safety.get_limits",
            log_window_seconds=300,
        )
    return ZipLimits()


class ZipSafetyError(Exception):
    """Raised when ZIP violates safety limits."""

    pass


def _validate_zip(zf: zipfile.ZipFile, limits: ZipLimits) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > limits.max_entries:
        raise ZipSafetyError(f"Too many entries: {len(infos)} (max: {limits.max_entries})")

    total_size = sum(info.file_size for info in infos)
    if total_size > limits.max_total_size:
        raise ZipSafetyError(
            f"Total size too large: {total_size} bytes (max: {limits.max_total_size})"
        )

    # Compression ratio guard (best-effort)
    # Note: compress_size can be 0 for empty files; skip those to avoid div-by-zero.
    try:
        max_ratio = float(limits.max_compression_ratio or 0.0)
    except Exception:
        max_ratio = 0.0
    if max_ratio and max_ratio > 0:
        for info in infos:
            try:
                if info.file_size and info.compress_size and info.compress_size > 0:
                    ratio = float(info.file_size) / float(info.compress_size)
                    if ratio > max_ratio:
                        raise ZipSafetyError(
                            f"Suspicious compression ratio: {ratio:.1f} (max: {max_ratio}) for {info.filename}"
                        )
            except ZipSafetyError:
                raise
            except Exception:
                continue

    return infos


def safe_list(data: bytes, limits: ZipLimits | None = None) -> list[str]:
    """
    List ZIP entries with safety checks.

    Raises ZipSafetyError if limits are exceeded.
    """
    limits = _get_limits(limits)

    with zipfile.ZipFile(BytesIO(data), "r") as zf:
        _validate_zip(zf, limits)
        return zf.namelist()


def safe_extract_bytes(data: bytes, filename: str, limits: ZipLimits | None = None) -> bytes:
    """
    Safely extract a single file from ZIP.

    Raises ZipSafetyError if limits are exceeded.
    """
    limits = _get_limits(limits)

    with zipfile.ZipFile(BytesIO(data), "r") as zf:
        # Verify overall limits first
        _validate_zip(zf, limits)

        info = zf.getinfo(filename)
        if info.file_size > limits.max_single_file:
            raise ZipSafetyError(
                f"File too large: {info.file_size} bytes (max: {limits.max_single_file})"
            )

        return zf.read(filename)


def find_file_by_ext(
    data: bytes, ext: str, limits: ZipLimits | None = None
) -> tuple[str, bytes] | None:
    """
    Find and extract first file with given extension.

    Returns (filename, content) or None if not found.
    """
    entries = safe_list(data, limits)

    for name in entries:
        if name.lower().endswith(ext.lower()):
            content = safe_extract_bytes(data, name, limits)
            return name, content

    return None


def find_files_by_ext(
    data: bytes, ext: str, limits: ZipLimits | None = None
) -> list[tuple[str, bytes]]:
    """
    Find and extract all files with given extension.

    Returns list of (filename, content) tuples.
    """
    entries = safe_list(data, limits)
    results = []

    for name in entries:
        if name.lower().endswith(ext.lower()):
            content = safe_extract_bytes(data, name, limits)
            results.append((name, content))

    return results


def find_file_by_ext_path(
    zip_path: str | Path, ext: str, limits: ZipLimits | None = None
) -> tuple[str, bytes] | None:
    """
    Find and extract first file with given extension from a ZIP file path.

    Returns (filename, content) or None if not found.
    """
    limits = _get_limits(limits)
    zip_path = Path(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = _validate_zip(zf, limits)
            ext_l = ext.lower()
            for info in infos:
                name = info.filename
                if not name or name.endswith("/"):
                    continue
                if name.lower().endswith(ext_l):
                    if info.file_size > limits.max_single_file:
                        raise ZipSafetyError(
                            f"File too large: {info.file_size} bytes (max: {limits.max_single_file})"
                        )
                    return name, zf.read(name)
            return None
    except zipfile.BadZipFile as e:
        raise ZipSafetyError(f"Invalid zip file: {e}") from e


def is_valid_zip(data: bytes) -> bool:
    """Check if data is a valid ZIP file."""
    try:
        with zipfile.ZipFile(BytesIO(data), "r") as zf:
            # Try to read the file list
            zf.namelist()
            return True
    except (zipfile.BadZipFile, Exception):
        return False
