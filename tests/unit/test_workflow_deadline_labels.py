from datetime import date


def test_workflow_deadline_title_normalizes_legacy_suffixes():
    from app.utils.workflow_deadline_labels import (
        strip_workflow_deadline_title_suffix,
        workflow_deadline_title,
    )

    assert strip_workflow_deadline_title_suffix("Office action response [Deadline]") == (
        "Office action response"
    )
    assert strip_workflow_deadline_title_suffix("Office action response [Statutory deadline]") == (
        "Office action response"
    )
    assert (
        workflow_deadline_title(
            "Office action response [Deadline]",
            "LEG",
            legal_due_date=date(2026, 4, 21),
            effective_due_date=date(2026, 4, 21),
        )
        == "Office action response [Final]"
    )
    assert (
        workflow_deadline_title(
            "Office action response [Final]",
            "LEG",
            legal_due_date=date(2026, 4, 21),
            effective_due_date=date(2026, 4, 18),
        )
        == "Office action response [Internal]"
    )
    assert (
        workflow_deadline_title(
            "Office action response [Internal]",
            "SUB",
            legal_due_date=date(2026, 4, 21),
            effective_due_date=date(2026, 4, 18),
        )
        == "Office action response [Internal]"
    )


def test_sync_from_workflow_sets_internal_due_on_leg_docket_and_normalizes_titles(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.task_sync import sync_from_workflow

    wf = Workflow(
        case_id=sample_matter.matter_id,
        name="Office action response [Deadline]",
        legal_due_date=date(2026, 4, 21),
        due_date=date(2026, 4, 18),
        status="Pending",
    )
    db_session.add(wf)
    db_session.commit()

    docket = DocketItem(
        docket_id=f"WF-{wf.id}-LEG",
        matter_id=sample_matter.matter_id,
        category="WORK",
        name_ref="Office action response [Deadline]",
        name_free="Office action response [Deadline]",
        due_date="2026-04-21",
        extended_due_date=None,
    )
    db_session.add(docket)
    db_session.commit()

    sync_from_workflow(workflow=wf, actor_id=None)
    db_session.commit()

    refreshed_wf = db_session.get(Workflow, wf.id)
    refreshed_docket = db_session.get(DocketItem, docket.docket_id)
    assert refreshed_wf is not None
    assert refreshed_docket is not None
    assert refreshed_wf.name == "Office action response"
    assert refreshed_docket.name_ref == "Office action response [Internal]"
    assert refreshed_docket.name_free == "Office action response [Internal]"
    assert refreshed_docket.due_date == "2026-04-21"
    assert refreshed_docket.extended_due_date == "2026-04-18"


def test_workflow_display_values_hides_deadline_suffix(app, db_session, sample_matter):
    from app.models.workflow import Workflow
    from app.services.workflow.status_sync import workflow_display_values

    wf = Workflow(
        case_id=sample_matter.matter_id,
        name="Office action response [Deadline]",
        legal_due_date=date(2026, 4, 21),
        due_date=date(2026, 4, 21),
        status="Pending",
    )
    db_session.add(wf)
    db_session.commit()

    assert workflow_display_values(wf)["name"] == "Office action response"


def test_workflow_deadline_kind_normalizes_legacy_identifier_aliases():
    from app.utils.workflow_deadline_labels import workflow_deadline_kind_from_docket_id

    assert workflow_deadline_kind_from_docket_id("WF-12-LEG") == "LEG"
    assert workflow_deadline_kind_from_docket_id("WF-12-LEGAL") == "LEG"
    assert workflow_deadline_kind_from_docket_id("WF-12-DRA") == "DRA"
    assert workflow_deadline_kind_from_docket_id("WF-12-DRAFT") == "DRA"
    assert workflow_deadline_kind_from_docket_id("WF-12-SUB") == "SUB"
    assert workflow_deadline_kind_from_docket_id("WF-12-SUBMIT") == "SUB"


def test_sync_from_workflow_soft_deletes_legacy_draft_and_submit_rows_when_cleared(
    app, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow
    from app.services.workflow.task_sync import sync_from_workflow

    wf = Workflow(
        case_id=sample_matter.matter_id,
        name="Office action response [Deadline]",
        legal_due_date=date(2026, 5, 21),
        due_date=date(2026, 5, 18),
        draft_due_date=None,
        submit_due_date=None,
        status="Pending",
    )
    db_session.add(wf)
    db_session.commit()

    db_session.add_all(
        [
            DocketItem(
                docket_id=f"WF-{wf.id}-LEG",
                matter_id=sample_matter.matter_id,
                category="WORK",
                due_date="2026-05-21",
            ),
            DocketItem(
                docket_id=f"WF-{wf.id}-DRA",
                matter_id=sample_matter.matter_id,
                category="WORK",
                due_date="2026-05-11",
            ),
            DocketItem(
                docket_id=f"legacy-random-{wf.id}",
                raw_id=f"WF-{wf.id}-SUBMIT",
                matter_id=sample_matter.matter_id,
                category="WORK",
                due_date="2026-05-20",
            ),
        ]
    )
    db_session.commit()

    sync_from_workflow(workflow=wf, actor_id=None)
    db_session.commit()

    leg = db_session.get(DocketItem, f"WF-{wf.id}-LEG")
    draft = db_session.get(DocketItem, f"WF-{wf.id}-DRA")
    submit = db_session.get(DocketItem, f"legacy-random-{wf.id}")

    assert leg is not None
    assert draft is not None
    assert submit is not None
    assert leg.is_deleted is False
    assert leg.extended_due_date == "2026-05-18"
    assert draft.is_deleted is True
    assert submit.is_deleted is True
