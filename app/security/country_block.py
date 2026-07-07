from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from app.utils.network_access import get_client_ip, is_blocked_country

logger = logging.getLogger(__name__)


def _wants_json_response() -> bool:
    try:
        accept = (request.headers.get("Accept") or "").lower()
    except Exception:
        accept = ""
    if "application/json" in accept:
        return True
    try:
        path = (request.path or "").lower()
    except Exception:
        path = ""
    if path.startswith("/api/") or path.startswith("/admin/api/"):
        return True
    return False


def init_country_block(app: Flask) -> None:
    """
    Enforce country blocking (e.g., CN/RU) early in the request lifecycle.

    Implementation detail:
    - Uses `app.utils.network_access.is_blocked_country()`
    - Supports GeoIP DB lookups OR CIDR list fallback at `/app/data/country_cidrs/<cc>.zone`.
    """

    @app.before_request
    def _country_block_guard():
        try:
            blocked = is_blocked_country()
        except Exception:
            # Security checks must be fail-open to avoid outages from misconfigurations.
            blocked = False

        if not blocked:
            return None

        ip = None
        try:
            ip = get_client_ip()
        except Exception:
            ip = None
        if logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "Blocked request by country policy (ip=%s path=%s)",
                ip,
                getattr(request, "path", None),
            )

        msg = "Access from this country is blocked."
        if _wants_json_response():
            return jsonify({"ok": False, "error": {"code": "blocked_country", "message": msg}}), 403
        return render_template("errors/blocked_country.html", message=msg), 403
