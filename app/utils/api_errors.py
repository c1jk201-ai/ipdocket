from __future__ import annotations

from typing import Any

from flask import g, jsonify


def json_error(
    code: str,
    message: str,
    *,
    status: int = 400,
    request_id: str | None = None,
    details: Any | None = None,
):
    payload = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details is not None:
        payload["error"]["details"] = details
    req_id = request_id or getattr(g, "request_id", None)
    if req_id:
        payload["request_id"] = req_id
    return jsonify(payload), status
