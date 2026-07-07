import os

from dotenv import load_dotenv

load_dotenv()  # Load variables from .env into os.environ

from app_config.settings.ai_automation import AiAutomationSettings
from app_config.settings.base import (
    BaseSettings,
    _env_bool,
    _env_float,
    _env_has,
    _env_int,
    _env_list,
    _env_str,
    _normalize_path,
)
from app_config.settings.database import DatabaseSettings
from app_config.settings.integrations import IntegrationSettings
from app_config.settings.scheduler import SchedulerSettings
from app_config.settings.security import SecuritySettings
from app_config.settings.storage import StorageSettings


class Config(
    BaseSettings,
    DatabaseSettings,
    SecuritySettings,
    StorageSettings,
    IntegrationSettings,
    SchedulerSettings,
    AiAutomationSettings,
):
    """
    Combined configuration class mapped out of the config/settings/ module.
    Maintains backward compatibility with all references like `from config import config`.
    """

    # Undo window explicitly added here or base.
    CASE_AUDIT_UNDO_SECONDS = os.environ.get("CASE_AUDIT_UNDO_SECONDS", 30)
    TASK_TODO_OVERDUE_DAYS = os.environ.get("TASK_TODO_OVERDUE_DAYS", 30)
    WORKLOG_AUTO_BACKFILL_OVERDUE_WINDOW_DAYS = _env_int(
        "WORKLOG_AUTO_BACKFILL_OVERDUE_WINDOW_DAYS", 3650
    )

    @classmethod
    def init_app(cls, app):
        return None


class DevelopmentConfig(Config):
    DEBUG = _env_bool("FLASK_DEBUG", False)
    CONFIG_NAME = "development"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


class ProductionConfig(Config):
    DEBUG = False
    CONFIG_NAME = "production"

    @classmethod
    def init_app(cls, app):
        super().init_app(app)

        # enforce secure cookie transmission
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config["REMEMBER_COOKIE_SECURE"] = True
        if str(app.config.get("BASE_URL") or "").strip().lower().startswith("https://"):
            app.config["PREFERRED_URL_SCHEME"] = "https"


class TestingConfig(Config):
    TESTING = True
    CONFIG_NAME = "testing"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URI") or "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    UPLOAD_VIRUS_SCAN_COMMAND = ""

    # disable caches during testing
    PDF_TEXT_CACHE_ENABLED = False
    CASE_LIST_EXTRAS_CACHE_ENABLED = False
    CONFIG_SERVICE_CACHE_TTL_SECONDS = 0


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
