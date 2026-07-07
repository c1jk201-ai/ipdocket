import uuid


def test_case_detail_history_includes_email_id_for_mail_linked_comm(db_session, sample_matter):
    """
    Cross-linking: when a Communication is created from email ingestion,
    email_message_matter_link(comm_id) should allow the case detail history rows
    to expose the source email id.
    """
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.communication import Communication
    from app.models.ip_records import EmailMessage, EmailMessageMatterLink

    mid = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    comm_id = uuid.uuid4().hex
    email_id = uuid.uuid4().hex

    db_session.add(
        Communication(
            comm_id=comm_id,
            matter_id=mid,
            comm_type="M",
            received_date="2026-02-01",
            note="Text Text Text",
        )
    )
    db_session.add(
        EmailMessage(
            id=email_id,
            mailbox_tag="DOCKET_INBOX",
            processing_status="INBOX_NEW",
            subject="Text Text Text",
        )
    )
    db_session.add(
        EmailMessageMatterLink(
            email_id=email_id,
            matter_id=mid,
            comm_id=comm_id,
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": mid})
    rows = out.get("history_rows") or []
    hit = next((r for r in rows if r.get("kind") == "letter" and r.get("id") == comm_id), None)
    assert hit is not None
    assert hit.get("email_id") == email_id
