import uuid


def test_case_detail_history_applies_saved_merge_groups(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.communication import Communication, OfficeAction
    from app.models.matter import MatterCustomField

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    comm_id = uuid.uuid4().hex
    other_comm_id = uuid.uuid4().hex
    group_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="Text Text",
            notified_date="2026-02-20",
            received_date="2026-02-20",
        )
    )
    db_session.add(
        Communication(
            comm_id=comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-21",
            note="Text Text",
        )
    )
    db_session.add(
        Communication(
            comm_id=other_comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-22",
            note="Text Text",
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=mid,
            namespace="history_merge_groups",
            data={
                "groups": [
                    {
                        "group_id": group_id,
                        "title": "Text Text Text",
                        "member_keys": [
                            f"letter:{comm_id}",
                            f"notice:{oa_id}",
                            f"letter:{uuid.uuid4().hex}",
                        ],
                    }
                ]
            },
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    history_rows = out.get("history_rows") or []
    row_map = {str((r or {}).get("row_key") or ""): r for r in history_rows}

    assert row_map[f"letter:{comm_id}"].get("merge_group_id") == group_id
    assert row_map[f"notice:{oa_id}"].get("merge_group_id") == group_id
    assert not row_map[f"letter:{other_comm_id}"].get("merge_group_id")

    groups = out.get("history_merge_groups") or []
    assert len(groups) == 1
    summary = groups[0]
    assert summary.get("group_id") == group_id
    assert summary.get("title") == "Text Text Text"
    assert set(summary.get("member_keys") or []) == {f"letter:{comm_id}", f"notice:{oa_id}"}
    assert int(summary.get("member_count") or 0) == 2
