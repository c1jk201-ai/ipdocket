from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage


def _reset_and_seed_billing_rows(app) -> None:
    from app.blueprints.billing_invoices.db import get_db, init_db

    with app.app_context():
        init_db()
        conn = get_db()
        for table in (
            "invoice_attachments",
            "client_attachments",
            "invoices",
            "clients",
            "business_profile",
        ):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                conn.rollback()
        conn.execute(
            "INSERT INTO business_profile (id, name, currency, vat_rate, next_invoice_no) "
            "VALUES (1, 'BP1', 'USD', 10.0, 1)"
        )
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Client A')")
        conn.execute(
            """
            INSERT INTO invoices (
                id, client_id, business_profile_id, number, issue_date, due_date,
                status, billing_status, payment_status, currency, total, total_minor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "INV-SEC-001",
                "2026-05-01",
                "2026-05-31",
                "draft",
                "draft",
                "unpaid",
                "USD",
                0,
                0,
            ),
        )
        conn.commit()
        conn.close()


def _count_rows(app, table: str) -> int:
    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()


def _stored_files(root) -> list:
    return [path for path in root.glob("**/*") if path.is_file()]


def test_invoice_attachment_rejects_pdf_extension_with_non_pdf_content(
    admin_client, app, tmp_path, clean_legacy_invoice_db
):
    _reset_and_seed_billing_rows(app)
    app.config["ATTACHMENTS_DIR"] = str(tmp_path / "invoice_attachments")
    app.config["ALLOWED_ATTACHMENT_EXTENSIONS"] = {"pdf", "png", "jpg", "jpeg", "gif", "zip"}

    resp = admin_client.post(
        "/accounting/invoice-system/invoices/1/attachments/upload",
        data={"file": (BytesIO(b"not a pdf"), "spoof.pdf")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert _count_rows(app, "invoice_attachments") == 0
    assert not _stored_files(tmp_path / "invoice_attachments")


def test_billing_client_attachment_rejects_pdf_extension_with_non_pdf_content(
    admin_client, app, tmp_path, clean_legacy_invoice_db
):
    _reset_and_seed_billing_rows(app)
    app.config["CLIENT_ATTACHMENTS_DIR"] = str(tmp_path / "client_attachments")
    app.config["ALLOWED_ATTACHMENT_EXTENSIONS"] = {"pdf", "png", "jpg", "jpeg", "gif", "zip"}

    resp = admin_client.post(
        "/accounting/invoice-system/clients/1/attachments/upload",
        data={"file": (BytesIO(b"not a pdf"), "spoof.pdf")},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert _count_rows(app, "client_attachments") == 0
    assert not _stored_files(tmp_path / "client_attachments")


def test_billing_client_attachment_download_does_not_fallback_to_latest_file(
    admin_client, app, tmp_path, clean_legacy_invoice_db
):
    _reset_and_seed_billing_rows(app)
    app.config["CLIENT_ATTACHMENTS_DIR"] = str(tmp_path / "client_attachments")
    app.config["ALLOWED_ATTACHMENT_EXTENSIONS"] = {"pdf", "png", "jpg", "jpeg", "gif", "zip"}

    attachment_dir = tmp_path / "client_attachments" / "client_1"
    attachment_dir.mkdir(parents=True)
    (attachment_dir / "real.pdf").write_bytes(b"%PDF-1.4\nreal attachment")

    from app.blueprints.billing_invoices.db import get_db

    with app.app_context():
        conn = get_db()
        conn.execute(
            """
            INSERT INTO client_attachments
                (id, client_id, original_name, stored_name, content_type, size)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (10, 1, "real.pdf", "real.pdf", "application/pdf", 24),
        )
        conn.commit()
        conn.close()

    resp = admin_client.get("/accounting/invoice-system/clients/1/attachments/999/download")

    assert resp.status_code == 404


def test_crm_client_attachment_service_rejects_pdf_extension_with_non_pdf_content(
    app, tmp_path, clean_legacy_invoice_db
):
    _reset_and_seed_billing_rows(app)
    app.config["CLIENT_ATTACHMENTS_DIR"] = str(tmp_path / "crm_client_attachments")
    app.config["ALLOWED_ATTACHMENT_EXTENSIONS"] = {"pdf", "png", "jpg", "jpeg", "gif", "zip"}

    from app.services.client.client_attachment_service import save_client_attachment_for_crm_client
    from app.services.uploads.intake_security import UploadSecurityError

    fake_crm_client = SimpleNamespace(id=1, external_invoice_client_id=None)
    upload = FileStorage(
        stream=BytesIO(b"not a pdf"),
        filename="spoof.pdf",
        content_type="application/pdf",
    )

    with app.app_context(), pytest.raises(UploadSecurityError):
        save_client_attachment_for_crm_client(fake_crm_client, upload, uploaded_by=None)

    assert _count_rows(app, "client_attachments") == 0
    assert not _stored_files(tmp_path / "crm_client_attachments")
