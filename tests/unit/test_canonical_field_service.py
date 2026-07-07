from __future__ import annotations

from app.services.case import canonical_field_service


def test_load_canonical_config_uses_app_data_file() -> None:
    canonical_field_service.load_canonical_config.cache_clear()
    try:
        config = canonical_field_service.load_canonical_config()
    finally:
        canonical_field_service.load_canonical_config.cache_clear()

    assert config["attorney"]
    assert any(target.get("namespace") == "basic" for target in config["attorney"])
