from __future__ import annotations

from flask import Flask, current_app, request


def _build_csp_value(policy: str, report_uri: str) -> str:
    policy = (policy or "").strip()
    report_uri = (report_uri or "").strip()
    if not policy:
        return ""
    if report_uri:
        # CSP3: report-to ,   report-uri 
        if "report-uri" not in policy and "report-to" not in policy:
            base = policy.rstrip()
            if not base.endswith(";"):
                base = base + ";"
            return f"{base} report-uri {report_uri};"
    return policy


def _frame_ancestors_self(policy: str) -> str:
    policy = (policy or "").strip()
    if not policy:
        return ""
    parts = [p.strip() for p in policy.split(";") if p.strip()]
    updated = False
    for idx, part in enumerate(parts):
        if part.startswith("frame-ancestors"):
            parts[idx] = "frame-ancestors 'self'"
            updated = True
            break
    if not updated:
        parts.append("frame-ancestors 'self'")
    return "; ".join(parts) + ";"


def _allow_same_origin_frame() -> bool:
    # This app uses same-origin iframe embeds in multiple places (split views/modals/previews).
    # Default policy is allow + SAMEORIGIN; can be tightened via config/env.
    try:
        if bool(current_app.config.get("SECURITY_FRAME_ALLOW_SAMEORIGIN", True)):
            return True
        if (request.args.get("popup") or "").strip() == "1":
            return True
        path = (request.path or "").strip()
        if path.startswith("/files/") and path.endswith("/preview"):
            return True
        return False
    except Exception:
        return False


def init_security_headers(app: Flask) -> None:
    if not app.config.get("SECURITY_HEADERS_ENABLED", True):
        return

    @app.after_request
    def _apply_security_headers(resp):
        allow_frame = _allow_same_origin_frame()
        # 1) Basic hardening
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if allow_frame:
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        #  (if needed Extend)
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

        # 2) CSP: OFF / REPORT_ONLY / ENFORCE
        csp_mode = (current_app.config.get("CSP_MODE") or "ENFORCE").upper()
        if csp_mode != "OFF":
            policy = current_app.config.get("CSP_POLICY", "")
            if allow_frame:
                policy = _frame_ancestors_self(policy)
            csp_val = _build_csp_value(policy, current_app.config.get("CSP_REPORT_URI", ""))
            if csp_val:
                if csp_mode == "ENFORCE":
                    resp.headers["Content-Security-Policy"] = csp_val
                    # REPORT_ONLY  ( )
                    resp.headers.pop("Content-Security-Policy-Report-Only", None)
                else:
                    resp.headers["Content-Security-Policy-Report-Only"] = csp_val
                    resp.headers.pop("Content-Security-Policy", None)

        # 3) HSTS (HTTPSfrom)
        if current_app.config.get("HSTS_ENABLED", True) and request.is_secure:
            max_age = int(current_app.config.get("HSTS_MAX_AGE_SECONDS", 31536000))
            include_sub = current_app.config.get("HSTS_INCLUDE_SUBDOMAINS", True)
            preload = current_app.config.get("HSTS_PRELOAD", False)
            parts = [f"max-age={max_age}"]
            if include_sub:
                parts.append("includeSubDomains")
            if preload:
                parts.append("preload")
            resp.headers["Strict-Transport-Security"] = "; ".join(parts)

        return resp
