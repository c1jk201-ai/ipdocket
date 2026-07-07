from __future__ import annotations

import uuid
from datetime import date


def test_reconcile_linked_docket_workflow_fields_tracks_source_without_overwriting_manual_dates(
    app, db_session
):
    from app.models.docket import DocketItem
    from app.models.matter import Matter
    from app.models.workflow import Workflow
    from app.services.workflow.status_sync import reconcile_linked_docket_workflow_fields

    matter_id = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26UT0099",
            right_name="DOCKET Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    docket = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="Text",
        name_free="Text Text",
        extended_due_date="2026-05-06",
        is_deleted=False,
    )
    workflow = Workflow(
        case_id=matter_id,
        name="Text Text [Text]",
        status="Pending",
        business_code=f"DOCKET:{docket_id}",
        due_date=date(2026, 5, 1),
        legal_due_date=date(2026, 5, 6),
    )
    db_session.add_all([docket, workflow])
    db_session.commit()

    changed = reconcile_linked_docket_workflow_fields(
        workflow,
        linked_docket_item=docket,
    )

    assert changed is True
    assert workflow.name == "Text Text [Text]"
    assert workflow.legal_due_date == date(2026, 5, 6)
    assert workflow.due_date == date(2026, 5, 1)
    assert workflow.source_docket_due_date == date(2026, 5, 6)
    assert workflow.source_docket_legal_due_date is None


def test_reconcile_linked_docket_workflow_fields_updates_when_source_docket_dates_change(
    app, db_session
):
    from app.models.docket import DocketItem
    from app.models.matter import Matter
    from app.models.workflow import Workflow
    from app.services.workflow.status_sync import reconcile_linked_docket_workflow_fields

    matter_id = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex

    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26UT0100",
            right_name="DOCKET Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    docket = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-06-10",
        extended_due_date="2026-06-01",
        is_deleted=False,
    )
    workflow = Workflow(
        case_id=matter_id,
        name="Text Text",
        status="Pending",
        business_code=f"DOCKET:{docket_id}",
        due_date=date(2026, 5, 1),
        legal_due_date=date(2026, 5, 6),
        source_docket_due_date=date(2026, 5, 1),
        source_docket_legal_due_date=date(2026, 5, 6),
    )
    db_session.add_all([docket, workflow])
    db_session.commit()

    changed = reconcile_linked_docket_workflow_fields(
        workflow,
        linked_docket_item=docket,
    )

    assert changed is True
    assert workflow.legal_due_date == date(2026, 6, 10)
    assert workflow.due_date == date(2026, 6, 1)
    assert workflow.source_docket_legal_due_date == date(2026, 6, 10)
    assert workflow.source_docket_due_date == date(2026, 6, 1)
