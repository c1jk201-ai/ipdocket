import uuid

from app.utils.policy_sql import policy_text as text


def test_history_notice_edit_form_posts_to_edit_and_does_not_create_notice(
    app, client, admin_user, db_session, tmp_path, monkeypatch
):
    from app.models.ip_records import Matter
    from app.services.storage.file_asset_service import get_file_asset_service

    monkeypatch.setitem(app.config, "UPLOAD_FOLDER", str(tmp_path))
    monkeypatch.setitem(app.config, "POLICY_ENGINE_ENABLED", False)

    admin_id = getattr(admin_user, "_test_id", None) or admin_user.id
    with client.session_transaction() as session:
        session["_user_id"] = admin_id
        session["_fresh"] = True

    mid = uuid.uuid4().hex
    matter = Matter(
        matter_id=mid,
        our_ref="26PDNOTICEUS",
        right_name="Text Text Text",
        status_red="Text",
        status_blue="Text",
    )
    db_session.add(matter)
    db_session.commit()

    oa_id = uuid.uuid4().hex
    db_session.execute(
        text(
            """
            INSERT INTO office_action(
                oa_id, matter_id, doc_name, received_date, notified_date, due_date
            )
            VALUES(:oid, :mid, 'Text', '2026-04-14', '2025-12-24', '2026-05-24')
            """
        ),
        {"oid": oa_id, "mid": mid},
    )

    staged = get_file_asset_service().stage_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="notice.pdf",
        subdir=f"matter/{mid}/notices",
        mime_type="application/pdf",
    )
    db_session.execute(
        text(
            """
            INSERT INTO office_action_file_asset(oa_file_id, oa_id, file_asset_id, role, description)
            VALUES(:id, :oid, :fid, 'upload', 'test')
            """
        ),
        {"id": uuid.uuid4().hex, "oid": oa_id, "fid": staged.file_asset_id},
    )
    db_session.commit()

    edit_url = f"/case/{mid}/history/notice/{oa_id}/edit?popup=1"
    edit_resp = client.get(edit_url)
    assert edit_resp.status_code == 200

    html = edit_resp.data.decode("utf-8")
    assert f'action="{edit_url}"' in html
    assert 'name="doc_name"' in html
    assert 'value="Text"' in html
    assert 'value="2026-04-14"' in html
    assert f'name="remove_files" value="{staged.file_asset_id}"' in html

    post_resp = client.post(
        edit_url,
        data={
            "doc_name": "Text",
            "received_date": "2026-04-14",
            "notified_date": "2025-12-24",
            "due_date": "2026-05-30",
            "extended_due_date": "",
            "done_date": "",
            "examiner": "Text Text",
            "remove_files": staged.file_asset_id,
        },
    )
    assert post_resp.status_code == 200

    notice_count = db_session.execute(
        text("SELECT COUNT(*) FROM office_action WHERE matter_id = :mid"),
        {"mid": mid},
    ).scalar()
    assert int(notice_count or 0) == 1

    row = (
        db_session.execute(
            text(
                """
                SELECT due_date, examiner
                FROM office_action
                WHERE oa_id = :oid
                """
            ),
            {"oid": oa_id},
        )
        .mappings()
        .one()
    )
    assert row["due_date"] == "2026-05-30"
    assert row["examiner"] == "Text Text"

    attachment_count = db_session.execute(
        text("SELECT COUNT(*) FROM office_action_file_asset WHERE oa_id = :oid"),
        {"oid": oa_id},
    ).scalar()
    assert int(attachment_count or 0) == 0
