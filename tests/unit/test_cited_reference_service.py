import json
import uuid
from types import SimpleNamespace


def test_parse_citations_from_oa_pdf_text_extracts_numbered_references():
    from app.services.citations.cited_reference_service import parse_citations_from_text

    text = """
    ◆ Text 1 : Text Text Text2001-295129Text(2001.10.26.)
    ◆ Text 2 : Text Text US9693594(2017.07.04.)
    ◆ Text 3 : Text Text US2019/0373976Text(2019.12.12.)
    Text. Text 1Text Text Text Text.
    """

    refs = parse_citations_from_text(text)

    assert [r.label for r in refs] == ["Text 1", "Text 2", "Text 3"]
    assert refs[1].publication_number == "US9693594"
    assert refs[2].country == "US"
    assert refs[2].published_date == "2019-12-12"


def test_parse_citations_handles_registered_number_and_non_patent_title():
    from app.services.citations.cited_reference_service import parse_citations_from_text

    refs = parse_citations_from_text(
        """
        Text 1: Text Text10-2730495Text(2024.11.18.)
        Text 2: Zahra Abbasi-Moud et al., "Tourism recommendation system based on semantic clustering and sentiment analysis," Expert Systems With Applications (2020.11.21.)
        """
    )

    assert refs[0].publication_number == "10-2730495"
    assert refs[0].ref_type == "patent"
    assert refs[1].publication_number is None
    assert refs[1].ref_type == "non_patent"


def test_parse_citations_stops_before_oa_body_text():
    from app.services.citations.cited_reference_service import parse_citations_from_text

    refs = parse_citations_from_text(
        """
        Text 1: Text Text10-2021-0022873Text(2021. 3. 4.) Text 2023. 1. 16.Text Text Text Text 1Text Text Text 2Text Text Text.
        Text 2: Text Text US9693594(2017.07.04.) (1) Text Text1Text Text Text 1Text Text.
        Text 3: Text Text10-1993703Text(2019.06.27.)1-1. Text1Text Text Text Text.
        Text 4: Text Text20-2008-0001740Text(2008.06.11.) Text 1 Text Text Text Text.
        Text 5: Zhao, et al. "Association between genes." Journal of diabetes research 2018.1 (2018)Text. Text 1Text Text Text Text Text Text Text.
        Text 6: Text Text10-2022-0090809Text(2022.06.30. Text)2.2 Text Text Text Text 1Text Text Text
        Text 7: Text Text10-2022-0020694Text(2022.02.21.) Text Text1Text Text 1Text Text Text.
        Text 8: Text Text10-2022-0040646Text(2022.03.31.) Text Text Text 223 Text
        Text 9: Text Text10-2022-0006993Text(2022.01.18.)Text. Text Text(1) 2025. 5. 29.Text Text Text
        Text 10: Federico Baldassarre Text, Bioinformatics, 2020.08.11., Vol.37, No.3, pp.360-366.oText Text Text2Text Text Text Text Text Text.
        Text 11: Text LDM layer4 Text Text AIText Text Text Text Text Text. Text, 2024.05.27., pp. 1-90.Text Text 10Text Text Text Text Text.
        Text 12: K. Yang et al., Mesoscale Modeling of Microstructure Evolution in Lithium Battery Electrode Material, Thesis, Rice University(2021.05.03.)1-1. Text Text Text Text. Text
        Text 13: Text Text10-2208246Text(2021.01.28.)Text Text Text Text Text Text Text Text.
        """
    )

    assert len(refs) == 13
    assert refs[0].raw_text == "Text Text10-2021-0022873Text(2021. 3. 4.)"
    assert refs[1].raw_text == "Text Text US9693594(2017.07.04.)"
    assert refs[2].raw_text == "Text Text10-1993703Text(2019.06.27.)"
    assert refs[3].raw_text == "Text Text20-2008-0001740Text(2008.06.11.)"
    assert refs[3].publication_number == "20-2008-0001740"
    assert (
        refs[4].raw_text
        == 'Zhao, et al. "Association between genes." Journal of diabetes research 2018.1 (2018)'
    )
    assert refs[5].raw_text == "Text Text10-2022-0090809Text(2022.06.30. Text)"
    assert refs[6].raw_text == "Text Text10-2022-0020694Text(2022.02.21.)"
    assert refs[7].raw_text == "Text Text10-2022-0040646Text(2022.03.31.)"
    assert refs[8].raw_text == "Text Text10-2022-0006993Text(2022.01.18.)"
    assert (
        refs[9].raw_text
        == "Federico Baldassarre Text, Bioinformatics, 2020.08.11., Vol.37, No.3, pp.360-366."
    )
    assert (
        refs[10].raw_text
        == "Text LDM layer4 Text Text AIText Text Text Text Text Text. Text, 2024.05.27., pp. 1-90."
    )
    assert (
        refs[11].raw_text
        == "K. Yang et al., Mesoscale Modeling of Microstructure Evolution in Lithium Battery Electrode Material, Thesis, Rice University(2021.05.03.)"
    )
    assert refs[12].raw_text == "Text Text10-2208246Text(2021.01.28.)"


def test_create_ids_tasks_for_us_family_creates_target_workflow(db_session):
    from app.models.cited_reference import CitedReference
    from app.models.matter import Family, Matter, MatterFamily
    from app.models.workflow import Workflow
    from app.services.citations.cited_reference_service import (
        create_ids_tasks_for_us_family,
        parse_citations_from_text,
    )

    family_id = uuid.uuid4().hex
    source_id = uuid.uuid4().hex
    target_id = uuid.uuid4().hex
    db_session.add(Family(family_id=family_id, family_key="TEST-FAM", key_type="manual"))
    db_session.add(
        Matter(
            matter_id=source_id,
            our_ref="26PD0001US",
            right_group="DOM",
            matter_type="PATENT",
            right_name="US source",
        )
    )
    db_session.add(
        Matter(
            matter_id=target_id,
            our_ref="26PO0001US",
            right_group="OUT",
            matter_type="PATENT",
            right_name="US target",
        )
    )
    db_session.add(MatterFamily(mf_id=uuid.uuid4().hex, matter_id=source_id, family_id=family_id))
    db_session.add(MatterFamily(mf_id=uuid.uuid4().hex, matter_id=target_id, family_id=family_id))
    db_session.commit()

    refs = parse_citations_from_text("Text 1: Text Text US9693594(2017.07.04.)")
    result = create_ids_tasks_for_us_family(
        source_matter_id=source_id,
        source_oa_id="oa-test",
        citations=refs,
        source_doc_name="Non-Final Office Action",
    )
    db_session.commit()

    assert result.created_count == 1
    workflow = Workflow.query.filter_by(case_id=target_id).one()
    assert workflow.name.startswith("IDS review - ")
    assert workflow.business_code.startswith("IDS:oa-test:")
    assert CitedReference.query.filter_by(workflow_id=workflow.id).count() == 0
    assert CitedReference.query.filter_by(office_action_id="oa-test").count() == 0
    assert "US9693594" in (workflow.note or "")


def test_create_ids_tasks_for_us_family_updates_existing_note(db_session):
    from app.models.matter import Family, Matter, MatterFamily
    from app.models.workflow import Workflow
    from app.services.citations.cited_reference_service import (
        create_ids_tasks_for_us_family,
        parse_citations_from_text,
    )

    family_id = uuid.uuid4().hex
    source_id = uuid.uuid4().hex
    target_id = uuid.uuid4().hex
    db_session.add(Family(family_id=family_id, family_key="TEST-FAM2", key_type="manual"))
    db_session.add(
        Matter(
            matter_id=source_id,
            our_ref="26PD0003US",
            right_group="DOM",
            matter_type="PATENT",
        )
    )
    db_session.add(
        Matter(
            matter_id=target_id,
            our_ref="26PO0003US",
            right_group="OUT",
            matter_type="PATENT",
        )
    )
    db_session.add(MatterFamily(mf_id=uuid.uuid4().hex, matter_id=source_id, family_id=family_id))
    db_session.add(MatterFamily(mf_id=uuid.uuid4().hex, matter_id=target_id, family_id=family_id))
    db_session.commit()

    first_refs = parse_citations_from_text("Text 1: Text Text US9693594(2017.07.04.)")
    create_ids_tasks_for_us_family(
        source_matter_id=source_id,
        source_oa_id="oa-note-test",
        citations=first_refs,
        source_doc_name="Non-Final Office Action",
    )
    db_session.commit()

    workflow = Workflow.query.filter_by(case_id=target_id).one()
    assert "US9693594" in (workflow.note or "")

    second_refs = parse_citations_from_text("Text 1: Text Text US1234567(2018.01.02.)")
    result = create_ids_tasks_for_us_family(
        source_matter_id=source_id,
        source_oa_id="oa-note-test",
        citations=second_refs,
        source_doc_name="Non-Final Office Action",
    )
    db_session.commit()

    assert result.created_count == 0
    assert result.updated_count == 1
    db_session.refresh(workflow)
    assert "US1234567" in (workflow.note or "")
    assert "US9693594" not in (workflow.note or "")


def test_matter_office_action_citation_groups_are_oa_scoped(db_session):
    from app.models.communication import OfficeAction
    from app.models.matter import Matter
    from app.services.citations.cited_reference_service import (
        matter_office_action_citation_groups,
        replace_office_action_citations_from_text,
    )

    matter_id = uuid.uuid4().hex
    oa_id = uuid.uuid4().hex
    response_oa_id = uuid.uuid4().hex
    decision_oa_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26PD0002US",
            right_group="DOM",
            matter_type="PATENT",
            right_name="US source",
        )
    )
    db_session.add(
        OfficeAction(
            oa_id=oa_id,
            matter_id=matter_id,
            doc_name="Non-Final Office Action",
            notified_date="2026-05-15",
        )
    )
    db_session.add(
        OfficeAction(
            oa_id=response_oa_id,
            matter_id=matter_id,
            doc_name="Response to Office Action",
            received_date="2026-05-16",
        )
    )
    db_session.add(
        OfficeAction(
            oa_id=decision_oa_id,
            matter_id=matter_id,
            doc_name="Notice of Allowance",
            received_date="2026-05-17",
        )
    )
    db_session.commit()

    replace_office_action_citations_from_text(
        matter_id=matter_id,
        office_action_id=oa_id,
        text_value="Text 1: Text Text US9693594(2017.07.04.)",
        source="manual_progress",
    )
    replace_office_action_citations_from_text(
        matter_id=matter_id,
        office_action_id=response_oa_id,
        text_value="Text 1: Text Text US1234567(2018.01.02.)",
        source="manual_progress",
    )
    db_session.commit()

    groups = matter_office_action_citation_groups(matter_id)

    assert len(groups) == 1
    assert groups[0]["oa_id"] == oa_id
    assert groups[0]["count"] == 1
    assert "US9693594" in groups[0]["text"]
    assert response_oa_id not in {group["oa_id"] for group in groups}
    assert decision_oa_id not in {group["oa_id"] for group in groups}


def test_save_auto_office_action_citations_preserves_manual_and_can_clear_auto(db_session):
    from app.models.cited_reference import CitedReference
    from app.models.communication import OfficeAction
    from app.models.matter import Matter
    from app.services.citations.cited_reference_service import (
        parse_citations_from_text,
        save_auto_office_action_citations,
    )

    matter_id = uuid.uuid4().hex
    oa_id = uuid.uuid4().hex
    db_session.add(Matter(matter_id=matter_id, our_ref="26PD0004US"))
    db_session.add(OfficeAction(oa_id=oa_id, matter_id=matter_id, doc_name="Text"))
    db_session.commit()

    auto_refs = parse_citations_from_text("Text 1: Text Text US9693594(2017.07.04.)")
    save_auto_office_action_citations(
        matter_id=matter_id,
        office_action_id=oa_id,
        drafts=auto_refs,
    )
    db_session.commit()
    assert CitedReference.query.filter_by(office_action_id=oa_id, source="auto_pdf").count() == 1

    save_auto_office_action_citations(
        matter_id=matter_id,
        office_action_id=oa_id,
        drafts=[],
        clear_when_empty=True,
    )
    db_session.commit()
    assert CitedReference.query.filter_by(office_action_id=oa_id, source="auto_pdf").count() == 0

    db_session.add(
        CitedReference(
            matter_id=matter_id,
            office_action_id=oa_id,
            source="manual",
            raw_text="Text Text",
            sort_order=1,
        )
    )
    db_session.commit()
    rows = save_auto_office_action_citations(
        matter_id=matter_id,
        office_action_id=oa_id,
        drafts=auto_refs,
    )
    db_session.commit()

    assert rows == []
    assert CitedReference.query.filter_by(office_action_id=oa_id).count() == 1
    assert CitedReference.query.filter_by(office_action_id=oa_id, source="manual").count() == 1


def test_save_auto_workflow_citations_replaces_rows(db_session):
    from app.models.cited_reference import CitedReference
    from app.models.matter import Matter
    from app.models.workflow import Workflow
    from app.services.citations.cited_reference_service import (
        parse_citations_from_text,
        save_auto_workflow_citations,
    )

    matter_id = uuid.uuid4().hex
    db_session.add(Matter(matter_id=matter_id, our_ref="26PD0005US"))
    workflow = Workflow(case_id=matter_id, name="Text")
    db_session.add(workflow)
    db_session.commit()

    first_refs = parse_citations_from_text("Text 1: Text Text US9693594(2017.07.04.)")
    save_auto_workflow_citations(
        matter_id=matter_id,
        workflow_id=workflow.id,
        drafts=first_refs,
    )
    db_session.commit()
    assert CitedReference.query.filter_by(workflow_id=workflow.id).count() == 1

    second_refs = parse_citations_from_text("Text 1: Text Text US1234567(2018.01.02.)")
    save_auto_workflow_citations(
        matter_id=matter_id,
        workflow_id=workflow.id,
        drafts=second_refs,
    )
    db_session.commit()

    rows = CitedReference.query.filter_by(workflow_id=workflow.id).all()
    assert len(rows) == 1
    assert rows[0].publication_number == "US1234567"


def test_extract_citations_from_pdf_bytes_ignores_non_pdf_bytes():
    from app.services.citations.cited_reference_service import extract_citations_from_pdf_bytes

    refs = extract_citations_from_pdf_bytes(b"not a pdf", max_bytes=100)

    assert refs == []


def test_pdf_citation_page_limit_is_capped_at_two(monkeypatch):
    import app.services.citations.cited_reference_service as svc

    monkeypatch.setattr(svc.ConfigService, "get_int", staticmethod(lambda *args, **kwargs: 2))

    assert svc._coerce_pdf_page_limit(12) == 2
    assert svc._coerce_pdf_page_limit(None) == 2


def test_parse_ai_citations_from_text_uses_structured_output(monkeypatch):
    import app.services.citations.cited_reference_service as svc

    payload = {
        "references": [
            {
                "label": "Text 1",
                "ref_type": "patent",
                "country": "US",
                "publication_number": "US2019/0373976",
                "published_date": "2019-12-12",
                "title": "",
                "raw_text": "Text Text US2019/0373976Text (2019.12.12.)",
            }
        ]
    }

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            assert kwargs["response_format"]["json_schema"]["name"] == "OaCitedReferences"
            assert "office action" in kwargs["messages"][1]["content"].casefold()
            assert "US2019/0373976" in kwargs["messages"][1]["content"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, api_key):
            assert api_key == "test-key"
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(svc, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(svc, "get_openai_api_key", lambda: "test-key")

    refs = svc.parse_ai_citations_from_text("OA Text. Text 1: US2019/0373976")

    assert len(refs) == 1
    assert refs[0].publication_number == "US2019/0373976"
    assert refs[0].country == "US"
