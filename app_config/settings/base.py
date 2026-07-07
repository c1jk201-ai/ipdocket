import os

_TRUE_SET = {"1", "true", "yes", "on"}
_FALSE_SET = {"0", "false", "no", "off", ""}


def _env_str(key: str, default: str = "", *, strip: bool = True) -> str:
    raw = os.environ.get(key)
    if raw is None:
        raw = default
    if isinstance(raw, str):
        return raw.strip() if strip else raw
    return str(raw).strip() if strip else str(raw)


def _normalize_path(value: str, base_dir: str) -> str:
    path = (value or "").strip()
    if not path:
        return ""
    path = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.normpath(path)


def _env_path_with_default(key: str, *, candidates: list[str], fallback: str) -> str:
    raw = _env_str(key, "", strip=True)
    if raw:
        return raw
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.exists(path):
                return path
        except Exception:
            continue
    return fallback


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return bool(default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    val = str(raw).strip().lower()
    if val in _TRUE_SET:
        return True
    if val in _FALSE_SET:
        return False
    return bool(default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return int(default)
    try:
        val = str(raw).strip()
        if val == "":
            return int(default)
        return int(val)
    except Exception:
        return int(default)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return float(default)
    try:
        val = str(raw).strip()
        if val == "":
            return float(default)
        return float(val)
    except Exception:
        return float(default)


def _env_list(key: str, default: str = "", *, sep: str = ",") -> list[str]:
    raw = os.environ.get(key)
    if raw is None:
        raw = default
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    parts = [p.strip() for p in str(raw).split(sep)]
    return [p for p in parts if p]


def _env_has(key: str) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return False
    return str(raw).strip() != ""


class BaseSettings:
    _base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    BASE_DIR = _base_dir

    TIMEZONE = _env_str("TIMEZONE", _env_str("TZ", "America/New_York"))
    LOCALE = _env_str("LOCALE", "en-US")
    DATE_FORMAT = _env_str("DATE_FORMAT", "%m/%d/%Y")
    DATETIME_FORMAT = _env_str("DATETIME_FORMAT", "%m/%d/%Y %I:%M:%S %p")
    DATETIME_MINUTE_FORMAT = _env_str("DATETIME_MINUTE_FORMAT", "%m/%d/%Y %I:%M %p")
    BASE_URL = _env_str("BASE_URL", "http://127.0.0.1:5000")
    JSON_AS_ASCII = False
    PERF_HEADERS_ENABLED = _env_bool("PERF_HEADERS_ENABLED", False)
    STATIC_ASSET_MAX_AGE_SECONDS = _env_int("STATIC_ASSET_MAX_AGE_SECONDS", 31536000)
    STATIC_ASSET_UNVERSIONED_MAX_AGE_SECONDS = _env_int(
        "STATIC_ASSET_UNVERSIONED_MAX_AGE_SECONDS", 3600
    )
    STATIC_ASSET_IMMUTABLE = _env_bool("STATIC_ASSET_IMMUTABLE", True)
    STATIC_ASSET_VERSION_CACHE_TTL_SECONDS = _env_int("STATIC_ASSET_VERSION_CACHE_TTL_SECONDS", 5)

    # User access activity logging
    USER_ACCESS_LOG_ENABLED = _env_bool("USER_ACCESS_LOG_ENABLED", True)
    USER_ACCESS_LOG_ASYNC_ENABLED = _env_bool("USER_ACCESS_LOG_ASYNC_ENABLED", True)
    USER_ACCESS_LOG_INCLUDE_API_GET = _env_bool("USER_ACCESS_LOG_INCLUDE_API_GET", False)
    USER_ACCESS_LOG_METHODS = _env_str("USER_ACCESS_LOG_METHODS", "POST,PUT,PATCH,DELETE")
    USER_ACCESS_LOG_EXCLUDE_PREFIXES = _env_str("USER_ACCESS_LOG_EXCLUDE_PREFIXES", "")
    TASK_DISTRIBUTION_RULES_PATH = _env_str(
        "TASK_DISTRIBUTION_RULES_PATH",
        os.path.join(_base_dir, "app", "data", "task_distribution_rules.json"),
    )
    TASK_DISTRIBUTION_AUDIT_LOG = _env_bool("TASK_DISTRIBUTION_AUDIT_LOG", False)
    WORKFLOW_OWNER_FALLBACK_USER_ID = _env_int("WORKFLOW_OWNER_FALLBACK_USER_ID", 0)

    INVOICE_MODULE_DB_PATH = _env_str("INVOICE_MODULE_DB_PATH", "")
    INVOICE_MODULE_VIEW_BASE_URL = _env_str(
        "INVOICE_MODULE_VIEW_BASE_URL", "/accounting/invoice-system/invoices"
    )

    CASE_VIEW_HIDE_DUPLICATE_PANELS = _env_bool("CASE_VIEW_HIDE_DUPLICATE_PANELS", True)

    CASE_DETAIL_HISTORY_LIMIT = _env_int("CASE_DETAIL_HISTORY_LIMIT", 200)
    CASE_DETAIL_DUE_LIMIT = _env_int("CASE_DETAIL_DUE_LIMIT", 200)
    CASE_DETAIL_FM_PER_PAGE = _env_int("CASE_DETAIL_FM_PER_PAGE", 200)
    CASE_DETAIL_FM_MAX_PER_PAGE = _env_int("CASE_DETAIL_FM_MAX_PER_PAGE", 500)
    CASE_LIST_SPLIT_QUERY_ENABLED = _env_bool("CASE_LIST_SPLIT_QUERY_ENABLED", True)
    CASE_LIST_EXTRAS_CACHE_ENABLED = _env_bool("CASE_LIST_EXTRAS_CACHE_ENABLED", True)
    CASE_LIST_EXTRAS_CACHE_TTL_SECONDS = _env_int("CASE_LIST_EXTRAS_CACHE_TTL_SECONDS", 15)

    INVOICEAPP_INTEGRATED = _env_bool("INVOICEAPP_INTEGRATED", True)
    INVOICEAPP_TABLE_PREFIX = _env_str("INVOICEAPP_TABLE_PREFIX", "billing_")
    INVOICEAPP_UNIFIED_CLIENTS = _env_bool("INVOICEAPP_UNIFIED_CLIENTS", False)
    INVOICEAPP_AUTO_MIGRATE = _env_bool("INVOICEAPP_AUTO_MIGRATE", False)
    INVOICEAPP_RUNTIME_SCHEMA_BOOTSTRAP = _env_bool("INVOICEAPP_RUNTIME_SCHEMA_BOOTSTRAP", False)
    INVOICEAPP_CLIENT_SYNC_ENABLED = _env_bool("INVOICEAPP_CLIENT_SYNC_ENABLED", False)
    INVOICEAPP_DISABLE_ACCOUNTING_FEATURES = _env_bool(
        "INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False
    )
    INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED = _env_bool(
        "INVOICE_TIMELINE_TO_CASE_MEMO_ENABLED", False
    )
    AUTO_APPLY_BIZREG_TO_CLIENT = _env_bool("AUTO_APPLY_BIZREG_TO_CLIENT", False)
    AUTO_APPLY_BIZREG_OVERWRITE = _env_bool("AUTO_APPLY_BIZREG_OVERWRITE", False)
    INVOICE_LIST_PY_FILTER_MAX_ROWS = _env_int("INVOICE_LIST_PY_FILTER_MAX_ROWS", 20000)
    CRM_RECENT_MERGE_LOG_TTL_SECONDS = _env_int("CRM_RECENT_MERGE_LOG_TTL_SECONDS", 24 * 3600)
    CRM_APPLICANT_CODE_SECURED_DEBUG_ENABLED = _env_bool(
        "CRM_APPLICANT_CODE_SECURED_DEBUG_ENABLED", False
    )

    # Core system toggles
    DB_SCHEMA_AUTO_CREATE = _env_bool("DB_SCHEMA_AUTO_CREATE", False)
    DB_SCHEMA_FAIL_FAST = _env_bool("DB_SCHEMA_FAIL_FAST", False)
    STARTUP_CHECKS_ENABLED = _env_bool("STARTUP_CHECKS_ENABLED", True)
    STARTUP_CHECKS_FAIL_FAST = _env_bool("STARTUP_CHECKS_FAIL_FAST", False)
    STARTUP_CHECKS_ENFORCE = _env_bool("STARTUP_CHECKS_ENFORCE", False)
    STARTUP_CHECK_MODEL_MAPPERS_ON_BOOT = _env_bool("STARTUP_CHECK_MODEL_MAPPERS_ON_BOOT", True)
    STARTUP_CHECK_DB_OBJECTS_ON_BOOT = _env_bool("STARTUP_CHECK_DB_OBJECTS_ON_BOOT", True)
    STARTUP_CHECK_LEGACY_AGENCY_ASSETS = _env_bool(
        "STARTUP_CHECK_LEGACY_AGENCY_ASSETS",
        _env_bool("STARTUP_CHECK_KIPO_ASSETS", False),
    )
    STARTUP_CHECK_KIPO_ASSETS = STARTUP_CHECK_LEGACY_AGENCY_ASSETS
    READY_CHECK_MIGRATIONS = _env_bool("READY_CHECK_MIGRATIONS", False)
    READY_CHECK_DB_OBJECTS = _env_bool("READY_CHECK_DB_OBJECTS", True)
    READY_PUBLIC_INCLUDE_CHECKS = _env_bool("READY_PUBLIC_INCLUDE_CHECKS", False)
    READY_REQUIRED_DB_VIEWS = _env_list("READY_REQUIRED_DB_VIEWS", "v_matter_overview")
    READY_DB_POOL_MAX_UTILIZATION = _env_float("READY_DB_POOL_MAX_UTILIZATION", 0.95)
    READY_MAX_DURABLE_QUEUE_LAG_SECONDS = _env_int("READY_MAX_DURABLE_QUEUE_LAG_SECONDS", 0)
    READY_WORKER_HEARTBEAT_MAX_AGE_SECONDS = _env_int("READY_WORKER_HEARTBEAT_MAX_AGE_SECONDS", 0)
    STARTUP_REQUIRED_SYSTEM_CONFIG_KEYS = _env_list(
        "STARTUP_REQUIRED_SYSTEM_CONFIG_KEYS",
        "",
    )

    CONFIG_SNAPSHOT_ENABLED = _env_bool("CONFIG_SNAPSHOT_ENABLED", False)
    CONFIG_SERVICE_CACHE_TTL_SECONDS = _env_int("CONFIG_SERVICE_CACHE_TTL_SECONDS", 30)
    CONFIG_SERVICE_CACHE_MAX_KEYS = _env_int("CONFIG_SERVICE_CACHE_MAX_KEYS", 2048)
