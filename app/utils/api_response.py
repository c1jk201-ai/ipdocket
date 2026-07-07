from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, Union

from flask import jsonify


def api_response(
    *,
    ok: bool = True,
    value: Any = None,
    meta: Optional[dict] = None,
    error: Optional[Union[str, dict]] = None,
    message: Optional[str] = None,
    status: int = 200,
    legacy: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, int]:
    """Return a normalized API response.

    Structure:
    {
        "ok": true/false,
        "value": <payload>,
        "meta": {...},
        "error": <error_code_or_detail>,
        "message": <human readable message>
    }
    """

    payload: Dict[str, Any] = {
        "ok": bool(ok),
        "value": value,
        "meta": meta or {},
        "error": error if not ok else None,
        "message": message,
    }
    if legacy:
        payload.update(legacy)
    # Remove None fields for brevity
    if payload["error"] is None:
        payload.pop("error")
    if payload["message"] is None:
        payload.pop("message")

    return jsonify(payload), status
