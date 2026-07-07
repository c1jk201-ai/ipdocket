from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, current_app

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class HealthItem:
    key: str
    status: Status
    message: str


def _sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _mask_uri_credentials(uri: str) -> str:
    raw = (uri or "").strip()
    if not raw:
        return "-"
    try:
        parts = urlsplit(raw)
        if not parts.netloc:
            return raw
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        if parts.password is not None:
            userinfo = f"{parts.username or ''}:***"
            netloc = f"{userinfo}@{host}"
        elif parts.username is not None:
            netloc = f"***@{host}"
        else:
            netloc = parts.netloc
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "***" if "@" in raw else raw


def _key_fingerprint(secret_key: str) -> str:
    # Track session-signing key changes without storing the raw secret.
    return _sha256(secret_key)


def _fingerprint_file(app: Flask) -> Path:
    # instance_path /Start     
    inst = Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)
    return inst / ".key_fingerprint"


def accept_key_rotation(app: Flask) -> None:
    """
    Administrator '  Change '  .
    mismatch    fingerprint Updated.
    """
    secret_key = app.config.get("SECRET_KEY", "")
    fp = _key_fingerprint(secret_key)
    _fingerprint_file(app).write_text(fp, encoding="utf-8")


def _check_key_rotation(app: Flask) -> Optional[HealthItem]:
    secret_key = app.config.get("SECRET_KEY", "")
    if not secret_key:
        return HealthItem(
            key="key_rotation",
            status="fail",
            message="SECRET_KEY is empty. Session cookies and CSRF protection are unsafe.",
        )

    fp_path = _fingerprint_file(app)
    current_fp = _key_fingerprint(secret_key)
    if not fp_path.exists():
        #  / row: fingerprint Reset
        fp_path.write_text(current_fp, encoding="utf-8")
        return HealthItem(
            key="key_rotation",
            status="ok",
            message=" fingerprint Reset Done( row).",
        )

    prev_fp = fp_path.read_text(encoding="utf-8").strip()
    if prev_fp != current_fp:
        return HealthItem(
            key="key_rotation",
            status="warn",
            message=(
                "SECRET_KEY changed since the saved fingerprint. "
                "Existing sessions and CSRF tokens may be invalid. "
                "Use accept rotation after confirming the change is intentional."
            ),
        )
    return HealthItem(key="key_rotation", status="ok", message=" fingerprint match.")


def _check_key_strength(app: Flask) -> List[HealthItem]:
    items: List[HealthItem] = []
    secret_key = app.config.get("SECRET_KEY", "")
    env_name = (app.config.get("ENV", "") or "").lower()

    # SECURITY: fail/warn on common bad defaults
    if env_name in ("prod", "production"):
        if not secret_key:
            items.append(
                HealthItem("secret_key_present", "fail", "SECRET_KEY is missing.")
            )
        elif secret_key == "dev-secret-key":
            items.append(
                HealthItem(
                    "secret_key_default",
                    "fail",
                    "SECRET_KEY is still using the development default. Rotate it immediately.",
                )
            )
        else:
            items.append(HealthItem("secret_key_default", "ok", "SECRET_KEY is customized."))
    if len(secret_key) < 32:
        items.append(
            HealthItem("secret_key_strength", "warn", "SECRET_KEY Length (>=32 ).")
        )
    else:
        items.append(HealthItem("secret_key_strength", "ok", "SECRET_KEY Length ."))

    return items


def _check_ratelimit_storage(app: Flask) -> HealthItem:
    env_name = (app.config.get("ENV", "") or "").lower()
    uri = (app.config.get("RATELIMIT_STORAGE_URI") or "").strip().lower()
    display_uri = _mask_uri_credentials(uri)
    if env_name in ("prod", "production"):
        if uri.startswith("memory://") or uri == "":
            return HealthItem(
                "ratelimit_storage",
                "fail",
                "RATELIMIT_STORAGE_URI must use shared storage such as Redis; memory:// is not allowed in production.",
            )
    return HealthItem("ratelimit_storage", "ok", f"RATELIMIT_STORAGE_URI={display_uri}")


def _check_csp_mode(app: Flask) -> HealthItem:
    env_name = (app.config.get("ENV", "") or "").lower()
    mode = (app.config.get("CSP_MODE") or "ENFORCE").upper()
    if env_name in ("prod", "production") and mode == "OFF":
        return HealthItem("csp", "warn", "CSP is disabled in production. Use REPORT_ONLY at minimum.")
    if env_name in ("prod", "production") and mode == "REPORT_ONLY":
        return HealthItem(
            "csp", "warn", "CSP is report-only in production. Move to ENFORCE when ready."
        )
    return HealthItem("csp", "ok", f"CSP_MODE={mode}")


def _check_hsts(app: Flask) -> HealthItem:
    env_name = (app.config.get("ENV", "") or "").lower()
    enabled = bool(app.config.get("HSTS_ENABLED", True))
    max_age = int(app.config.get("HSTS_MAX_AGE_SECONDS", 0))
    if env_name in ("prod", "production") and (not enabled or max_age <= 0):
        return HealthItem(
            "hsts", "warn", "HSTS is disabled or has max_age=0 in production. Enable it for HTTPS."
        )
    return HealthItem("hsts", "ok", f"HSTS enabled={enabled}, max_age={max_age}")


def _check_cookie_security(app: Flask) -> HealthItem:
    env_name = (app.config.get("ENV", "") or "").lower()
    sess_secure = bool(app.config.get("SESSION_COOKIE_SECURE", False))
    rem_secure = bool(app.config.get("REMEMBER_COOKIE_SECURE", False))
    same_site = str(app.config.get("SESSION_COOKIE_SAMESITE", "") or "").strip()
    if env_name in ("prod", "production"):
        if not sess_secure or not rem_secure:
            return HealthItem(
                "cookie_secure",
                "warn",
                "SESSION_COOKIE_SECURE and REMEMBER_COOKIE_SECURE should be true for HTTPS production use.",
            )
        if same_site.lower() == "none" and not sess_secure:
            return HealthItem("cookie_samesite", "warn", "SameSite=None   Secure Required.")
    return HealthItem(
        "cookie_secure",
        "ok",
        f"cookie secure(session={sess_secure}, remember={rem_secure}), samesite={same_site or '-'}",
    )


def _check_admin_cidr(app: Flask) -> HealthItem:
    env_name = (app.config.get("ENV", "") or "").lower()
    raw_default = (app.config.get("ADMIN_CIDR_ALLOWLIST") or "").strip()
    raw = raw_default
    try:
        from app.services.core.config_service import ConfigService

        raw = (
            ConfigService.get_str(
                "ADMIN_CIDR_ALLOWLIST",
                raw_default,
                strip=True,
                allow_blank=True,
                prefer_env=True,
            )
            or ""
        ).strip()
    except Exception:
        raw = raw_default
    if env_name in ("prod", "production") and not raw:
        return HealthItem(
            "admin_cidr",
            "warn",
            "ADMIN_CIDR_ALLOWLIST is empty. Configure an explicit administrator network allowlist.",
        )
    return HealthItem("admin_cidr", "ok", "ADMIN_CIDR_ALLOWLIST is configured.")


def get_security_health(app: Optional[Flask] = None) -> Dict:
    app = app or current_app
    items: List[HealthItem] = []

    key_rotation = _check_key_rotation(app)
    if key_rotation:
        items.append(key_rotation)
    items.extend(_check_key_strength(app))
    items.append(_check_ratelimit_storage(app))
    items.append(_check_csp_mode(app))
    items.append(_check_hsts(app))
    items.append(_check_cookie_security(app))
    items.append(_check_admin_cidr(app))

    # overall
    overall: Status = "ok"
    if any(i.status == "fail" for i in items):
        overall = "fail"
    elif any(i.status == "warn" for i in items):
        overall = "warn"

    return {
        "overall": overall,
        "items": [i.__dict__ for i in items],
    }
