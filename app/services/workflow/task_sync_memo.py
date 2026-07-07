from __future__ import annotations

import json
from typing import Any

from app.models.docket import DocketItem
from app.services.workflow.task_sync_constants import _MANUAL_WORKFLOW_ASSIGNMENT_KEY


def _coerce_user_id(value: object) -> int | None:
    try:
        user_id = int(value) if value is not None and str(value).strip() else None
    except Exception:
        return None
    if user_id and user_id > 0:
        return user_id
    return None


def _docket_memo_json(docket_item: DocketItem | None) -> dict[str, Any] | None:
    raw = (getattr(docket_item, "memo", None) or "").strip() if docket_item else ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _manual_assignment_override_for_docket(
    docket_item: DocketItem | None,
) -> tuple[int | None, int | None, int | None] | None:
    memo = _docket_memo_json(docket_item)
    if memo is None:
        return None
    raw = memo.get(_MANUAL_WORKFLOW_ASSIGNMENT_KEY)
    if not isinstance(raw, dict):
        return None
    if not bool(raw.get("enabled", True)):
        return None
    return (
        _coerce_user_id(raw.get("handler_id")),
        _coerce_user_id(raw.get("attorney_assignee_id")),
        _coerce_user_id(raw.get("manager_assignee_id")),
    )
