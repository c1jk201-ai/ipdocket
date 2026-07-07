import os

from app_config.settings.base import (
    BaseSettings,
    _env_bool,
    _env_float,
    _env_int,
    _env_list,
    _env_path_with_default,
    _env_str,
)


class StorageSettings:
    UPLOAD_STORAGE_ROOT = _env_str(
        "UPLOAD_STORAGE_ROOT", os.path.join(BaseSettings.BASE_DIR, "uploads")
    )
    BACKUP_STORAGE_ROOT = _env_str(
        "BACKUP_STORAGE_ROOT", os.path.join(BaseSettings.BASE_DIR, "backups")
    )

    UPLOAD_RETENTION_DAYS = _env_int("UPLOAD_RETENTION_DAYS", 365)

    STORAGE_TYPE = _env_str("STORAGE_TYPE", "local").lower()

    S3_ENDPOINT = _env_str("S3_ENDPOINT", "")
    S3_REGION = _env_str("S3_REGION", "sgp1")
    S3_ACCESS_KEY = _env_str("S3_ACCESS_KEY", "")
    S3_SECRET_KEY = _env_str("S3_SECRET_KEY", "")
    S3_BUCKET = _env_str("S3_BUCKET", "")
    S3_PRESIGNED_EXPIRY = _env_int("S3_PRESIGNED_EXPIRY", 3600)

    UPLOAD_FOLDER = _env_path_with_default(
        "UPLOAD_FOLDER",
        candidates=[os.path.join(BaseSettings.BASE_DIR, "data", "uploads")],
        fallback=os.path.join(BaseSettings.BASE_DIR, "data", "uploads"),
    )
    BACKUP_DIR = _env_path_with_default(
        "BACKUP_DIR",
        candidates=[os.path.join(BaseSettings.BASE_DIR, "data", "backups")],
        fallback=os.path.join(BaseSettings.BASE_DIR, "data", "backups"),
    )
    ATTACHMENTS_DIR = _env_path_with_default(
        "ATTACHMENTS_DIR",
        candidates=[os.path.join(BaseSettings.BASE_DIR, "data", "attachments")],
        fallback=os.path.join(BaseSettings.BASE_DIR, "data", "attachments"),
    )
    CLIENT_ATTACHMENTS_DIR = _env_path_with_default(
        "CLIENT_ATTACHMENTS_DIR",
        candidates=[os.path.join(BaseSettings.BASE_DIR, "uploads", "clients")],
        fallback=os.path.join(BaseSettings.BASE_DIR, "uploads", "clients"),
    )

    ALLOWED_ATTACHMENT_EXTENSIONS = {
        ext.lower().lstrip(".")
        for ext in _env_list(
            "ALLOWED_ATTACHMENT_EXTENSIONS",
            "pdf,png,jpg,jpeg,gif,webp,zip,hwp,doc,docx,xls,xlsx,ppt,pptx,txt,csv",
        )
    }

    BACKUP_RETENTION_DAYS = _env_int("BACKUP_RETENTION_DAYS", 30)
    BACKUP_RETENTION_MAX_COUNT = _env_int("BACKUP_RETENTION_MAX_COUNT", 30)
    BACKUP_RETENTION_MAX_BYTES = _env_int("BACKUP_RETENTION_MAX_BYTES", 0)
    BACKUP_ATTACHMENTS_RETENTION_DAYS = _env_int(
        "BACKUP_ATTACHMENTS_RETENTION_DAYS", BACKUP_RETENTION_DAYS
    )
    BACKUP_ATTACHMENTS_RETENTION_MAX_COUNT = _env_int("BACKUP_ATTACHMENTS_RETENTION_MAX_COUNT", 10)
    BACKUP_ATTACHMENTS_RETENTION_MAX_BYTES = _env_int("BACKUP_ATTACHMENTS_RETENTION_MAX_BYTES", 0)
    TRANSFER_BUNDLE_RETENTION_DAYS = _env_int(
        "TRANSFER_BUNDLE_RETENTION_DAYS", BACKUP_RETENTION_DAYS
    )
    TRANSFER_BUNDLE_RETENTION_MAX_COUNT = _env_int("TRANSFER_BUNDLE_RETENTION_MAX_COUNT", 5)
    TRANSFER_BUNDLE_RETENTION_MAX_BYTES = _env_int("TRANSFER_BUNDLE_RETENTION_MAX_BYTES", 0)

    BACKUP_PG_NO_OWNER = _env_bool("BACKUP_PG_NO_OWNER", True)
    BACKUP_PG_NO_PRIVILEGES = _env_bool("BACKUP_PG_NO_PRIVILEGES", True)
    BACKUP_PG_FORMAT = _env_str("BACKUP_PG_FORMAT", "dump").lower()
    BACKUP_PG_COMPRESS = _env_str("BACKUP_PG_COMPRESS", "")
    BACKUP_PG_COMPRESSION = _env_int("BACKUP_PG_COMPRESSION", 6)
    BACKUP_AUTO_INTERVAL_HOURS = _env_int("BACKUP_AUTO_INTERVAL_HOURS", 24)

    UPLOAD_MAX_BYTES = _env_int("UPLOAD_MAX_BYTES", 16 * 1024 * 1024)
    MAX_CONTENT_LENGTH = _env_int("MAX_CONTENT_LENGTH", UPLOAD_MAX_BYTES)
    FILE_ASSET_MAX_BYTES = _env_int("FILE_ASSET_MAX_BYTES", MAX_CONTENT_LENGTH)
    INVOICE_ATTACHMENT_MAX_BYTES = _env_int("INVOICE_ATTACHMENT_MAX_BYTES", FILE_ASSET_MAX_BYTES)
    EMAIL_ATTACHMENT_READ_MAX_BYTES = _env_int("EMAIL_ATTACHMENT_READ_MAX_BYTES", 25 * 1024 * 1024)
    BIB_MAX_PARSE_BYTES = _env_int("BIB_MAX_PARSE_BYTES", 5 * 1024 * 1024)
    ZIP_MAX_ENTRIES = _env_int("ZIP_MAX_ENTRIES", 200)
    ZIP_MAX_TOTAL_UNCOMPRESSED = _env_int("ZIP_MAX_TOTAL_UNCOMPRESSED", 50 * 1024 * 1024)
    ZIP_MAX_TOTAL_SIZE = _env_int("ZIP_MAX_TOTAL_SIZE", ZIP_MAX_TOTAL_UNCOMPRESSED)
    ZIP_MAX_SINGLE_UNCOMPRESSED = _env_int("ZIP_MAX_SINGLE_UNCOMPRESSED", 20 * 1024 * 1024)
    ZIP_MAX_SINGLE_FILE = _env_int("ZIP_MAX_SINGLE_FILE", ZIP_MAX_SINGLE_UNCOMPRESSED)
    ZIP_MAX_COMPRESSION_RATIO = _env_float("ZIP_MAX_COMPRESSION_RATIO", 100.0)
    ZIP_UPLOAD_MAX_BYTES = _env_int("ZIP_UPLOAD_MAX_BYTES", FILE_ASSET_MAX_BYTES)
    LEGACY_NOTICE_ZIP_MAX_BYTES = _env_int(
        "LEGACY_NOTICE_ZIP_MAX_BYTES",
        _env_int("KIPO_ZIP_MAX_BYTES", 100 * 1024 * 1024),
    )
    LEGACY_NOTICE_ZIP_MAX_TOTAL_SIZE = _env_int(
        "LEGACY_NOTICE_ZIP_MAX_TOTAL_SIZE",
        _env_int("KIPO_ZIP_MAX_TOTAL_SIZE", LEGACY_NOTICE_ZIP_MAX_BYTES),
    )
    KIPO_ZIP_MAX_BYTES = LEGACY_NOTICE_ZIP_MAX_BYTES
    KIPO_ZIP_MAX_TOTAL_SIZE = LEGACY_NOTICE_ZIP_MAX_TOTAL_SIZE
    UPLOAD_PARSER_TIMEOUT_SECONDS = _env_int("UPLOAD_PARSER_TIMEOUT_SECONDS", 20)
    UPLOAD_VIRUS_SCAN_COMMAND = _env_str("UPLOAD_VIRUS_SCAN_COMMAND", "")
    UPLOAD_VIRUS_SCAN_MODE = _env_str("UPLOAD_VIRUS_SCAN_MODE", "async").lower()
    UPLOAD_VIRUS_SCAN_TIMEOUT_SECONDS = _env_int("UPLOAD_VIRUS_SCAN_TIMEOUT_SECONDS", 30)
    UPLOAD_VIRUS_SCAN_FAIL_OPEN = _env_bool("UPLOAD_VIRUS_SCAN_FAIL_OPEN", False)
    LEGACY_NOTICE_UPLOAD_AUTO_APPLY_TRUSTED = _env_bool(
        "LEGACY_NOTICE_UPLOAD_AUTO_APPLY_TRUSTED",
        _env_bool("KIPO_UPLOAD_AUTO_APPLY_TRUSTED", False),
    )
    KIPO_UPLOAD_AUTO_APPLY_TRUSTED = LEGACY_NOTICE_UPLOAD_AUTO_APPLY_TRUSTED
    RESPONSE_UPLOAD_DIRECT_AUTO_APPLY_CONFIDENCE = _env_str(
        "RESPONSE_UPLOAD_DIRECT_AUTO_APPLY_CONFIDENCE", "HIGH"
    )
