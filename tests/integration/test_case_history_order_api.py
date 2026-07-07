import uuid


def test_case_history_order_save_and_reset(admin_client, db_session, sample_matter):
    from app.models.communication import Communication, OfficeAction
    from app.models.matter import MatterCustomField

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    comm_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="OA Text Text",
            received_date="2026-02-10",
        )
    )
    db_session.add(
        Communication(
            comm_id=comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-11",
            note="Text Text Text",
        )
    )
    db_session.commit()

    save_resp = admin_client.post(
        f"/case/{mid}/history/order",
        json={
            "order": [
                f"letter:{comm_id}",
                f"notice:{oa_id}",
                "invalid:xxx",
                f"notice:{uuid.uuid4().hex}",
            ]
        },
    )
    assert save_resp.status_code == 200
    payload = save_resp.get_json() or {}
    assert payload.get("ok") is True
    assert payload.get("order_count") == 2

    row = MatterCustomField.query.filter_by(matter_id=mid, namespace="history_order").first()
    assert row is not None
    assert (row.data or {}).get("order") == [f"letter:{comm_id}", f"notice:{oa_id}"]

    reset_resp = admin_client.delete(f"/case/{mid}/history/order")
    assert reset_resp.status_code == 200
    reset_payload = reset_resp.get_json() or {}
    assert reset_payload.get("ok") is True

    row_after = MatterCustomField.query.filter_by(matter_id=mid, namespace="history_order").first()
    assert row_after is None
