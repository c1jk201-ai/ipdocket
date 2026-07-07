from __future__ import annotations

import uuid
from datetime import date, timedelta


def test_auto_complete_workflows_from_events_syncs_linked_docket_done_date(app, db_session):
    from app.blueprints.case.helpers import _auto_complete_workflows_from_events
    from app.models.docket import DocketItem
    from app.models.matter import Matter, MatterEvent
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    docket_id = uuid.uuid4().hex
    done_ymd = "2026-01-19"

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0002",
            right_name="Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=mid,
            category="WORK",
            name_ref="Text Text Text",
            name_free="Text Text Text",
            due_date=(date.today() + timedelta(days=5)).isoformat(),
            is_deleted=False,
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Text Text Text",
        status="Pending",
        business_code=f"DOCKET:{docket_id}",
    )
    db_session.add(wf)
    db_session.add(
        MatterEvent(
            matter_id=mid,
            event_key="Text/Text",
            event_at=done_ymd,
        )
    )
    db_session.commit()

    _auto_complete_workflows_from_events(matter_id=mid)
    db_session.commit()

    wf2 = db_session.get(Workflow, wf.id)
    di = db_session.get(DocketItem, docket_id)

    assert wf2 is not None
    assert wf2.status == "Completed"
    assert wf2.completed_date is not None
    assert wf2.completed_date.isoformat() == done_ymd

    assert di is not None
    assert (di.done_date or "").strip() == done_ymd


def test_auto_complete_workflows_from_events_completes_intake_confirmation_on_filing_date(
    app, db_session
):
    from app.blueprints.case.helpers import _auto_complete_workflows_from_events
    from app.models.matter import Matter, MatterEvent
    from app.models.workflow import Workflow

    mid = uuid.uuid4().hex
    done_ymd = "2026-02-27"

    db_session.add(
        Matter(
            matter_id=mid,
            our_ref="26UT0003",
            right_name="Text Text Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    wf = Workflow(
        case_id=mid,
        name="Text Text",
        status="Pending",
        category="MGMT",
        business_code=f"INTAKE:{mid}:2",
    )
    db_session.add(wf)
    db_session.add(
        MatterEvent(
            matter_id=mid,
            event_key="Text",
            event_at=done_ymd,
        )
    )
    db_session.commit()

    _auto_complete_workflows_from_events(matter_id=mid)
    db_session.commit()

    wf2 = db_session.get(Workflow, wf.id)
    assert wf2 is not None
    assert wf2.status == "Completed"
    assert wf2.completed_date is not None
    assert wf2.completed_date.isoformat() == done_ymd
