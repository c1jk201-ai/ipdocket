import uuid


def test_case_history_merge_save_update_delete(admin_client, db_session, sample_matter):
    from app.models.communication import Communication, OfficeAction
    from app.models.matter import MatterCustomField

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    comm_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="Text Text Text",
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
        f"/case/{mid}/history/merge",
        json={
            "title": "Text Text Text",
            "row_keys": [
                f"letter:{comm_id}",
                f"notice:{oa_id}",
                "invalid:abc",
                f"notice:{uuid.uuid4().hex}",
            ],
        },
    )
    assert save_resp.status_code == 200
    save_payload = save_resp.get_json() or {}
    assert save_payload.get("ok") is True
    assert save_payload.get("group_count") == 1
    assert save_payload.get("selected_count") == 2

    row = MatterCustomField.query.filter_by(matter_id=mid, namespace="history_merge_groups").first()
    assert row is not None
    groups = (row.data or {}).get("groups") or []
    assert len(groups) == 1
    group = groups[0]
    assert group.get("title") == "Text Text Text"
    assert group.get("member_keys") == [f"letter:{comm_id}", f"notice:{oa_id}"]

    group_id = str(group.get("group_id") or "")
    assert group_id

    patch_resp = admin_client.patch(
        f"/case/{mid}/history/merge/{group_id}",
        json={"title": "Text Text Text", "collapsed": False},
    )
    assert patch_resp.status_code == 200
    patch_payload = patch_resp.get_json() or {}
    assert patch_payload.get("ok") is True

    row_after_patch = MatterCustomField.query.filter_by(
        matter_id=mid, namespace="history_merge_groups"
    ).first()
    assert row_after_patch is not None
    groups_after_patch = (row_after_patch.data or {}).get("groups") or []
    assert groups_after_patch
    assert groups_after_patch[0].get("title") == "Text Text Text"
    assert groups_after_patch[0].get("collapsed") is False

    delete_resp = admin_client.delete(f"/case/{mid}/history/merge/{group_id}")
    assert delete_resp.status_code == 200
    delete_payload = delete_resp.get_json() or {}
    assert delete_payload.get("ok") is True
    assert delete_payload.get("group_count") == 0

    row_after_delete = MatterCustomField.query.filter_by(
        matter_id=mid, namespace="history_merge_groups"
    ).first()
    assert row_after_delete is None
