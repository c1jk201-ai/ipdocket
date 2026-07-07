from __future__ import annotations

from pathlib import Path

from app.models.assets import FileAsset, MatterFileAsset
from app.services.storage.file_asset_storage_audit import audit_file_asset_storage


def _file_asset(file_asset_id: str, file_path: str, byte_size: int) -> FileAsset:
    return FileAsset(
        file_asset_id=file_asset_id,
        storage_type="local",
        file_path=file_path,
        original_name=f"{file_asset_id}.txt",
        sha256=f"sha-{file_asset_id}",
        byte_size=byte_size,
        mime_type="text/plain",
        created_at="2026-01-01T00:00:00",
    )


def _write(root: Path, rel_path: str, data: bytes) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sample_paths(section: dict) -> set[str]:
    return {str(item.get("path") or "") for item in section.get("sample") or []}


def test_audit_reports_missing_orphan_and_untracked_disk_files(
    app, db_session, tmp_path: Path
) -> None:
    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    _write(tmp_path, "known/linked.txt", b"linked")
    _write(tmp_path, "known/orphan.txt", b"orphan")
    _write(tmp_path, "loose/untracked.txt", b"loose")

    db_session.add_all(
        [
            _file_asset("fa-linked", "known/linked.txt", 6),
            _file_asset("fa-orphan", "known/orphan.txt", 6),
            _file_asset("fa-missing", "known/missing.txt", 7),
            _file_asset("fa-invalid", "../outside.txt", 8),
            MatterFileAsset(
                matter_file_id="mfa-linked",
                matter_id="matter-1",
                file_asset_id="fa-linked",
                role="application",
                created_at="2026-01-01T00:00:00",
            ),
        ]
    )
    db_session.commit()

    report = audit_file_asset_storage(upload_root=tmp_path, sample_limit=10)

    assert report["db_file_assets"]["count"] == 4
    assert report["db_file_assets"]["valid_local_paths"] == 3
    assert report["existing_db_files"]["count"] == 2
    assert report["missing_db_files"]["count"] == 1
    assert _sample_paths(report["missing_db_files"]) == {"known/missing.txt"}
    assert report["invalid_db_paths"]["count"] == 1

    assert report["orphan_file_assets"]["count"] == 2
    assert report["orphan_file_assets"]["existing_files"] == 1
    assert _sample_paths(report["orphan_file_assets"]) == {
        "known/orphan.txt",
        "known/missing.txt",
    }

    assert report["disk_files"]["scanned"] == 3
    assert report["untracked_disk_files"]["count"] == 1
    assert _sample_paths(report["untracked_disk_files"]) == {"loose/untracked.txt"}


def test_audit_distinguishes_soft_deleted_links_from_active_links(
    app, db_session, tmp_path: Path
) -> None:
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    _write(tmp_path, "known/soft-linked.txt", b"soft")

    db_session.add_all(
        [
            _file_asset("fa-soft-linked", "known/soft-linked.txt", 4),
            MatterFileAsset(
                matter_file_id="mfa-soft-linked",
                matter_id="matter-1",
                file_asset_id="fa-soft-linked",
                role="application",
                created_at="2026-01-01T00:00:00",
                is_deleted=True,
            ),
        ]
    )
    db_session.commit()

    report = audit_file_asset_storage(upload_root=tmp_path, sample_limit=10)

    assert report["referenced_file_assets"]["by_any_link"] == 1
    assert report["referenced_file_assets"]["by_active_link"] == 0
    assert report["orphan_file_assets"]["count"] == 0
