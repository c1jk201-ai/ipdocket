"""DEPRECATED: backward-compatibility shim.

This module used to host case kind inference logic. It has been moved to
`app.services.case.case_kind` to avoid circular imports when services/scripts
import case-kind helpers (importing from `app.blueprints.case.*` triggers eager
route imports via the package `__init__.py`).

Keep this file so older imports and tests keep working.
"""

from __future__ import annotations

# Re-export public helpers for legacy import paths.
from app.services.case.case_kind import (  # noqa: F401
    PATENT_LIKE_TYPES,
    CaseKind,
    _apply_case_kind_to_matter,
    _has_litigation_keyword,
    _infer_case_kind,
    _infer_case_kind_from_app_no,
    _infer_case_kind_from_right_name,
    _lookup_app_no,
    _lookup_raw_right_label,
    _normalize_case_division,
    _normalize_case_type,
)
