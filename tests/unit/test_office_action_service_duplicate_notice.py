from __future__ import annotations

from app.utils.policy_sql import policy_text as text


def _mark_as_dom_patent(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.right_group = "DOM"
    matter.matter_type = "PATENT"
    db_session.add(matter)
    db_session.commit()
    return matter


def _sample_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def _stage_notice_file(app, monkeypatch, tmp_path, *, matter_id: str, filename: str = "notice.pdf"):
    from app.services.storage import file_asset_service as fas

    monkeypatch.setitem(app.config, "UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setattr(fas, "_service", None)
    return fas.get_file_asset_service().stage_bytes(
        _sample_pdf_bytes(),
        filename=filename,
        subdir=f"matter/{matter_id}/notices",
        mime_type="application/pdf",
    )


def test_office_action_create_blocks_duplicate_notice_attachment(
    app, db_session, sample_matter, monkeypatch, tmp_path
):
    from app.services.history.office_action_service import (
        OfficeActionData,
        get_office_action_service,
    )

    matter = _mark_as_dom_patent(db_session, sample_matter)
    matter_id = str(matter.matter_id)
    oa_service = get_office_action_service()

    first_stage = _stage_notice_file(
        app,
        monkeypatch,
        tmp_path,
        matter_id=matter_id,
    )
    created = oa_service.create(
        OfficeActionData(
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-01",
            due_date="2026-03-10",
        ),
        staged_files=[first_stage],
    )
    assert created.success is True
    db_session.commit()

    duplicate_stage = _stage_notice_file(
        app,
        monkeypatch,
        tmp_path,
        matter_id=matter_id,
    )
    assert duplicate_stage.is_new is False

    duplicate = oa_service.create(
        OfficeActionData(
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-02",
            due_date="2026-03-10",
        ),
        staged_files=[duplicate_stage],
    )

    assert duplicate.success is False
    assert any("notice.pdf" in err and "2026-03-10" in err for err in duplicate.errors), duplicate.errors

    count = db_session.execute(
        text("SELECT COUNT(*) FROM office_action WHERE matter_id = :mid"),
        {"mid": matter_id},
    ).scalar()
    assert int(count or 0) == 1


def test_office_action_update_blocks_attachment_used_by_another_notice(
    app, db_session, sample_matter, monkeypatch, tmp_path
):
    from app.services.history.office_action_service import (
        OfficeActionData,
        get_office_action_service,
    )

    matter = _mark_as_dom_patent(db_session, sample_matter)
    matter_id = str(matter.matter_id)
    oa_service = get_office_action_service()

    first_stage = _stage_notice_file(
        app,
        monkeypatch,
        tmp_path,
        matter_id=matter_id,
    )
    first = oa_service.create(
        OfficeActionData(
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-01",
            due_date="2026-03-10",
        ),
        staged_files=[first_stage],
    )
    assert first.success is True

    second = oa_service.create(
        OfficeActionData(
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-03",
        ),
    )
    assert second.success is True
    assert second.oa_id
    db_session.commit()

    duplicate_stage = _stage_notice_file(
        app,
        monkeypatch,
        tmp_path,
        matter_id=matter_id,
    )
    assert duplicate_stage.is_new is False

    updated = oa_service.update(
        str(second.oa_id),
        OfficeActionData(
            matter_id=matter_id,
            doc_name="Text",
            received_date="2026-02-03",
        ),
        staged_files=[duplicate_stage],
    )

    assert updated.success is False
    assert any("notice.pdf" in err and "2026-03-10" in err for err in updated.errors), updated.errors

    attachment_count = db_session.execute(
        text("SELECT COUNT(*) FROM office_action_file_asset WHERE oa_id = :oid"),
        {"oid": str(second.oa_id)},
    ).scalar()
    assert int(attachment_count or 0) == 0
