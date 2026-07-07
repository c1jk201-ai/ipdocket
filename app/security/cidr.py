from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from typing import Iterable, Union

from flask import Flask, abort, g, request
from werkzeug.middleware.proxy_fix import ProxyFix

from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def _proxy_hint(request, trust_proxy: bool) -> dict:
    try:
        return {
            "remote_addr": getattr(request, "remote_addr", None),
            "x_forwarded_for": request.headers.get("X-Forwarded-For"),
            "x_forwarded_proto": request.headers.get("X-Forwarded-Proto"),
            "trust_proxy_headers": bool(trust_proxy),
        }
    except Exception:
        return {"trust_proxy_headers": bool(trust_proxy)}


PRIVATE_DEFAULTS = [
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
]

TRUSTED_PROXY_DEFAULTS = [
    "127.0.0.1/32",
    "::1/128",
]


def _get_client_ip(*, trust_proxy: bool) -> str:
    """
    Return the normalized client IP for CIDR checks.

    SECURITY: Do not read X-Forwarded-For directly here. When proxy headers are
    enabled, ProxyFix normalizes request.remote_addr only for trusted proxy peers.
    request.access_route still exposes raw header values and may include spoofed
    client-supplied entries.
    """
    if not trust_proxy:
        # Fail closed for CIDR-guarded routes when we detect proxy headers but are not configured
        # to trust them. Otherwise, a reverse proxy's private `remote_addr` may incorrectly pass
        # allowlists that default to private ranges (production safety default).
        try:
            if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
                return ""
        except Exception:
            return ""
    return str(request.remote_addr or "")


def _parse_cidrs(raw: str) -> list[Network]:
    raw = (raw or "").strip()
    if not raw:
        return []
    cidrs: list[Network] = []
    # Accept comma/line-separated inputs (admin UI textareas often contain newlines).
    raw = raw.replace("\r", "\n").replace("\n", ",")
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            cidrs.append(ipaddress.ip_network(p, strict=False))
        except ValueError:
            # Invalid CIDR should not crash the request; skip with a warning.
            logger.warning("Invalid CIDR entry ignored: %r", p)
            continue
    return cidrs


@lru_cache(maxsize=64)
def _compiled_allowlist(raw: str, env_name: str) -> list[Network]:
    # Production defaults to private/loopback ranges until an explicit
    # administrator allowlist is configured.
    allow = _parse_cidrs(raw)
    if allow:
        return allow
    if (env_name or "").lower() in ("prod", "production"):
        return _parse_cidrs(",".join(PRIVATE_DEFAULTS))
    return []  # non-production: no default allowlist.


def _is_allowed(ip_str: str, allowlist: Iterable[Network]) -> bool:
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    allowlist = list(allowlist)
    if not allowlist:
        # allowlist   None(non-prod Default)
        return True
    return any(ip in net for net in allowlist)


def _ip_in_networks(ip_str: str, networks: Iterable[Network]) -> bool:
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in networks)


@lru_cache(maxsize=32)
def _compiled_trusted_proxy_allowlist(raw: str) -> list[Network]:
    raw = (raw or "").strip() or ",".join(TRUSTED_PROXY_DEFAULTS)
    return _parse_cidrs(raw)


class _TrustedProxyFix:
    """Apply ProxyFix only when the direct peer is an operator-trusted proxy."""

    def __init__(self, app, *, trusted_proxy_cidrs: str, **kwargs):
        self.app = app
        self.proxy_fix = ProxyFix(app, **kwargs)
        self.trusted_proxy_networks = _compiled_trusted_proxy_allowlist(trusted_proxy_cidrs)

    def __call__(self, environ, start_response):
        remote_addr = str(environ.get("REMOTE_ADDR") or "")
        if _ip_in_networks(remote_addr, self.trusted_proxy_networks):
            return self.proxy_fix(environ, start_response)

        for key in (
            "HTTP_X_FORWARDED_FOR",
            "HTTP_X_REAL_IP",
            "HTTP_X_FORWARDED_PROTO",
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PORT",
            "HTTP_X_FORWARDED_PREFIX",
        ):
            environ.pop(key, None)
        return self.app(environ, start_response)


def init_cidr_guards(app: Flask) -> None:
    # ProxyFix (real IP / scheme)
    trust_proxy_boot = app.config.get(
        "SECURITY_TRUST_PROXY_HEADERS",
        app.config.get("TRUST_PROXY_HEADERS", False),  # legacy alias
    )
    if trust_proxy_boot:
        # Only trusted direct proxy peers may normalize X-Forwarded-* headers.
        app.wsgi_app = _TrustedProxyFix(
            app.wsgi_app,
            trusted_proxy_cidrs=app.config.get("SECURITY_TRUSTED_PROXY_CIDRS", ""),
            x_for=app.config.get("PROXY_FIX_X_FOR", 1),
            x_proto=app.config.get("PROXY_FIX_X_PROTO", 1),
            x_host=app.config.get("PROXY_FIX_X_HOST", 1),
            x_port=app.config.get("PROXY_FIX_X_PORT", 1),
            x_prefix=app.config.get("PROXY_FIX_X_PREFIX", 1),
        )

    @app.before_request
    def _cidr_guard():
        path = request.path or ""
        env_name = app.config.get("ENV", "") or app.config.get("FLASK_ENV", "")
        if path.startswith("/static/") or path in {
            "/favicon.ico",
            "/health",
            "/ready",
        }:
            return None

        # Allow runtime overrides via SystemConfig (prefer env if set).
        cidr_guard_enabled = bool(app.config.get("CIDR_GUARD_ENABLED", True))
        trust_proxy = bool(trust_proxy_boot)
        try:
            from app.services.core.config_service import ConfigService

            cidr_guard_enabled = bool(
                ConfigService.get_bool("CIDR_GUARD_ENABLED", cidr_guard_enabled, prefer_env=True)
            )
            trust_proxy = bool(
                ConfigService.get_bool("SECURITY_TRUST_PROXY_HEADERS", trust_proxy, prefer_env=True)
            )
        except Exception as exc:
            # Keep boot-time defaults if config service is unavailable.
            report_swallowed_exception(
                exc,
                context="security.cidr.guard.load_runtime_flags",
                log_key="security.cidr.guard.load_runtime_flags",
                log_window_seconds=300,
            )

        if not cidr_guard_enabled:
            return

        # Admin 
        if path.startswith("/admin"):
            raw = app.config.get("ADMIN_CIDR_ALLOWLIST", "")
            try:
                from app.services.core.config_service import ConfigService

                raw = (
                    ConfigService.get_str(
                        "ADMIN_CIDR_ALLOWLIST",
                        raw,
                        strip=True,
                        allow_blank=True,
                        prefer_env=True,
                    )
                    or ""
                )
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="security.cidr.guard.admin_allowlist",
                    log_key="security.cidr.guard.admin_allowlist",
                    log_window_seconds=300,
                )
            allow = _compiled_allowlist(raw, str(env_name))
            client_ip = _get_client_ip(trust_proxy=bool(trust_proxy))
            if not _is_allowed(client_ip, allow):
                hint = _proxy_hint(request, trust_proxy=bool(trust_proxy))
                try:
                    # Provide a structured hint for the 403 handler so operators
                    # can distinguish "Permissions None" vs "(CIDR) ".
                    g.cidr_deny = {
                        "scope": "admin",
                        "client_ip": client_ip,
                        **hint,
                    }
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="security.cidr.guard.admin_deny_context",
                        log_key="security.cidr.guard.admin_deny_context",
                        log_window_seconds=300,
                    )
                logger.warning("CIDR deny (admin): %s", hint)
                abort(403, "   exists. (CIDR allowlist)")

        # Internal API (if needed prefix )
        if path.startswith("/internal") or path.startswith("/api/internal"):
            raw = app.config.get("INTERNAL_API_CIDR_ALLOWLIST", "")
            try:
                from app.services.core.config_service import ConfigService

                raw = (
                    ConfigService.get_str(
                        "INTERNAL_API_CIDR_ALLOWLIST",
                        raw,
                        strip=True,
                        allow_blank=True,
                        prefer_env=True,
                    )
                    or ""
                )
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="security.cidr.guard.internal_allowlist",
                    log_key="security.cidr.guard.internal_allowlist",
                    log_window_seconds=300,
                )
            allow = _compiled_allowlist(raw, str(env_name))
            client_ip = _get_client_ip(trust_proxy=bool(trust_proxy))
            if not _is_allowed(client_ip, allow):
                hint = _proxy_hint(request, trust_proxy=bool(trust_proxy))
                try:
                    g.cidr_deny = {
                        "scope": "internal",
                        "client_ip": client_ip,
                        **hint,
                    }
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="security.cidr.guard.internal_deny_context",
                        log_key="security.cidr.guard.internal_deny_context",
                        log_window_seconds=300,
                    )
                logger.warning("CIDR deny (internal): %s", hint)
                abort(403, "   exists. (CIDR allowlist)")
