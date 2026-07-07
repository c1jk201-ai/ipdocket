from __future__ import annotations

import zipfile
from pathlib import Path


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_backup_full_zip_includes_upload_folder_and_attachments(app, tmp_path, monkeypatch):
    from app.blueprints.billing_invoices.routes import admin as admin_routes

    backup_dir = tmp_path / "backups"
    attachments_dir = tmp_path / "attachments"
    uploads_dir = tmp_path / "uploads"

    _write_file(attachments_dir / "invoice_1" / "legacy.txt", b"legacy-attachment")
    _write_file(uploads_dir / "emails" / "raw.eml", b"raw-eml")
    _write_file(uploads_dir / "matter" / "sample.pdf", b"pdf-data")

    with app.app_context():
        monkeypatch.setitem(app.config, "BACKUP_DIR", str(backup_dir))
        monkeypatch.setitem(app.config, "ATTACHMENTS_DIR", str(attachments_dir))
        monkeypatch.setitem(app.config, "UPLOAD_FOLDER", str(uploads_dir))
        zip_path = admin_routes._zip_attachments("20260212010101")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())

    assert "attachments/invoice_1/legacy.txt" in names
    assert "uploads/emails/raw.eml" in names
    assert "uploads/matter/sample.pdf" in names
