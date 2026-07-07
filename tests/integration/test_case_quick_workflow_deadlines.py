from __future__ import annotations

from datetime import date


def test_case_quick_workflow_maps_template_to_internal_due_and_single_leg_docket(
    admin_client, db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    resp = admin_client.post(
        f"/case/{matter_id}/quick/workflow",
        data={
            "title": "OA Text Text Text",
            "legal_due_date": "2026-04-21",
            "template_key": "OA",
        },
    )
    assert resp.status_code == 302

    wf = (
        Workflow.query.filter_by(case_id=matter_id, name="OA Text Text Text")
        .order_by(Workflow.id.desc())
        .first()
    )
    assert wf is not None
    assert wf.legal_due_date == date(2026, 4, 21)
    assert wf.due_date == date(2026, 4, 11)

    rows = DocketItem.query.filter_by(matter_id=matter_id).all()
    workflow_rows = [
        row
        for row in rows
        if (getattr(row, "raw_id", None) or "").startswith(f"WF-{wf.id}-")
        or (getattr(row, "docket_id", None) or "").startswith(f"WF-{wf.id}-")
    ]

    assert len(workflow_rows) == 1
    row = workflow_rows[0]
    assert row.due_date == "2026-04-21"
    assert row.extended_due_date == "2026-04-11"
    assert row.is_deleted is False
