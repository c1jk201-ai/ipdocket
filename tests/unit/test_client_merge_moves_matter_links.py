import uuid


def test_client_merge_moves_matter_links(app, db_session):
    from flask import current_app

    from app.models.case_flat_index import CaseFlatIndex
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterCustomField, MatterPartyRole
    from app.services.client.client_merge_service import ClientMergeService

    # Keep this unit test focused: skip invoice-side merging complexity.
    # The app treats missing INVOICEAPP_INTEGRATED as disabled; mirror that here.
    had_integrated_key = "INVOICEAPP_INTEGRATED" in current_app.config
    old_integrated = current_app.config.get("INVOICEAPP_INTEGRATED")
    current_app.config.pop("INVOICEAPP_INTEGRATED", None)

    try:
        target = Client(name="Text-Text", party_id="party_target_01", extra={})
        source = Client(name="Text-Text", party_id="party_source_01", extra={})
        db_session.add_all([target, source])
        db_session.commit()

        matter_id = uuid.uuid4().hex
        db_session.add(
            Matter(
                matter_id=matter_id,
                our_ref="TEST-MERGE-MATTER-1",
                right_name="Text Text",
                right_group="DOM",
                matter_type="PATENT",
                is_deleted=False,
            )
        )
        db_session.add(
            MatterCustomField(
                matter_id=matter_id,
                namespace="domestic_patent",
                data={
                    "client_id": str(source.id),
                    "client_name": source.name,
                    "applicant_name": source.name,
                    "application_applicant_name": source.name,
                },
            )
        )
        db_session.add(
            MatterPartyRole(
                matter_id=matter_id,
                party_id=source.party_id,
                role_code="client",
                seq=1,
                raw_text=source.name,
            )
        )
        db_session.add(
            MatterPartyRole(
                matter_id=matter_id,
                party_id="",
                role_code="applicant",
                seq=1,
                raw_text=source.name,
            )
        )
        db_session.commit()

        result = ClientMergeService.merge_clients(
            target_client_id=int(target.id),
            source_client_ids=[int(source.id)],
            merge_notes=False,
            merged_by=None,
            reason="unit-test",
            backup_required=False,
            backup_attachments=False,
        )
        assert result.get("ok") is True

        mcf = MatterCustomField.query.filter_by(
            matter_id=matter_id, namespace="domestic_patent"
        ).first()
        assert mcf is not None
        assert (mcf.data or {}).get("client_id") == str(target.id)
        assert (mcf.data or {}).get("client_name") == target.name
        assert (mcf.data or {}).get("applicant_name") == target.name
        assert (mcf.data or {}).get("application_applicant_name") == target.name

        mpr = (
            MatterPartyRole.query.filter_by(matter_id=matter_id)
            .filter(MatterPartyRole.role_code == "client")
            .first()
        )
        assert mpr is not None
        assert mpr.party_id == target.party_id
        assert mpr.raw_text == target.name

        applicant_role = (
            MatterPartyRole.query.filter_by(matter_id=matter_id)
            .filter(MatterPartyRole.role_code == "applicant")
            .first()
        )
        assert applicant_role is not None
        assert applicant_role.raw_text == target.name

        flat = CaseFlatIndex.query.get(matter_id)
        assert flat is not None
        assert flat.client_name == target.name
        assert flat.applicant == target.name

    finally:
        if had_integrated_key:
            current_app.config["INVOICEAPP_INTEGRATED"] = old_integrated
        else:
            current_app.config.pop("INVOICEAPP_INTEGRATED", None)


def test_client_merge_adopts_source_party_id_before_case_flat_index_refresh(app, db_session):
    from flask import current_app

    from app.models.case_flat_index import CaseFlatIndex
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterCustomField, MatterPartyRole
    from app.services.client.client_merge_service import ClientMergeService

    had_integrated_key = "INVOICEAPP_INTEGRATED" in current_app.config
    old_integrated = current_app.config.get("INVOICEAPP_INTEGRATED")
    current_app.config.pop("INVOICEAPP_INTEGRATED", None)

    try:
        target = Client(name="Text-Text", party_id=None, extra={})
        source = Client(name="Text-Text", party_id="party_source_adopt_01", extra={})
        db_session.add_all([target, source])
        db_session.commit()

        target_id = int(target.id)
        source_id = int(source.id)
        source_party_id = str(source.party_id)

        matter_id = uuid.uuid4().hex
        db_session.add(
            Matter(
                matter_id=matter_id,
                our_ref="TEST-MERGE-MATTER-ADOPT",
                right_name="Text Text",
                right_group="DOM",
                matter_type="PATENT",
                is_deleted=False,
            )
        )
        db_session.add(
            MatterCustomField(
                matter_id=matter_id,
                namespace="domestic_patent",
                data={
                    "client_id": str(source_id),
                    "client_name": source.name,
                    "applicant_name": source.name,
                },
            )
        )
        db_session.add(
            MatterPartyRole(
                matter_id=matter_id,
                party_id=source_party_id,
                role_code="client",
                seq=1,
                raw_text=source.name,
            )
        )
        db_session.commit()

        result = ClientMergeService.merge_clients(
            target_client_id=target_id,
            source_client_ids=[source_id],
            merge_notes=False,
            merged_by=None,
            reason="unit-test-adopt-party",
            backup_required=False,
            backup_attachments=False,
        )
        assert result.get("ok") is True

        db_session.expire_all()
        target_after = db_session.get(Client, target_id)
        source_after = db_session.get(Client, source_id)
        assert target_after.party_id == source_party_id
        assert source_after.party_id is None
        assert source_after.is_deleted is True

        flat = CaseFlatIndex.query.get(matter_id)
        assert flat is not None
        assert flat.client_name == target_after.name
        assert flat.applicant == target_after.name

    finally:
        if had_integrated_key:
            current_app.config["INVOICEAPP_INTEGRATED"] = old_integrated
        else:
            current_app.config.pop("INVOICEAPP_INTEGRATED", None)
