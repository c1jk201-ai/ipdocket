import warnings

import pytest
from flask import Flask

from app.core.setup.security_guards import (
    _configure_image_safety,
    _guard_production_required_security_config,
)
from app.services.ops.security_health import _mask_uri_credentials

_REQUIRED_ENV = {
    "FLASK_ENV": "production",
    "SECRET_KEY": "production-secret-key-with-enough-length",
    "RATELIMIT_STORAGE_URI": "redis://:password@redis:6379/0",
    "BASE_URL": "https://ipm.example.test",
    "SCHEDULER_ENABLED": "0",
}


def _production_app(**overrides):
    app = Flask(__name__)
    app.config.update(
        CONFIG_NAME="production",
        SECRET_KEY=_REQUIRED_ENV["SECRET_KEY"],
        RATELIMIT_STORAGE_URI=_REQUIRED_ENV["RATELIMIT_STORAGE_URI"],
        BASE_URL=_REQUIRED_ENV["BASE_URL"],
        RATELIMIT_REQUIRE_SHARED_STORAGE=True,
        POLICY_RAW_SQL_GUARD_MODE="enforce",
    )
    app.config.update(overrides)
    return app


def test_production_guard_requires_explicit_env(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    for key in _REQUIRED_ENV:
        if key != "FLASK_ENV":
            monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="Missing required production environment variables"):
        _guard_production_required_security_config(_production_app())


def test_production_guard_allows_shared_ratelimit_and_enforced_sql_guard(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    _guard_production_required_security_config(_production_app())


def test_production_guard_rejects_memory_ratelimit(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "memory://")

    with pytest.raises(RuntimeError, match="shared storage"):
        _guard_production_required_security_config(
            _production_app(RATELIMIT_STORAGE_URI="memory://")
        )


def test_production_guard_rejects_report_only_raw_sql_guard(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match="POLICY_RAW_SQL_GUARD_MODE=enforce"):
        _guard_production_required_security_config(
            _production_app(POLICY_RAW_SQL_GUARD_MODE="report")
        )


def test_configure_image_safety_applies_pillow_limits(monkeypatch):
    from PIL import Image

    app = Flask(__name__)
    app.config.update(
        IMAGE_MAX_PIXELS="12345",
        IMAGE_STRICT_DECOMPRESSION_BOMB=True,
    )
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS, raising=False)

    with warnings.catch_warnings():
        _configure_image_safety(app)

        assert Image.MAX_IMAGE_PIXELS == 12345
        with pytest.raises(Image.DecompressionBombWarning):
            warnings.warn("oversized image", Image.DecompressionBombWarning)


def test_security_health_masks_ratelimit_uri_credentials():
    masked = _mask_uri_credentials("redis://:super-secret@redis:6379/0")

    assert masked == "redis://:***@redis:6379/0"
    assert "super-secret" not in masked
