from __future__ import annotations

import uuid


def test_case_file_download_sanitizes_header_filename(
    app, authenticated_client, sample_matter, db_session, tmp_path
):
    from app.models.assets import FileAsset, MatterFileAsset

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    file_asset_id = uuid.uuid4().hex
    rel_path = "cases/bad-name.pdf"
    abs_path = tmp_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"test-pdf-bytes")

    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    db_session.add(
        FileAsset(
            file_asset_id=file_asset_id,
            file_path=rel_path,
            original_name="bad\r\nname.pdf",
            mime_type="application/pdf",
            storage_type="local",
        )
    )
    db_session.add(
        MatterFileAsset(
            matter_file_id=uuid.uuid4().hex,
            matter_id=matter_id,
            file_asset_id=file_asset_id,
            role="attachment",
        )
    )
    db_session.commit()

    response = authenticated_client.get(f"/case/{matter_id}/file/{file_asset_id}/download")

    assert response.status_code == 200
    assert response.data == b"test-pdf-bytes"

    content_disposition = response.headers.get("Content-Disposition", "")
    assert "\r" not in content_disposition
    assert "\n" not in content_disposition
    assert "bad__name.pdf" in content_disposition
