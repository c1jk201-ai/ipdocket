import uuid


def test_crm_client_merge_page_counts_matter_links_via_party_role(admin_client, db_session):
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterPartyRole

    crm_client = Client(name="Text-Text", party_id="test_party_merge_01", extra={})
    db_session.add(crm_client)
    db_session.commit()

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CRM-MERGE-COUNT-1",
            right_name="Text Text (merge count)",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            party_id=crm_client.party_id,
            role_code="client",
            seq=1,
            raw_text=crm_client.name,
        )
    )
    db_session.commit()

    client_id = int(crm_client.id)
    client_name = str(crm_client.name or "")

    resp = admin_client.get("/crm/clients/merge")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8")

    # The merge table shows case counts per CRM client. For migrated matters, the link comes from
    # matter_party_role.party_id == clients.party_id (role_code='client').
    assert f"#{client_id} {client_name}" in html
    assert "Matter 1" in html
