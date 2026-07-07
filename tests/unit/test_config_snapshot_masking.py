from app.app_factory import _sanitize_config_value as sanitize_app_factory_value
from app.core.setup.logging_setup import _sanitize_config_value as sanitize_logging_setup_value


def test_config_snapshot_masking_keeps_non_secret_operational_values_visible():
    for sanitize in (sanitize_app_factory_value, sanitize_logging_setup_value):
        assert sanitize("SECURITY_HEADERS_ENABLED", True) is True
        assert sanitize("SECURITY_TRUST_PROXY_HEADERS", False) is False
        assert sanitize("SECURITY_INTERNAL_CIDRS", "10.0.0.0/8") == "10.0.0.0/8"
        assert sanitize("GOOGLE_CALENDAR_ID_DEADLINE", "primary") == "primary"
        assert sanitize("CSP_REPORT_URI", "/csp-report") == "/csp-report"


def test_config_snapshot_masking_still_masks_secrets_and_database_uris():
    for sanitize in (sanitize_app_factory_value, sanitize_logging_setup_value):
        assert sanitize("OPENAI_API_KEY", "sk-test") == "***"
        assert sanitize("SQLALCHEMY_DATABASE_URI", "postgresql://user:pw@db/app") == "***"
        assert sanitize("RATELIMIT_STORAGE_URI", "redis://:pw@redis/0") == "***"
