from __future__ import annotations

import uuid
from datetime import date, datetime


def test_build_history_section_orders_workflows_by_occurrence_then_display_due(
    db_session, sample_matter
):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.workflow import Workflow

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)

    db_session.add_all(
        [
            Workflow(
                case_id=matter_id,
                name="wf-order-backdated",
                status="Pending",
                business_code="MANUAL:wf-order-backdated",
                request_start_date=date(2026, 2, 27),
                due_date=date(2026, 5, 1),
                created_at=datetime(2026, 3, 10, 9, 0, 0),
            ),
            Workflow(
                case_id=matter_id,
                name="wf-order-older-event",
                status="Pending",
                business_code="MANUAL:wf-order-older-event",
                due_date=date(2026, 5, 10),
                created_at=datetime(2026, 3, 1, 9, 0, 0),
            ),
            Workflow(
                case_id=matter_id,
                name="wf-order-same-day-legal-early",
                status="Pending",
                business_code="MANUAL:wf-order-same-day-legal-early",
                due_date=date(2026, 4, 4),
                legal_due_date=date(2026, 4, 4),
                created_at=datetime(2026, 3, 5, 9, 0, 0),
            ),
            Workflow(
                case_id=matter_id,
                name="wf-order-same-day-legal-late",
                status="Pending",
                business_code="MANUAL:wf-order-same-day-legal-late",
                due_date=date(2026, 4, 3),
                legal_due_date=date(2026, 4, 10),
                created_at=datetime(2026, 3, 5, 8, 0, 0),
            ),
        ]
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": matter_id})
    ordered_names = [
        wf.name for wf in (out.get("workflows") or []) if wf.name.startswith("wf-order-")
    ]

    assert ordered_names == [
        "wf-order-backdated",
        "wf-order-older-event",
        "wf-order-same-day-legal-early",
        "wf-order-same-day-legal-late",
    ]


def test_build_history_section_prefers_workflow_display_values_for_linked_workflows(
    db_session, sample_matter
):
    from app.blueprints.case.services.detail_context import _build_history_section
    from app.models.docket import DocketItem
    from app.models.workflow import Workflow

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    docket_id = uuid.uuid4().hex

    db_session.add(
        DocketItem(
            docket_id=docket_id,
            matter_id=matter_id,
            category="MGMT",
            name_ref="MGMT:STATUS_RED:Text",
            name_free="Text/Text",
            due_date="2026-06-17",
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Text",
            status="Pending",
            business_code=f"DOCKET:{docket_id}",
            due_date=date(2026, 4, 8),
            legal_due_date=date(2026, 4, 8),
            created_at=datetime(2026, 3, 17, 8, 34, 40),
        )
    )
    db_session.commit()

    out = _build_history_section({"matter": sample_matter, "overview": None, "_mid_str": matter_id})
    workflow = next(
        wf
        for wf in (out.get("workflows") or [])
        if getattr(wf, "business_code", None) == f"DOCKET:{docket_id}"
    )

    assert workflow._display_name == "Text"
    assert workflow._display_legal_due_date == date(2026, 4, 8)
    assert workflow._display_due_date == date(2026, 4, 8)
