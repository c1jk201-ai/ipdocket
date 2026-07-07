import uuid

from app.utils.policy_sql import policy_text as text


def test_all_responses_excludes_email_assets_even_if_comm_type_is_R(
    app, client, db_session, monkeypatch
):
    from app.models.assets import FileAsset
    from app.models.ip_records import Matter

    mid = uuid.uuid4().hex
    matter = Matter(
        matter_id=mid,
        our_ref="25TD0334US",
        right_name="Text Text",
        status_red="Text",
        status_blue="Text",
    )
    db_session.add(matter)
    db_session.commit()

    comm_id = uuid.uuid4().hex
    db_session.execute(
        text(
            """
            INSERT INTO communication(comm_id, matter_id, comm_type, note, received_date)
            VALUES(:cid, :mid, 'R', :note, :d)
            """
        ),
        {"cid": comm_id, "mid": mid, "note": "mail.eml", "d": "2026-01-25"},
    )

    fa_id = uuid.uuid4().hex
    db_session.add(
        FileAsset(
            file_asset_id=fa_id,
            original_name="mail.eml",
            mime_type="message/rfc822",
            file_path="emails/mail.eml",
            byte_size=123,
            storage_type="local",
            created_at="2026-01-25T00:00:00",
        )
    )
    db_session.execute(
        text(
            """
            INSERT INTO communication_file_asset(comm_file_id, comm_id, file_asset_id, role, description)
            VALUES(:cfid, :cid, :fid, 'upload', 'test')
            """
        ),
        {"cfid": uuid.uuid4().hex, "cid": comm_id, "fid": fa_id},
    )
    db_session.commit()

    monkeypatch.setitem(app.config, "LOGIN_DISABLED", True)

    r = client.get("/case/all-responses")
    assert r.status_code == 200
    html = r.data.decode("utf-8", errors="ignore")
    assert "mail.eml" not in html

    r2 = client.get("/case/all-letters")
    assert r2.status_code == 200
    html2 = r2.data.decode("utf-8", errors="ignore")
    assert "mail.eml" in html2


def test_notice_view_redirects_when_migrated_to_response(app, client, admin_user, db_session):
    from app.models.ip_records import Matter

    mid = uuid.uuid4().hex
    matter = Matter(
        matter_id=mid,
        our_ref="25TD0001US",
        right_name="Text Text",
        status_red="Text",
        status_blue="Text",
    )
    db_session.add(matter)
    db_session.commit()

    comm_id = uuid.uuid4().hex
    db_session.execute(
        text(
            """
            INSERT INTO communication(comm_id, matter_id, comm_type, note, received_date)
            VALUES(:cid, :mid, 'R', :note, :d)
            """
        ),
        {"cid": comm_id, "mid": mid, "note": "Text(Text): Text", "d": "2026-01-25"},
    )

    oa_id = uuid.uuid4().hex
    db_session.execute(
        text(
            """
            INSERT INTO office_action(oa_id, matter_id, doc_name, received_date, raw_id)
            VALUES(:oid, :mid, :doc_name, :d, :raw_id)
            """
        ),
        {
            "oid": oa_id,
            "mid": mid,
            "doc_name": "Text(Text)",
            "d": "2026-01-25",
            "raw_id": f"MIGRATED_TO_COMM:{comm_id}",
        },
    )
    db_session.commit()

    with client.session_transaction() as session:
        session["_user_id"] = getattr(admin_user, "_test_id", None) or admin_user.id
        session["_fresh"] = True
    resp = client.get(f"/case/{mid}/history/notice/{oa_id}/viewNewpopup=1")
    assert resp.status_code in (301, 302, 303, 307, 308)
    loc = resp.headers.get("Location") or ""
    assert f"/case/{mid}/history/letter/{comm_id}/view" in loc
