from pathlib import Path


def test_compose_uses_env_overridable_storage_defaults() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert 'UPLOAD_FOLDER: "${UPLOAD_FOLDER:-/app/data/uploads}"' in compose
    assert 'ATTACHMENTS_DIR: "${ATTACHMENTS_DIR:-/app/data/attachments}"' in compose
    assert (
        'CLIENT_ATTACHMENTS_DIR: "${CLIENT_ATTACHMENTS_DIR:-/app/data/uploads/clients}"' in compose
    )


def test_config_allows_attachments_dir_env_override() -> None:
    config_src = Path("app_config/settings/storage.py").read_text(encoding="utf-8")

    assert '_env_path_with_default(\n        "ATTACHMENTS_DIR",' in config_src
    assert 'os.path.join(BaseSettings.BASE_DIR, "data", "attachments")' in config_src


def test_startup_path_checks_include_attachments_dir() -> None:
    startup_src = Path("app/utils/db_startup.py").read_text(encoding="utf-8")

    assert '"attachments_dir": (app.config.get("ATTACHMENTS_DIR") or "").strip()' in startup_src
