from app.ops.models import DiskSample


def test_admin_ops_disk_page_shows_empty_state_without_samples(admin_client):
    res = admin_client.get("/admin/ops/disk")

    assert res.status_code == 200, f"Response: {res.data}"
    html = res.get_data(as_text=True)

    assert "Disk Usage" in html
    assert "No disk samples yet." in html
    assert "disk_monitor" in html


def test_check_disk_and_alert_persists_disk_samples_for_admin_page(app, db_session, tmp_path):
    from app.services.ops.disk_monitor import check_disk_and_alert

    uploads_dir = tmp_path / "uploads"
    backups_dir = tmp_path / "backups"
    clients_dir = uploads_dir / "clients"

    app.config.update(
        {
            "DISK_MONITOR_ENABLED": True,
            "UPLOAD_FOLDER": str(uploads_dir),
            "BACKUP_DIR": str(backups_dir),
            "CLIENT_ATTACHMENTS_DIR": str(clients_dir),
            "DISK_ALERT_EMAILS": "",
            "ERROR_REPORT_ALERT_EMAILS": "",
        }
    )

    with app.app_context():
        result = check_disk_and_alert()
        rows = DiskSample.query.order_by(DiskSample.mount_label.asc()).all()

    assert result["enabled"] is True
    assert result["ok"] is True
    assert int(result.get("samples_written") or 0) >= 2
    assert {row.mount_label for row in rows} >= {"uploads", "backups"}
    assert all(row.total_bytes > 0 for row in rows)
