import os

from app_config.settings.base import (
    BaseSettings,
    _env_bool,
    _env_float,
    _env_int,
    _env_str,
    _normalize_path,
)


class IntegrationSettings:
    EXTERNAL_API_MAX_ATTEMPTS = _env_int("EXTERNAL_API_MAX_ATTEMPTS", 3)
    EXTERNAL_API_RETRY_BASE_DELAY_SECONDS = _env_float("EXTERNAL_API_RETRY_BASE_DELAY_SECONDS", 0.5)
    EXTERNAL_API_RETRY_MAX_DELAY_SECONDS = _env_float("EXTERNAL_API_RETRY_MAX_DELAY_SECONDS", 8.0)
    EXTERNAL_API_ALERTS_ENABLED = _env_bool("EXTERNAL_API_ALERTS_ENABLED", False)
    EXTERNAL_API_AUTOPAUSE_ENABLED = _env_bool("EXTERNAL_API_AUTOPAUSE_ENABLED", True)
    EXTERNAL_API_AUTOPAUSE_CALENDAR_KEYS = _env_str(
        "EXTERNAL_API_AUTOPAUSE_CALENDAR_KEYS", "google_calendar"
    )
    EXTERNAL_API_AUTOPAUSE_EMAIL_KEYS = _env_str("EXTERNAL_API_AUTOPAUSE_EMAIL_KEYS", "imap,graph")

    # Bank account inquiry / transaction import
    # manual: direct ledger entry (default for US deployments)
    # plaid: Plaid access-token based import
    BANK_ACCOUNT_DATA_PROVIDER = _env_str("BANK_ACCOUNT_DATA_PROVIDER", "manual")
    BANK_ACCOUNT_BASE_CURRENCY = _env_str("BANK_ACCOUNT_BASE_CURRENCY", "USD")
    PLAID_ENV = _env_str("PLAID_ENV", "sandbox")
    PLAID_CLIENT_ID = _env_str("PLAID_CLIENT_ID", "")
    PLAID_SECRET = _env_str("PLAID_SECRET", "")
    PLAID_ACCESS_TOKEN = _env_str("PLAID_ACCESS_TOKEN", "")
    PLAID_ACCOUNT_IDS = _env_str("PLAID_ACCOUNT_IDS", "")

    STAFF_EMAIL_DOMAINS = _env_str("STAFF_EMAIL_DOMAINS", "")
    INTERNAL_EMAIL_DOMAINS = _env_str("INTERNAL_EMAIL_DOMAINS", "")
    ALLOW_PASSWORD_LOGIN = _env_bool("ALLOW_PASSWORD_LOGIN", True)
    LOCAL_ADMIN_BOOTSTRAP_ENABLED = _env_bool("LOCAL_ADMIN_BOOTSTRAP_ENABLED", False)

    GOOGLE_CALENDAR_ID_DEADLINE = _env_str("GOOGLE_CALENDAR_ID_DEADLINE", "")
    GOOGLE_CALENDAR_ID_RENEWAL = _env_str("GOOGLE_CALENDAR_ID_RENEWAL", "")
    GOOGLE_SHARED_SYNC_EMAILS = _env_str("GOOGLE_SHARED_SYNC_EMAILS", "")
    GOOGLE_CALENDAR_WORK_DEPT_MAP = _env_str("GOOGLE_CALENDAR_WORK_DEPT_MAP", "{}")
    GOOGLE_CALENDAR_MGMT_ALL = _env_str("GOOGLE_CALENDAR_MGMT_ALL", "")
    GOOGLE_SHARED_MGMT_EMAIL = _env_str("GOOGLE_SHARED_MGMT_EMAIL", "")
    GOOGLE_CALENDAR_WORK_ALL = _env_str("GOOGLE_CALENDAR_WORK_ALL", "")
    GOOGLE_SHARED_WORK_EMAIL = _env_str("GOOGLE_SHARED_WORK_EMAIL", "")

    ENABLE_TEST_ACCOUNTS = _env_bool("ENABLE_TEST_ACCOUNTS", False)

    NKEAPS_MDB_PATH = _env_str(
        "NKEAPS_MDB_PATH", os.path.join(BaseSettings.BASE_DIR, "data", "bib_schema.sqlite")
    )
    LEGACY_NOTICE_MESSAGE_DB_PATH = _env_str(
        "LEGACY_NOTICE_MESSAGE_DB_PATH",
        _env_str(
            "KIPOMSG_DB_PATH",
            os.path.join(BaseSettings.BASE_DIR, "data", "legacy_notice_messages.sqlite"),
        ),
    )
    KIPOMSG_DB_PATH = LEGACY_NOTICE_MESSAGE_DB_PATH
    NKEAPS_XML_BASE_DIR = _normalize_path(
        _env_str("NKEAPS_XML_BASE_DIR", os.path.join(BaseSettings.BASE_DIR, "data", "nkeaps")),
        BaseSettings.BASE_DIR,
    )
