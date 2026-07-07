from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception


def _default_dataset_path() -> Path:
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            base_dir = current_app.config.get("BASE_DIR")
            if base_dir:
                return Path(base_dir) / "data" / "match_dataset.jsonl"
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="auto_match_dataset._default_dataset_path",
            log_key="auto_match_dataset._default_dataset_path",
            log_window_seconds=300,
        )
    return Path(__file__).resolve().parents[2] / "data" / "match_dataset.jsonl"


def _dataset_settings() -> tuple[bool, Path, int | None]:
    enabled = ConfigService.get_bool("MATCH_DATASET_ENABLED", False)
    raw_path = ConfigService.get_str("MATCH_DATASET_PATH", "", allow_blank=False) or ""
    raw_max = ConfigService.get_int("MATCH_DATASET_MAX_BYTES", None, min_value=0)
    max_bytes = raw_max if raw_max and raw_max > 0 else None
    path = Path(raw_path) if raw_path else _default_dataset_path()
    return enabled, path, max_bytes


def _hash_text(value: str) -> str:
    if not value:
        return ""
    try:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return ""


def record_match_sample(
    *,
    source_type: str,
    filename: str,
    text: str,
    match_result: Any,
    ground_truth_matter_id: str | None = None,
    extra: dict | None = None,
) -> None:
    enabled, path, max_bytes = _dataset_settings()
    if not enabled:
        return

    payload: dict[str, Any] = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "source_type": (source_type or "").strip(),
        "filename": (filename or "").strip(),
        "text_sha256": _hash_text(text or ""),
        "text_snippet": (text or "")[:1200],
        "ground_truth_matter_id": ground_truth_matter_id,
    }

    if match_result is not None:
        if hasattr(match_result, "to_dict"):
            payload["match_result"] = match_result.to_dict()
        elif isinstance(match_result, dict):
            payload["match_result"] = match_result
        else:
            payload["match_result"] = {"value": str(match_result)}

    if extra:
        payload["extra"] = extra

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if max_bytes and path.exists():
            try:
                if path.stat().st_size >= max_bytes:
                    return
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="auto_match_dataset.record_match_sample.stat",
                    log_key="auto_match_dataset.record_match_sample.stat",
                    log_window_seconds=300,
                )
                return
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False))
            fp.write("\n")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="auto_match_dataset.record_match_sample",
            log_key="auto_match_dataset.record_match_sample",
            log_window_seconds=300,
        )
