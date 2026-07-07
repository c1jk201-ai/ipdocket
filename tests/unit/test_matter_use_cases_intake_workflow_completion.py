from __future__ import annotations


def test_create_intake_workflows_starts_completed_when_filing_signal_exists(
    app, db_session, sample_matter
):
    from app.models.matter import MatterEvent
    from app.models.workflow import Workflow
    from app.services.matter.matter_use_cases import _create_intake_workflows

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))
    done_ymd = "2026-02-27"

    db_session.add(
        MatterEvent(
            matter_id=matter_id,
            event_key="Filing date",
            event_at=done_ymd,
        )
    )
    db_session.commit()

    created = _create_intake_workflows(matter_id=matter_id, actor_id=None)
    db_session.commit()

    assert created >= 1
    rows = (
        Workflow.query.filter(Workflow.business_code.like(f"INTAKE:{matter_id}%"))
        .order_by(Workflow.id.asc())
        .all()
    )
    assert rows
    assert all((wf.status or "").strip() == "Completed" for wf in rows)
    assert all(wf.completed_date is not None for wf in rows)
    assert all(wf.completed_date.isoformat() == done_ymd for wf in rows)


def test_create_intake_workflows_remains_pending_without_filing_signal(
    app, db_session, sample_matter
):
    from app.models.workflow import Workflow
    from app.services.matter.matter_use_cases import _create_intake_workflows

    matter_id = str(getattr(sample_matter, "_test_matter_id", sample_matter.matter_id))

    created = _create_intake_workflows(matter_id=matter_id, actor_id=None)
    db_session.commit()

    assert created >= 1
    rows = (
        Workflow.query.filter(Workflow.business_code.like(f"INTAKE:{matter_id}%"))
        .order_by(Workflow.id.asc())
        .all()
    )
    assert rows
    assert all((wf.status or "").strip() == "Pending" for wf in rows)
    assert all(wf.completed_date is None for wf in rows)
