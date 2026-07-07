from app_config.settings.base import _env_bool, _env_int, _env_str


class DatabaseSettings:
    SQLALCHEMY_DATABASE_URI = _env_str("DATABASE_URL", "")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 20)
    DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 50)
    DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 30)
    DB_POOL_RECYCLE_SECONDS = _env_int("DB_POOL_RECYCLE_SECONDS", 0)
    DB_POOL_PRE_PING = _env_bool("DB_POOL_PRE_PING", True)
    DB_STATEMENT_TIMEOUT_MS = _env_int("DB_STATEMENT_TIMEOUT_MS", 0)

    DB_LOCK_TIMEOUT_MS = _env_int("DB_LOCK_TIMEOUT_MS", 5000)
    DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS = _env_int(
        "DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS", 60000
    )

    MIGRATION_ADVISORY_LOCK_KEY = _env_str(
        "MIGRATION_ADVISORY_LOCK_KEY",
        _env_str("DB_MIGRATE_ADVISORY_LOCK_KEY", ""),
    )
