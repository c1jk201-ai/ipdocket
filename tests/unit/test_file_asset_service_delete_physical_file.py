from __future__ import annotations

from pathlib import Path


def test_delete_physical_file_respects_custom_upload_root(app, tmp_path: Path) -> None:
    from app.services.storage.file_asset_service import FileAssetService

    # Ensure the app's configured UPLOAD_FOLDER differs from the service's upload_root.
    configured_root = tmp_path / "data_uploads"
    configured_root.mkdir(parents=True, exist_ok=True)
    custom_root = tmp_path / "uploads" / "clients"
    custom_root.mkdir(parents=True, exist_ok=True)

    rel_path = "client_1/test.txt"
    abs_path = custom_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"hello")
    assert abs_path.exists()

    with app.app_context():
        app.config["UPLOAD_FOLDER"] = str(configured_root)
        service = FileAssetService(upload_root=custom_root)
        assert service.delete_physical_file(rel_path) is True

    assert not abs_path.exists()
