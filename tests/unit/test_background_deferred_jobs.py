from datetime import datetime


def test_customer_llm_parse_endpoint_queues_job(
    app, db_session, authenticated_client, sample_user, monkeypatch
):
    from app.models.operation import Operation
    from app.ops.models import DurableJob

    monkeypatch.setattr(
        "app.blueprints.crm.routes.get_openai_api_key",
        lambda allow_legacy=False: "test-key",
    )

    response = authenticated_client.post(
        "/crm/clients/parse-customer-llm",
        json={"email_text": "ACME Corp / contact@example.com"},
    )

    assert response.status_code == 202
    data = response.get_json()
    assert data["success"] is True
    assert data["queued"] is True
    assert data["operation_id"]
    assert data["status_url"].endswith(f"/crm/clients/parse-customer-llm/{data['operation_id']}")

    op = db_session.get(Operation, int(data["operation_id"]))
    assert op is not None
    assert op.action == "client.parse_customer_llm"
    assert op.status == "queued"
    assert op.actor_id == sample_user.id

    job = DurableJob.query.filter_by(task="client.parse_customer_llm").one()
    assert job.queue == "deferred"
    assert job.payload["operation_id"] == op.id


def test_customer_llm_parse_status_returns_completed_result(
    app, db_session, authenticated_client, sample_user
):
    from app.models.operation import Operation

    op = Operation(
        actor_id=sample_user.id,
        action="client.parse_customer_llm",
        risk_level="LOW",
        status="succeeded",
        summary_json={"customer": {"name": "ACME", "email": "contact@example.com"}},
        created_at=datetime.utcnow(),
        applied_at=datetime.utcnow(),
    )
    db_session.add(op)
    db_session.commit()

    response = authenticated_client.get(f"/crm/clients/parse-customer-llm/{op.id}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["status"] == "succeeded"
    assert data["customer"]["name"] == "ACME"


def test_delete_workflow_background_removes_workflow(app, db_session, sample_matter, monkeypatch):
    from app.models.workflow import Workflow
    from app.services.workflow import deletion_jobs

    wf = Workflow(
        case_id=sample_matter.matter_id,
        name="Delete in background",
        status="Pending",
    )
    db_session.add(wf)
    db_session.commit()
    wf_id = wf.id

    monkeypatch.setattr(deletion_jobs, "delete_workflow_from_google", lambda wf: 0)
    monkeypatch.setattr(deletion_jobs, "delete_workflow_fk_children", lambda workflow_id: None)

    deletion_jobs.delete_workflow_background(wf_id, thread_key=["", "", ""], actor_id=None)

    assert db_session.get(Workflow, wf_id) is None
