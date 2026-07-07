from datetime import date


def test_legacy_case_workflows_resolve_via_matter_ref(db_session):
    from app.models.case import Case
    from app.models.matter import Matter
    from app.models.workflow import Workflow

    case = Case(ref_no="25PA0001US", title="Legacy case", right_type="PATENT")
    matter = Matter(
        matter_id="matter_for_legacy_case",
        our_ref="25PA0001US",
        right_name="Matter case",
        matter_type="PATENT",
        is_deleted=False,
    )
    workflow = Workflow(
        case_id="matter_for_legacy_case",
        name="OA response",
        due_date=date(2026, 1, 31),
    )
    db_session.add_all([case, matter, workflow])
    db_session.commit()

    loaded = Case.query.filter_by(ref_no="25PA0001US").one()
    workflows = loaded.workflows.order_by(Workflow.due_date.desc()).all()

    assert [wf.id for wf in workflows] == [workflow.id]


def test_legacy_case_workflows_resolve_via_old_matter_ref(db_session):
    from app.models.case import Case
    from app.models.matter import Matter
    from app.models.workflow import Workflow

    case = Case(ref_no="OLD-REF-1", title="Legacy case", right_type="PATENT")
    matter = Matter(
        matter_id="matter_for_old_ref",
        our_ref="25PA0002US",
        old_our_ref="OLD-REF-1",
        right_name="Matter case",
        matter_type="PATENT",
        is_deleted=False,
    )
    workflow = Workflow(case_id="matter_for_old_ref", name="Review")
    db_session.add_all([case, matter, workflow])
    db_session.commit()

    loaded = Case.query.filter_by(ref_no="OLD-REF-1").one()

    assert [wf.id for wf in loaded.workflows.all()] == [workflow.id]
