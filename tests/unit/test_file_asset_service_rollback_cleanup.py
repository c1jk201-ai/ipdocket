import io
from pathlib import Path

from werkzeug.datastructures import FileStorage


def test_stage_upload_rollback_deletes_physical_file(app, db_session, tmp_path):
    """
    Regression: FileAssetService.stage_upload persists physical files before DB commit.
    A rollback should not leave orphaned objects behind.
    """
    from app.services.storage.file_asset_service import FileAssetService

    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    svc = FileAssetService(upload_root=str(tmp_path))

    staged = svc.stage_upload(
        FileStorage(stream=io.BytesIO(b"hello"), filename="a.txt", content_type="text/plain"),
        subdir="tests",
    )
    abs_path = (Path(tmp_path) / staged.rel_path).resolve()
    assert abs_path.exists()

    db_session.rollback()
    assert not abs_path.exists()
