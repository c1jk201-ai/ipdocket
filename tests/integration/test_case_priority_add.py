from __future__ import annotations


def test_priority_add_saves_identifier_and_event(app, db_session, admin_client, sample_matter):
    from app.models.ip_records import MatterEvent, MatterIdentifier

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    resp = admin_client.post(
        f"/case/{matter_id}/priority/add",
        data={
            "priority_no": "10-2024-1234567",
            "claim_date": "2024-03-10",
            "country": "US",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert f"/case/{matter_id}#sec-priority" in (resp.headers.get("Location") or "")

    id_rows = MatterIdentifier.query.filter_by(
        matter_id=str(matter_id), id_type="Priority"
    ).all()
    assert len(id_rows) == 1
    assert (id_rows[0].id_value or "").strip() == "10-2024-1234567"
    assert (id_rows[0].country or "").strip() == "US"

    event = MatterEvent.query.filter_by(
        matter_id=str(matter_id), event_key="PRIORITY_DATE"
    ).first()
    assert event is not None
    assert (event.event_at or "").strip() == "2024-03-10"


def test_priority_add_dedupes_normalized_number(app, db_session, admin_client, sample_matter):
    from app.models.ip_records import MatterIdentifier

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    db_session.add(
        MatterIdentifier(
            mid_id="mi-prio-existing",
            matter_id=str(matter_id),
            id_type="Text",
            id_value="10-2024-1234567",
        )
    )
    db_session.commit()

    resp = admin_client.post(
        f"/case/{matter_id}/priority/add",
        data={"priority_no": "1020241234567"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    id_rows = MatterIdentifier.query.filter_by(
        matter_id=str(matter_id), id_type="Text"
    ).all()
    assert len(id_rows) == 1
    priority_rows = MatterIdentifier.query.filter_by(
        matter_id=str(matter_id), id_type="Priority"
    ).all()
    assert priority_rows == []
