from __future__ import annotations

from flask import Flask, g, jsonify, render_template, request
from flask.signals import got_request_exception

from app.core.setup.logging_setup import _log_swallowed


def register_error_handlers(app: Flask) -> None:
    def _format_max_upload_label(value: object | None) -> str | None:
        try:
            size = int(value or 0)
        except Exception:
            return None
        if size <= 0:
            return None
        if size < 1024:
            return f"{size}B"
        mb = size / (1024 * 1024)
        if mb >= 1:
            return f"{mb:.0f}MB" if float(mb).is_integer() else f"{mb:.1f}MB"
        kb = size / 1024
        return f"{kb:.0f}KB"

    @app.errorhandler(413)
    def request_entity_too_large(e):
        message = "The selected file exceeds the allowed upload size."
        try:
            wants_json = (request.path or "").startswith("/api/") or request.is_json
            if not wants_json:
                best = request.accept_mimetypes.best or ""
                wants_json = best == "application/json"
            if wants_json:
                return jsonify({"success": False, "error": message}), 413
        except Exception as exc:
            _log_swallowed("request_entity_too_large_handler", exc)

        max_upload_label = _format_max_upload_label(app.config.get("MAX_CONTENT_LENGTH"))
        return (
            render_template("errors/413.html", message=message, max_upload_label=max_upload_label),
            413,
        )

    @app.errorhandler(403)
    def forbidden(e):
        cidr_hint = None
        try:
            cidr_hint = getattr(g, "cidr_deny", None)
        except Exception:
            cidr_hint = None

        def _safe_description(err) -> str | None:
            try:
                desc = getattr(err, "description", None)
            except Exception:
                desc = None
            if not desc:
                return None
            text = str(desc).strip()
            if not text:
                return None
            # Hide the noisy default werkzeug text.
            if "You don't have the permission" in text:
                return None
            if text.lower() == "forbidden":
                return None
            if len(text) > 300:
                return None
            return text

        try:
            if (request.path or "").startswith("/admin/api/"):
                msg = _safe_description(e) or "forbidden"
                payload = {"success": False, "error": msg}
                if isinstance(cidr_hint, dict) and cidr_hint:
                    payload["cidr"] = {
                        "scope": cidr_hint.get("scope"),
                        "client_ip": cidr_hint.get("client_ip"),
                        "trust_proxy_headers": cidr_hint.get("trust_proxy_headers"),
                    }
                return jsonify(payload), 403
        except Exception as exc:
            _log_swallowed("forbidden_handler", exc)

        message = _safe_description(e) or "You do not have permission to access this page."
        if isinstance(cidr_hint, dict) and cidr_hint:
            # Override the generic permission message for CIDR-denied requests.
            message = "Your IP address is not allowed by the CIDR allowlist."
        return render_template("errors/403.html", message=message, cidr=cidr_hint), 403

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template("404.html"), 404

    from app.utils.error_logging import capture_exception

    @got_request_exception.connect_via(app)
    def _log_exception(sender, exception, **extra):
        capture_exception(exception)
