from __future__ import annotations

from pathlib import Path

from app.utils.policy_sql import policy_text as text


def _reset_file_asset_service_singleton() -> None:
    import app.services.storage.file_asset_service as file_asset_service

    file_asset_service._service = None


def test_file_delete_queue_deletes_existing_file(app, db_session, tmp_path: Path):
    _reset_file_asset_service_singleton()
    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    from app.services.files.file_delete_queue import (
        drain_file_delete_queue,
        enqueue_file_delete_retry,
    )

    db_session.execute(text("DELETE FROM file_delete_queue"))
    db_session.commit()

    rel_path = "queue_test/test.txt"
    abs_path = tmp_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("hello", encoding="utf-8")
    assert abs_path.exists()

    ok = enqueue_file_delete_retry(file_path=rel_path, file_asset_id="fa_test", error="locked")
    assert ok is True

    result = drain_file_delete_queue(limit=10)
    assert result["picked"] == 1
    assert result["deleted"] == 1
    assert abs_path.exists() is False

    remaining = db_session.execute(text("SELECT COUNT(*) FROM file_delete_queue")).scalar()
    assert int(remaining or 0) == 0


def test_file_delete_queue_removes_row_when_file_missing(app, db_session, tmp_path: Path):
    _reset_file_asset_service_singleton()
    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    from app.services.files.file_delete_queue import (
        drain_file_delete_queue,
        enqueue_file_delete_retry,
    )

    db_session.execute(text("DELETE FROM file_delete_queue"))
    db_session.commit()

    rel_path = "queue_test/missing.txt"
    assert (tmp_path / rel_path).exists() is False

    ok = enqueue_file_delete_retry(file_path=rel_path, file_asset_id="fa_test", error="missing")
    assert ok is True

    result = drain_file_delete_queue(limit=10)
    assert result["picked"] == 1
    assert result["deleted"] == 1

    remaining = db_session.execute(text("SELECT COUNT(*) FROM file_delete_queue")).scalar()
    assert int(remaining or 0) == 0


def test_file_delete_queue_marks_unsafe_path_failed(app, db_session, tmp_path: Path):
    _reset_file_asset_service_singleton()
    app.config["UPLOAD_FOLDER"] = str(tmp_path)

    from app.services.files.file_delete_queue import (
        drain_file_delete_queue,
        enqueue_file_delete_retry,
    )

    db_session.execute(text("DELETE FROM file_delete_queue"))
    db_session.commit()

    ok = enqueue_file_delete_retry(file_path="../evil.txt", file_asset_id="fa_test", error="unsafe")
    assert ok is True

    result = drain_file_delete_queue(limit=10)
    assert result["picked"] == 1
    assert result["failed"] == 1

    row = db_session.execute(
        text("SELECT attempts, last_error FROM file_delete_queue LIMIT 1")
    ).first()
    assert row is not None
    assert int(row[0] or 0) >= 10
    assert "unsafe_path" in str(row[1] or "")
