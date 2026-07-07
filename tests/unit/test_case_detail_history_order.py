import uuid


def test_case_detail_history_applies_saved_custom_order(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.communication import Communication, OfficeAction
    from app.models.matter import MatterCustomField

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    old_comm_id = uuid.uuid4().hex
    recent_comm_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="OA Text Text",
            received_date="2026-02-01",
        )
    )
    db_session.add(
        Communication(
            comm_id=old_comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-01-31",
            note="Text Text",
        )
    )
    db_session.add(
        Communication(
            comm_id=recent_comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-15",
            note="Text Text",
        )
    )
    db_session.add(
        MatterCustomField(
            matter_id=mid,
            namespace="history_order",
            data={
                "order": [
                    f"letter:{old_comm_id}",
                    f"notice:{oa_id}",
                    f"letter:{recent_comm_id}",
                ]
            },
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    keys = [str(r.get("row_key") or "") for r in (out.get("history_rows") or [])]

    assert keys[:3] == [f"letter:{old_comm_id}", f"notice:{oa_id}", f"letter:{recent_comm_id}"]


def test_case_detail_history_ignores_stale_partial_saved_order(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.communication import Communication, OfficeAction
    from app.models.matter import MatterCustomField

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    old_comm_id = uuid.uuid4().hex
    recent_comm_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="OA Text Text",
            notified_date="2026-02-01",
            received_date="2026-02-02",
        )
    )
    db_session.add(
        Communication(
            comm_id=old_comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-01-31",
            note="Text Text",
        )
    )
    db_session.add(
        Communication(
            comm_id=recent_comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-15",
            note="Text Text",
        )
    )
    # Stale order: does not include all current rows.
    db_session.add(
        MatterCustomField(
            matter_id=mid,
            namespace="history_order",
            data={"order": [f"letter:{old_comm_id}", f"notice:{oa_id}"]},
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    keys = [str(r.get("row_key") or "") for r in (out.get("history_rows") or [])]

    # Fallback to timeline sort (recent communication first).
    assert keys[0] == f"letter:{recent_comm_id}"
    assert f"notice:{oa_id}" in keys
    assert f"letter:{old_comm_id}" in keys


def test_case_detail_history_notice_prefers_notified_date_for_sort(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.communication import OfficeAction

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    older_notice_id = uuid.uuid4().hex
    newer_notice_id = uuid.uuid4().hex

    # Same received day (upload day), different notified_date.
    db_session.add(
        OfficeAction(
            oa_id=older_notice_id,
            matter_id=mid,
            doc_name="Text",
            received_date="2026-03-04",
            notified_date="2026-02-26",
        )
    )
    db_session.add(
        OfficeAction(
            oa_id=newer_notice_id,
            matter_id=mid,
            doc_name="Text",
            received_date="2026-03-04",
            notified_date="2026-02-27",
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    keys = [str(r.get("row_key") or "") for r in (out.get("history_rows") or [])]

    assert keys[:2] == [f"notice:{newer_notice_id}", f"notice:{older_notice_id}"]


def test_case_detail_history_tiebreaks_same_day_with_upload_timestamp(db_session, sample_matter):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.assets import FileAsset
    from app.models.communication import (
        Communication,
        CommunicationFileAsset,
        OfficeAction,
        OfficeActionFileAsset,
    )

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    oa_id = uuid.uuid4().hex
    comm_id = uuid.uuid4().hex
    oa_file_id = uuid.uuid4().hex
    comm_file_id = uuid.uuid4().hex
    oa_asset_id = uuid.uuid4().hex
    comm_asset_id = uuid.uuid4().hex

    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=mid,
            doc_name="Text Text Text",
            received_date="2026-02-23",
            notified_date="2026-02-21",
        )
    )
    db_session.add(
        Communication(
            comm_id=comm_id,
            matter_id=mid,
            comm_type="M",
            sent_date="2026-02-23",
            note="Text Text Text Text",
        )
    )

    db_session.add(
        FileAsset(
            file_asset_id=oa_asset_id,
            storage_type="local",
            file_path=f"tests/{oa_asset_id}.pdf",
            original_name="notice.pdf",
            sha256=uuid.uuid4().hex,
            byte_size=10,
            mime_type="application/pdf",
            created_at="2026-02-23T07:03:11",
        )
    )
    db_session.add(
        FileAsset(
            file_asset_id=comm_asset_id,
            storage_type="local",
            file_path=f"tests/{comm_asset_id}.eml",
            original_name="mail.eml",
            sha256=uuid.uuid4().hex,
            byte_size=10,
            mime_type="message/rfc822",
            created_at="2026-02-23T07:29:20",
        )
    )

    # created_at intentionally left null to validate fallback to file_asset.created_at.
    db_session.add(
        OfficeActionFileAsset(
            oa_file_id=oa_file_id,
            oa_id=oa_id,
            file_asset_id=oa_asset_id,
            role="pdf",
            created_at=None,
        )
    )
    db_session.add(
        CommunicationFileAsset(
            comm_file_id=comm_file_id,
            comm_id=comm_id,
            file_asset_id=comm_asset_id,
            role="upload",
            created_at=None,
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    keys = [str(r.get("row_key") or "") for r in (out.get("history_rows") or [])]

    assert keys[:2] == [f"letter:{comm_id}", f"notice:{oa_id}"]
