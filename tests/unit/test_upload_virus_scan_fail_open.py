from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest


def test_scan_upload_path_rejects_positive_detection_even_when_fail_open(
    app, tmp_path, monkeypatch
):
    from app.services.uploads.intake_security import UploadSecurityError, scan_upload_path

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"pdf")
    monkeypatch.delenv("TESTING", raising=False)
    app.config["UPLOAD_VIRUS_SCAN_COMMAND"] = "scanner {path}"
    app.config["UPLOAD_VIRUS_SCAN_FAIL_OPEN"] = True

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["scanner", str(sample)],
            returncode=1,
            stdout="Eicar-Test-Signature FOUND",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(UploadSecurityError, match="virus_scan_rejected"):
        scan_upload_path(sample, filename="sample.pdf")


def test_async_file_asset_scan_fail_open_timeout_marks_clean(
    app, db_session, tmp_path, monkeypatch
):
    from app.models.assets import FileAsset
    from app.services.storage import file_asset_scan_service as scan_service

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"pdf")
    asset = FileAsset(
        file_asset_id="scan-fail-open-timeout",
        file_path=str(sample),
        original_name="sample.pdf",
        storage_type="local",
        virus_scan_status=scan_service.SCAN_STATUS_PENDING,
        is_deleted=False,
    )
    db_session.add(asset)
    db_session.commit()

    @contextmanager
    def fake_scan_target_path(*_args, **_kwargs):
        yield sample

    from app.services.storage import file_asset_scan_queue

    monkeypatch.setattr(scan_service, "virus_scan_enabled", lambda: True)
    monkeypatch.setattr(scan_service, "virus_scan_mode", lambda: "async")
    monkeypatch.setattr(file_asset_scan_queue, "virus_scan_enabled", lambda: True)
    monkeypatch.setattr(file_asset_scan_queue, "virus_scan_mode", lambda: "async")
    monkeypatch.setattr(scan_service, "_scan_target_path", fake_scan_target_path)
    monkeypatch.setattr(file_asset_scan_queue, "_scan_target_path", fake_scan_target_path)
    monkeypatch.setattr(
        scan_service,
        "scan_upload_path",
        lambda *_args, **_kwargs: {
            "status": "timeout",
            "timeout_seconds": 10,
            "fail_open": True,
        },
    )
    monkeypatch.setattr(
        file_asset_scan_queue,
        "scan_upload_path",
        lambda *_args, **_kwargs: {
            "status": "timeout",
            "timeout_seconds": 10,
            "fail_open": True,
        },
    )

    result = scan_service.run_file_asset_virus_scan(asset.file_asset_id)

    db_session.expire_all()
    refreshed = db_session.get(FileAsset, asset.file_asset_id)
    assert result == {
        "status": scan_service.SCAN_STATUS_CLEAN,
        "fail_open": True,
        "scanner_status": "timeout",
    }
    assert refreshed.virus_scan_status == scan_service.SCAN_STATUS_CLEAN
    assert "fail_open:status=timeout" in refreshed.virus_scan_error
