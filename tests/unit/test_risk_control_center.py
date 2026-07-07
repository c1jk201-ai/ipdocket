from datetime import date


def test_deadline_verification_creates_review_queue_for_workflow_mismatch(app, db_session):
    from app.models.ip_records import DeadlineReviewQueue, DocketItem, Matter
    from app.models.workflow import Workflow
    from app.services.deadlines.deadline_verification import verify_deadlines_for_matter

    matter_id = "RISK-DL-1"
    db_session.add(Matter(matter_id=matter_id, our_ref="RISK-DL-1", right_name="Deadline Risk"))
    docket = DocketItem(
        docket_id="risk-docket-1",
        matter_id=matter_id,
        category="WORK",
        name_ref="OA_RESPONSE",
        due_date="2026-05-10",
    )
    db_session.add(docket)
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="OA response",
            business_code="DOCKET:risk-docket-1",
            status="Pending",
            due_date=date(2026, 5, 20),
            legal_due_date=date(2026, 5, 10),
        )
    )
    db_session.commit()

    result = verify_deadlines_for_matter(matter_id, commit=True)

    assert result["open_issues"] >= 1
    issue = DeadlineReviewQueue.query.filter_by(
        matter_id=matter_id,
        issue_type="deadline.workflow_docket_recalc_mismatch",
        status="OPEN",
    ).first()
    assert issue is not None
    assert issue.expected_json["due_date"] == "2026-05-10"
    assert issue.actual_json["due_date"] == "2026-05-20"


def test_refresh_matter_risk_facts_scores_combined_risks(app, db_session):
    from app.models.case_flat_index import CaseFlatIndex
    from app.models.ip_records import DeadlineReviewQueue, DocketItem, Matter, MatterRiskFact
    from app.models.workflow import Workflow
    from app.services.matter.matter_risk_service import refresh_matter_risk_facts

    matter_id = "RISK-MATTER-1"
    db_session.add(
        Matter(
            matter_id=matter_id,
            our_ref="RISK-MATTER-1",
            right_name="Combined Risk",
            retained_at="2026-01-01",
        )
    )
    db_session.add(
        CaseFlatIndex(
            matter_id=matter_id,
            handler="Handler A",
            handler_id="STAFF-H",
            department="PAT",
        )
    )
    db_session.add(
        DocketItem(
            docket_id="risk-docket-overdue",
            matter_id=matter_id,
            category="WORK",
            name_ref="OVERDUE",
            due_date="2026-05-01",
        )
    )
    db_session.add(
        Workflow(
            case_id=matter_id,
            name="Urgent workflow",
            status="Pending",
            due_date=date(2026, 5, 13),
        )
    )
    db_session.add(
        DeadlineReviewQueue(
            signature="risk-review-1",
            matter_id=matter_id,
            issue_type="deadline.test",
            severity="HIGH",
            status="OPEN",
            rule_version="test",
        )
    )
    db_session.commit()

    result = refresh_matter_risk_facts(
        matter_ids=[matter_id],
        as_of=date(2026, 5, 11),
        commit=True,
    )

    fact = db_session.get(MatterRiskFact, matter_id)
    assert result["updated"] == 1
    assert fact is not None
    assert fact.owner_staff_party_id == "STAFF-H"
    assert fact.overdue_deadline_count == 1
    assert fact.urgent_workflow_count == 1
    assert fact.deadline_review_count == 1
    assert fact.score >= 60
    assert fact.risk_level in {"HIGH", "CRITICAL"}


def test_automation_feedback_records_field_labels_and_metrics(app, db_session):
    from app.models.ip_records import AutomationFieldFeedback
    from app.services.automation.review_feedback import (
        ACTION_UPDATE_PAYLOAD,
        LABEL_CORRECTED,
        collect_doc_type_feedback_metrics,
        record_automation_feedback,
    )

    before = {
        "doc": {"doc_type": "office_action"},
        "params": {"response_deadline": "2026-05-10"},
        "evidence_map": {"params.response_deadline": {"snippet": "May 10"}},
    }
    after = {
        "doc": {"doc_type": "office_action"},
        "params": {"response_deadline": "2026-05-12"},
        "evidence_map": {"params.response_deadline": {"snippet": "May 12"}},
    }

    feedback = record_automation_feedback(
        run_id="field-run-1",
        action=ACTION_UPDATE_PAYLOAD,
        label=LABEL_CORRECTED,
        before_json=before,
        after_json=after,
    )
    db_session.commit()

    rows = AutomationFieldFeedback.query.filter_by(feedback_id=feedback.id).all()
    assert [row.field_path for row in rows] == ["params.response_deadline"]
    assert rows[0].label == LABEL_CORRECTED
    assert rows[0].evidence_present is True

    metrics = collect_doc_type_feedback_metrics(window_days=30)
    field_metrics = metrics["doc_types"]["office_action"]["fields"]["params.response_deadline"]
    assert field_metrics["corrected"] == 1
    assert field_metrics["missing_evidence_rate"] == 0.0


def test_risk_center_page_can_be_disabled_by_feature_flag(admin_client, db_session, monkeypatch):
    from app.models.ip_records import Matter
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService

    monkeypatch.delenv("RISK_CENTER_ENABLED", raising=False)
    SystemConfig.set_config("RISK_CENTER_ENABLED", "0")
    db_session.commit()
    ConfigService.clear_cache()

    db_session.add(Matter(matter_id="RISK-PAGE-1", our_ref="RISK-PAGE-1", right_name="Page"))
    db_session.commit()

    response = admin_client.get("/risk-center")

    assert response.status_code == 404


def test_risk_center_staff_scope_hides_unassigned_matters(
    authenticated_client,
    sample_user,
    db_session,
    monkeypatch,
):
    from app.models.ip_records import Matter, MatterRiskFact, MatterStaffAssignment
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService

    monkeypatch.delenv("RISK_CENTER_ENABLED", raising=False)
    SystemConfig.set_config("RISK_CENTER_ENABLED", "1")
    user = db_session.merge(sample_user)
    user.role = "patent_staff"
    user.staff_party_id = "STAFF-RISK-A"
    db_session.add_all(
        [
            Matter(matter_id="RISK-OWN-1", our_ref="RISK-OWN-1", right_name="Visible Risk"),
            Matter(matter_id="RISK-OTHER-1", our_ref="RISK-OTHER-1", right_name="Hidden Risk"),
            MatterStaffAssignment(
                matter_id="RISK-OWN-1",
                staff_party_id="STAFF-RISK-A",
                staff_role_code="attorney",
            ),
            MatterRiskFact(
                matter_id="RISK-OWN-1",
                score=80,
                risk_level="HIGH",
                owner_staff_party_id="STAFF-RISK-A",
                attorney_id="STAFF-RISK-A",
                team_key="PAT",
            ),
            MatterRiskFact(
                matter_id="RISK-OTHER-1",
                score=90,
                risk_level="CRITICAL",
                owner_staff_party_id="STAFF-RISK-B",
                attorney_id="STAFF-RISK-B",
                team_key="PAT",
            ),
        ]
    )
    db_session.commit()
    ConfigService.clear_cache()

    response = authenticated_client.get("/risk-centerNewscope=all&owner=STAFF-RISK-B")

    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "RISK-OWN-1" in html
    assert "RISK-OTHER-1" not in html


def test_risk_center_resolve_rejects_inaccessible_deadline_review(
    authenticated_client,
    sample_user,
    db_session,
    monkeypatch,
):
    from app.models.ip_records import DeadlineReviewQueue, Matter, MatterStaffAssignment
    from app.models.system_config import SystemConfig
    from app.services.core.config_service import ConfigService

    monkeypatch.delenv("RISK_CENTER_ENABLED", raising=False)
    SystemConfig.set_config("RISK_CENTER_ENABLED", "1")
    user = db_session.merge(sample_user)
    user.role = "patent_staff"
    user.staff_party_id = "STAFF-RISK-A"
    db_session.add_all(
        [
            Matter(matter_id="RISK-OWN-2", our_ref="RISK-OWN-2", right_name="Own"),
            Matter(matter_id="RISK-OTHER-2", our_ref="RISK-OTHER-2", right_name="Other"),
            MatterStaffAssignment(
                matter_id="RISK-OWN-2",
                staff_party_id="STAFF-RISK-A",
                staff_role_code="attorney",
            ),
            DeadlineReviewQueue(
                signature="risk-review-forbidden",
                matter_id="RISK-OTHER-2",
                issue_type="deadline.test",
                severity="HIGH",
                status="OPEN",
                rule_version="test",
            ),
        ]
    )
    db_session.commit()
    ConfigService.clear_cache()
    review = DeadlineReviewQueue.query.filter_by(signature="risk-review-forbidden").one()

    response = authenticated_client.post(
        f"/risk-center/deadline-reviews/{review.id}/resolve",
        data={"note": "nope"},
    )

    assert response.status_code == 403
    db_session.refresh(review)
    assert review.status == "OPEN"
