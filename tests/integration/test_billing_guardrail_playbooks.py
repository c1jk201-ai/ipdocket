from __future__ import annotations

import uuid
from datetime import date


def test_guardrail_detects_unbilled_ipm_expense(app, db_session, clean_legacy_invoice_db):
    from app.models.ip_records import Matter, LegacyExpense
    from app.services.billing.guardrail_service import build_current_findings

    matter_id = uuid.uuid4().hex
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="26GR0001US",
            right_name="Guardrail Matter",
            matter_type="PATENT",
        )
    )
    db_session.add(
        LegacyExpense(
            expense_id=f"EXP-{uuid.uuid4().hex[:8]}",
            matter_id=matter_id,
            expense_ref="DN-001",
            dn_date="2026-05-01",
            currency="USD",
            requested_total=150000,
            description="Text Text",
        )
    )
    db_session.commit()

    findings = build_current_findings(limit_per_source=20)

    assert any(
        row["finding_type"] == "unbilled_expense"
        and row["matter_id"] == matter_id
        and row["gap_amount_minor"] == 150000
        for row in findings
    )


def test_playbook_apply_adds_checklist_and_missing_dates(
    app, db_session, sample_matter, admin_user
):
    from app.models.workflow import Workflow
    from app.models.workflow_checklist import WorkflowChecklistItem
    from app.models.workflow_playbook import WorkflowPlaybookTemplate
    from app.services.workflow.playbook_service import apply_template_to_workflow

    matter_id = getattr(sample_matter, "_test_matter_id", None) or str(sample_matter.matter_id)
    wf = Workflow(
        case_id=matter_id,
        name="OA response",
        status=Workflow.STATUS_PENDING,
        request_start_date=date(2026, 5, 11),
    )
    db_session.add(wf)
    db_session.flush()

    tpl = WorkflowPlaybookTemplate(
        name="OA Text Text",
        doc_type="OA",
        category="WORK",
        checklist_json=["Text Text Text", "Text Text", "Text Text Extra"],
        schedule_json={"internal_due_offset_days": 14, "draft_due_offset_days": 7},
        request_template="{our_ref} {workflow_name} Text Text",
        memo_template="{doc_type} Text Playbook Text",
        is_active=True,
    )
    db_session.add(tpl)
    db_session.commit()

    result = apply_template_to_workflow(
        template=tpl,
        workflow=wf,
        actor_id=getattr(admin_user, "_test_id", None) or admin_user.id,
        base_date=date(2026, 5, 11),
    )

    assert result.checklist_created == 3
    assert result.fields_updated >= 3
    assert wf.due_date == date(2026, 5, 25)
    assert wf.draft_due_date == date(2026, 5, 18)
    assert "OA response" in (wf.note or "")
    assert WorkflowChecklistItem.query.filter_by(workflow_id=wf.id).count() == 3


def test_new_pages_render(admin_client, db_session, clean_legacy_invoice_db):
    responses = [
        admin_client.get("/accounting/invoice-system/guardrail/Newrefresh=0"),
        admin_client.get("/workflow/playbooks"),
        admin_client.get("/business/executive"),
    ]
    assert [resp.status_code for resp in responses] == [200, 200, 200]


def test_playbook_management_is_admin_only(authenticated_client):
    response = authenticated_client.get("/workflow/playbooks")

    assert response.status_code == 403


def test_playbook_management_hidden_from_deadline_menu(authenticated_client):
    response = authenticated_client.get("/deadline/calendar/month")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Text Playbook" not in body
    assert "/workflow/playbooks" not in body
