import json
import os
import tempfile
from typing import Any

from app.utils.error_logging import report_swallowed_exception


def read_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        # Corrupt/partial file: do NOT crash ingestion; fall back to default
        return default


def write_json_atomic(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=dir_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="json_atomic.write_json_atomic.cleanup",
                log_key="json_atomic.write_json_atomic.cleanup",
                log_window_seconds=300,
            )
