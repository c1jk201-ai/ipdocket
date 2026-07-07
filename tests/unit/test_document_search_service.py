from __future__ import annotations

import uuid


def test_dms_search_returns_acl_filtered_evidence_snippets(
    authenticated_client,
    sample_matter,
    db_session,
):
    from app.models.ip_records import Matter
    from app.services.document_search_service import upsert_document_index

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    hidden_matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=hidden_matter_id,
            our_ref=f"HIDDEN-{uuid.uuid4().hex[:8]}",
            right_name="Hidden Matter",
            is_deleted=False,
        )
    )
    db_session.commit()

    upsert_document_index(
        matter_id=matter_id,
        source_type="matter_file",
        source_id="visible-source",
        title="Visible document",
        body="This evidence snippet includes DOCKETPORTAL-NEEDLE for the assigned matter.",
    )
    upsert_document_index(
        matter_id=hidden_matter_id,
        source_type="matter_file",
        source_id="hidden-source",
        title="Hidden document",
        body="DOCKETPORTAL-NEEDLE should not leak across matter ACL.",
    )

    res = authenticated_client.get("/api/dms/searchNewq=DOCKETPORTAL-NEEDLE")
    assert res.status_code == 200
    data = res.get_json() or {}
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["items"][0]["matter_id"] == matter_id
    assert "DOCKETPORTAL-NEEDLE" in data["items"][0]["snippet"]
