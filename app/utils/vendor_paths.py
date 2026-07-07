from __future__ import annotations

import os
from typing import Optional


def _default_base_dir() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def ensure_deadline_engine_path(base_dir: Optional[str] = None) -> bool:
    _ = (base_dir or "").strip() or _default_base_dir()
    return False
