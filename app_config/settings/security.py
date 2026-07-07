from app_config.settings.base import (
    BaseSettings,
    _env_bool,
    _env_has,
    _env_int,
    _env_list,
    _env_str,
)

_RUNTIME_ENV = _env_str("FLASK_ENV", _env_str("ENV", _env_str("APP_ENV", ""))).strip().lower()
_IS_PRODUCTION_ENV = _RUNTIME_ENV in {"prod", "production"}


class SecuritySettings:
    SECRET_KEY = _env_str("SECRET_KEY", "")

    # Session / cookies
    SESSION_COOKIE_NAME = _env_str("SESSION_COOKIE_NAME", "new_ipm_session")
    REMEMBER_COOKIE_NAME = _env_str("REMEMBER_COOKIE_NAME", "new_ipm_remember_v2")
    SESSION_COOKIE_SAMESITE = _env_str("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_SECURE = _env_bool("REMEMBER_COOKIE_SECURE", False)
    AUTH_REMEMBER_ENABLED = _env_bool("AUTH_REMEMBER_ENABLED", False)
    PREFERRED_URL_SCHEME = _env_str("PREFERRED_URL_SCHEME", "http")

    PERMANENT_SESSION_LIFETIME_SECONDS = _env_int("PERMANENT_SESSION_LIFETIME_SECONDS", 8 * 3600)
    REMEMBER_COOKIE_DURATION_SECONDS = _env_int("REMEMBER_COOKIE_DURATION_SECONDS", 14 * 24 * 3600)
    SESSION_REFRESH_EACH_REQUEST = _env_bool("SESSION_REFRESH_EACH_REQUEST", True)

    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600

    SECURITY_TRUST_PROXY_HEADERS = _env_bool("SECURITY_TRUST_PROXY_HEADERS", False)
    SECURITY_TRUSTED_PROXY_CIDRS = _env_str("SECURITY_TRUSTED_PROXY_CIDRS", "127.0.0.1/32,::1/128")
    SECURITY_INTERNAL_CIDRS = _env_str("SECURITY_INTERNAL_CIDRS", "")
    SECURITY_BLOCKED_COUNTRY_CODES = _env_str("SECURITY_BLOCKED_COUNTRY_CODES", "")
    SECURITY_GEOIP_COUNTRY_DB = _env_str("SECURITY_GEOIP_COUNTRY_DB", "")

    SECURITY_HEADERS_ENABLED = _env_bool("SECURITY_HEADERS_ENABLED", True)
    SECURITY_FRAME_ALLOW_SAMEORIGIN = _env_bool("SECURITY_FRAME_ALLOW_SAMEORIGIN", True)

    if _env_has("CSP_MODE"):
        CSP_MODE = _env_str("CSP_MODE", "REPORT_ONLY").upper()
    else:
        _legacy_csp_enabled = _env_bool("SECURITY_CSP_ENABLED", True)
        _legacy_csp_report_only = _env_bool("SECURITY_CSP_REPORT_ONLY", False)
        if not _legacy_csp_enabled:
            CSP_MODE = "OFF"
        elif _legacy_csp_report_only:
            CSP_MODE = "REPORT_ONLY"
        else:
            CSP_MODE = "ENFORCE"

    _default_csp_policy = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    if _env_has("CSP_POLICY"):
        CSP_POLICY = _env_str("CSP_POLICY", _default_csp_policy)
    else:
        _legacy_csp = _env_str("SECURITY_CSP", "")
        CSP_POLICY = _legacy_csp or _default_csp_policy

    CSP_REPORT_URI = _env_str("CSP_REPORT_URI", "")

    if _env_has("HSTS_ENABLED"):
        HSTS_ENABLED = _env_bool("HSTS_ENABLED", True)
    else:
        HSTS_ENABLED = _env_bool("SECURITY_HSTS_ENABLED", True)
    if _env_has("HSTS_MAX_AGE_SECONDS"):
        HSTS_MAX_AGE_SECONDS = _env_int("HSTS_MAX_AGE_SECONDS", 31536000)
    else:
        HSTS_MAX_AGE_SECONDS = _env_int("SECURITY_HSTS_MAX_AGE", 31536000)
    if _env_has("HSTS_INCLUDE_SUBDOMAINS"):
        HSTS_INCLUDE_SUBDOMAINS = _env_bool("HSTS_INCLUDE_SUBDOMAINS", True)
    else:
        HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURITY_HSTS_INCLUDE_SUBDOMAINS", True)
    if _env_has("HSTS_PRELOAD"):
        HSTS_PRELOAD = _env_bool("HSTS_PRELOAD", False)
    else:
        HSTS_PRELOAD = _env_bool("SECURITY_HSTS_PRELOAD", False)

    TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", SECURITY_TRUST_PROXY_HEADERS)
    PROXY_FIX_X_FOR = _env_int("PROXY_FIX_X_FOR", 1)
    PROXY_FIX_X_PROTO = _env_int("PROXY_FIX_X_PROTO", 1)
    PROXY_FIX_X_HOST = _env_int("PROXY_FIX_X_HOST", 1)
    PROXY_FIX_X_PORT = _env_int("PROXY_FIX_X_PORT", 1)
    PROXY_FIX_X_PREFIX = _env_int("PROXY_FIX_X_PREFIX", 1)

    ADMIN_CIDR_ALLOWLIST = _env_str("ADMIN_CIDR_ALLOWLIST", "")
    INTERNAL_API_CIDR_ALLOWLIST = _env_str("INTERNAL_API_CIDR_ALLOWLIST", "")
    CIDR_GUARD_ENABLED = _env_bool("CIDR_GUARD_ENABLED", True)

    POLICY_ENGINE_ENABLED = _env_bool("POLICY_ENGINE_ENABLED", True)
    POLICY_DEFAULT_REQUIRE_ASSIGNEE_MATCH = _env_bool("POLICY_DEFAULT_REQUIRE_ASSIGNEE_MATCH", True)
    POLICY_RAW_SQL_GUARD_MODE = (
        _env_str("POLICY_RAW_SQL_GUARD_MODE", "enforce" if _IS_PRODUCTION_ENV else "report")
        .strip()
        .lower()
    )
    POLICY_RAW_SQL_BYPASS_REASON_REQUIRED = _env_bool(
        "POLICY_RAW_SQL_BYPASS_REASON_REQUIRED", False
    )

    RATELIMIT_STORAGE_URI = _env_str("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULTS = _env_list("RATELIMIT_DEFAULTS", "1000 per hour")
    RATELIMIT_REQUIRE_SHARED_STORAGE = _env_bool(
        "RATELIMIT_REQUIRE_SHARED_STORAGE", _IS_PRODUCTION_ENV
    )
    RATELIMIT_SWALLOW_ERRORS = _env_bool("RATELIMIT_SWALLOW_ERRORS", False)
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = _env_bool("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED", False)
