import uuid


def test_case_list_extras_reads_client_id_from_non_basic_namespace(db_session):
    """
    /case/list deep-links should work even when MatterCustomField.data.client_id is stored
    in a case-specific namespace (e.g. 'domestic_patent'), not only in 'basic'.
    """
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    crm_client = Client(name="Text", extra={})
    db_session.add(crm_client)
    db_session.commit()

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-CLIENT-LINK-1",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-CLIENT-LINK-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        clients="Text Text",
        applicants="Text Text",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data={"client_id": str(crm_client.id), "client_name": crm_client.name},
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["client_id"] == str(crm_client.id)
    assert extras[matter_id]["client_name"] == crm_client.name


def test_case_list_extras_resolves_client_via_party_role(db_session):
    """
    If a matter has a 'client' MatterPartyRole with party_id, the case list should resolve
    CRM client_id via Client.party_id even if no custom-field client_id exists.
    """
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterPartyRole, VMatterOverview

    crm_client = Client(name="Text Text", party_id="party_kakaobank", extra={})
    db_session.add(crm_client)
    db_session.commit()

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-CLIENT-LINK-PARTY-1",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-CLIENT-LINK-PARTY-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        clients="Text",
        applicants="Text Text",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
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

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["client_id"] == str(crm_client.id)
    assert extras[matter_id]["client_name"] == crm_client.name


def test_case_list_extras_resolves_applicant_to_crm_client(db_session):
    """
    Applicant is also treated as a CRM Client entity. For list view convenience, link applicant
    to CRM client when it can be resolved uniquely (prefer party_id mapping).
    """
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.client import Client
    from app.models.ip_records import Matter, MatterPartyRole, VMatterOverview

    crm_client = Client(name="Text-UNIT", party_id="party_royjung", extra={})
    db_session.add(crm_client)
    db_session.commit()

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-APPLICANT-LINK-1",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-APPLICANT-LINK-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        clients="",
        applicants="Text-UNIT",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            party_id=crm_client.party_id,
            role_code="applicant",
            seq=1,
            raw_text=crm_client.name,
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["applicant_client_id"] == str(crm_client.id)


def test_case_list_extras_prefers_application_form_applicant_over_party_role(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterCustomField, MatterPartyRole, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-APP-FORM-APPLICANT-1",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-APP-FORM-APPLICANT-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        clients="Text",
        applicants="Text",
        attorneys="",
        entered_at="2026-04-28",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data={
                "applicant_name": "Text",
                "application_applicant_name": "Text Text",
            },
        )
    )
    db_session.add(
        MatterPartyRole(
            matter_id=matter_id,
            role_code="applicant",
            seq=1,
            raw_text="Text",
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["applicant_name"] == "Text Text"


def test_case_list_extras_reads_application_no_from_app_no_identifier(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterIdentifier, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-APP-NO-1",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-APP-NO-1",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterIdentifier(
            matter_id=matter_id,
            id_type="APP_NO",
            id_value="10-2026-0031355",
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["application_no"] == "10-2026-0031355"


def test_case_list_extras_uses_auto_blue_even_when_inhouse_status_exists(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-INHOUSE-BLUE-1",
            right_name="Text Text",
            right_group="INC",
            matter_type="TRADEMARK",
            status_blue="Text Text Text",
            inhouse_status="Text Text Text",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-INHOUSE-BLUE-1",
        right_name="Text Text",
        right_group="INC",
        matter_type="TRADEMARK",
        status_blue="Text Text Text",
        inhouse_status="Text Text Text",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["display_blue"] == "Text Text Text"
    assert extras[matter_id]["display_red"] == ""


def test_case_list_extras_derives_blue_from_custom_fields_when_events_missing(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-CUSTOM-BLUE-1",
            right_name="Text Text",
            right_group="INC",
            matter_type="TRADEMARK",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-CUSTOM-BLUE-1",
        right_name="Text Text",
        right_group="INC",
        matter_type="TRADEMARK",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="incoming_trademark",
            data={"application_date": "2025-12-22"},
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["display_blue"] == "Filing Examination In Progress"


def test_case_list_extras_refines_existing_blue_with_post_filing_pending(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26PD0999US",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            status_red="Text",
            status_red_related_date="2027-02-19",
            status_blue="Text Text Text",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="26PD0999US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        status_red="Text",
        status_blue="Text Text Text",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data={
                "application_date": "2026-02-19",
                "foreign_filing_deadline": "2027-02-19",
            },
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["display_blue"] == "ForeignFiling In Progress(ExaminationBilling)"
    assert extras[matter_id]["display_red"] == "Text[2027-02-19]"


def test_case_list_extras_uses_earliest_mgmt_status_red_candidate_for_pct(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import DocketItem, Matter, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26PD0998PCT",
            right_name="PCT Text Text",
            right_group="ETC",
            matter_type="PCT",
            status_blue="Text Text Text",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="26PD0998PCT",
        right_name="PCT Text Text",
        right_group="ETC",
        matter_type="PCT",
        status_blue="Text Text Text",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add_all(
        [
            DocketItem(
                docket_id=uuid.uuid4().hex,
                matter_id=matter_id,
                category="MGMT_WORK",
                name_ref="MGMT:STATUS_RED:Domestic Deadline 1 Notice",
                name_free="Domestic Deadline 1 Notice",
                due_date="2027-01-01",
                done_date=None,
                is_deleted=False,
            ),
            DocketItem(
                docket_id=uuid.uuid4().hex,
                matter_id=matter_id,
                category="MGMT_WORK",
                name_ref="MGMT:STATUS_RED:PCTText",
                name_free="PCTText",
                due_date="2028-01-01",
                done_date=None,
                is_deleted=False,
            ),
            DocketItem(
                docket_id=uuid.uuid4().hex,
                matter_id=matter_id,
                category="MGMT_WORK",
                name_ref="MGMT:STATUS_RED:Notice",
                name_free="Notice",
                due_date="2027-06-05",
                done_date=None,
                is_deleted=False,
            ),
        ]
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])

    assert extras[matter_id]["display_red"] == "Notice[2027-06-05]"


def test_case_list_extras_keeps_mainline_blue_with_parallel_foreign_action(db_session):
    from datetime import date, timedelta

    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    application_date = (date.today() - timedelta(days=30)).isoformat()
    exam_request_date = (date.today() - timedelta(days=20)).isoformat()
    foreign_due = (date.today() + timedelta(days=10)).isoformat()
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26PD0997US",
            right_name="Text Text",
            right_group="DOM",
            matter_type="PATENT",
            status_red="ForeignFilingDeadline",
            status_red_related_date=foreign_due,
            status_blue="Filing Examination In Progress",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="26PD0997US",
        right_name="Text Text",
        right_group="DOM",
        matter_type="PATENT",
        status_red="ForeignFilingDeadline",
        status_blue="Filing Examination In Progress",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_patent",
            data={
                "application_date": application_date,
                "exam_request_date": exam_request_date,
                "foreign_filing_deadline": foreign_due,
            },
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert (
        extras[matter_id]["display_blue"]
        == "Filing Examination In Progress · ForeignFiling  In Progress"
    )


def test_case_list_extras_hides_internal_mgmt_notice_ref_from_status_red(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26PO0998US",
            right_name="Text Text",
            right_group="OUT",
            matter_type="PATENT",
            status_red="MGMT:FOREIGN_FILING_NOTICE_3M",
            status_red_related_date="2027-01-22",
            status_blue="Text Text Text",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="26PO0998US",
        right_name="Text Text",
        right_group="OUT",
        matter_type="PATENT",
        status_red="MGMT:FOREIGN_FILING_NOTICE_3M",
        status_blue="Text Text Text",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["display_red"] == ""


def test_case_list_extras_reads_trademark_classes_from_custom_fields(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, MatterCustomField, VMatterOverview

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-TM-CLASS-1",
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="TRADEMARK",
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-TM-CLASS-1",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        MatterCustomField(
            matter_id=matter_id,
            namespace="domestic_trademark",
            data={"application_classes": "09, 35"},
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["trademark_classes"] == "09, 35"


def test_case_list_extras_reads_trademark_classes_from_raw_import_fallback(db_session):
    from app.blueprints.case.helpers import _build_case_list_extras
    from app.models.ip_records import Matter, VMatterOverview
    from app.models.raw_import import RawImportField

    matter_id = uuid.uuid4().hex
    raw_id = f"RAW-{uuid.uuid4().hex[:8]}"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="TEST-CASE-LIST-TM-CLASS-RAW-1",
            right_name="Text Text Text",
            right_group="DOM",
            matter_type="TRADEMARK",
            raw_id=raw_id,
            is_deleted=False,
        )
    )
    overview = VMatterOverview(
        matter_id=matter_id,
        our_ref="TEST-CASE-LIST-TM-CLASS-RAW-1",
        right_name="Text Text Text",
        right_group="DOM",
        matter_type="TRADEMARK",
        clients="",
        applicants="",
        attorneys="",
        entered_at="2026-01-22",
    )
    db_session.add(overview)
    db_session.add(
        RawImportField(
            raw_id=raw_id,
            sheet_name="Matter",
            source_column="Type",
            value_text="35",
        )
    )
    db_session.commit()

    extras = _build_case_list_extras([overview])
    assert extras[matter_id]["trademark_classes"] == "35"
