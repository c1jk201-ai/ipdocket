import uuid


class _DummySession(dict):
    # Flask session objects expose `.modified`; SessionIdempotencyStore relies on it.
    modified: bool = False


def test_prepare_out_patent_defaults_exam_requested(app, sample_user) -> None:
    from app.services.matter.matter_domain import MatterCreatePrepareCommand
    from app.services.matter.matter_use_cases import (
        MatterCreatePrepareUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore(_DummySession())
    cmd = MatterCreatePrepareCommand(division="OUT", case_type="PATENT", raw_args={})
    res = MatterCreatePrepareUseCase().execute(cmd, store)

    assert res.prefill.get("exam_requested") == "Y"
    assert res.prefill.get("filing_deadline_type") == "INTERNAL"


def test_create_out_patent_us_forces_exam_requested_and_sets_exam_request_date(
    app, db_session, sample_user
) -> None:
    from datetime import date

    from app.models.client import Client
    from app.models.ip_records import MatterCustomField
    from app.services.matter.matter_domain import MatterCreateCommand
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore(_DummySession())
    app_date = date(2026, 1, 2).isoformat()
    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = MatterCreateCommand(
        division="OUT",
        case_type="PATENT",
        form_data={
            "our_ref": f"TESTOUT{uuid.uuid4().hex[:8].upper()}US",
            "retained_at": date.today().isoformat(),
            "client_name": client.name,
            "client_id": str(client.id),
            "application_country": "US",
            "application_date": app_date,
            "exam_requested": "N",
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )

    res = MatterCreateApplyUseCase().execute(cmd, store)
    assert res.success is True
    assert res.matter_id

    row = MatterCustomField.query.filter_by(
        matter_id=str(res.matter_id), namespace="outgoing_patent"
    ).first()
    assert row is not None
    data = row.data or {}
    assert data.get("exam_requested") == "Y"
    assert data.get("exam_request_date") == app_date


def test_create_out_trademark_defaults_exam_requested_without_exam_request_date(
    app, db_session, sample_user
) -> None:
    from datetime import date

    from app.models.client import Client
    from app.models.ip_records import MatterCustomField
    from app.services.matter.matter_domain import MatterCreateCommand
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore(_DummySession())
    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    cmd = MatterCreateCommand(
        division="OUT",
        case_type="TRADEMARK",
        form_data={
            "our_ref": f"TESTOUT{uuid.uuid4().hex[:8].upper()}TM",
            "retained_at": date.today().isoformat(),
            "client_name": client.name,
            "client_id": str(client.id),
            "application_country": "US",
            "application_date": date(2026, 1, 2).isoformat(),
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )

    res = MatterCreateApplyUseCase().execute(cmd, store)
    assert res.success is True
    assert res.matter_id

    row = MatterCustomField.query.filter_by(
        matter_id=str(res.matter_id), namespace="outgoing_trademark"
    ).first()
    assert row is not None
    data = row.data or {}
    assert data.get("exam_requested") == "Y"
    assert "exam_request_date" not in data


def test_apply_exam_request_date_default_when_requested_for_non_out_case() -> None:
    from app.services.deadlines.exam_request_rules import (
        apply_exam_request_date_default_when_requested,
    )

    data = {
        "application_date": "2026-02-15",
        "exam_requested": "Yes",
        "exam_request_date": "",
    }
    apply_exam_request_date_default_when_requested(
        data,
        allowed_keys={"application_date", "exam_requested", "exam_request_date"},
    )
    assert data.get("exam_request_date") == "2026-02-15"


def test_apply_exam_request_date_default_when_requested_preserves_manual_date() -> None:
    from app.services.deadlines.exam_request_rules import (
        apply_exam_request_date_default_when_requested,
    )

    data = {
        "application_date": "2026-02-15",
        "exam_requested": "Y",
        "exam_request_date": "2026-02-20",
    }
    apply_exam_request_date_default_when_requested(
        data,
        allowed_keys={"application_date", "exam_requested", "exam_request_date"},
    )
    assert data.get("exam_request_date") == "2026-02-20"


def test_create_dom_patent_defaults_exam_request_date_when_exam_requested_yes(
    app, db_session, sample_user
) -> None:
    from datetime import date

    from app.models.client import Client
    from app.models.ip_records import MatterCustomField
    from app.services.matter.matter_domain import MatterCreateCommand
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore(_DummySession())
    app_date = date(2026, 2, 16).isoformat()
    client = Client(name="DOM Client")
    db_session.add(client)
    db_session.commit()

    cmd = MatterCreateCommand(
        division="DOM",
        case_type="PATENT",
        form_data={
            "our_ref": f"26PD{uuid.uuid4().hex[:4].upper()}US",
            "retained_at": date.today().isoformat(),
            "client_name": client.name,
            "client_id": str(client.id),
            "application_date": app_date,
            "exam_requested": "Yes",
            "exam_request_date": "",
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )

    res = MatterCreateApplyUseCase().execute(cmd, store)
    assert res.success is True
    assert res.matter_id

    row = MatterCustomField.query.filter_by(
        matter_id=str(res.matter_id), namespace="domestic_patent"
    ).first()
    assert row is not None
    data = row.data or {}
    assert str(data.get("exam_requested") or "").strip().lower() in {"y", "yes", "true", "1"}
    assert data.get("exam_request_date") == app_date


def test_create_defaults_filing_deadline_type_to_internal_when_missing(
    app, db_session, sample_user
) -> None:
    from datetime import date

    from app.models.client import Client
    from app.models.docket import DocketItem
    from app.services.matter.matter_domain import MatterCreateCommand
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore(_DummySession())
    client = Client(name="Deadline Type Client")
    db_session.add(client)
    db_session.commit()

    filing_deadline = "2026-03-30"
    cmd = MatterCreateCommand(
        division="DOM",
        case_type="PATENT",
        form_data={
            "our_ref": f"26PD{uuid.uuid4().hex[:4].upper()}US",
            "retained_at": date.today().isoformat(),
            "client_name": client.name,
            "client_id": str(client.id),
            "filing_deadline": filing_deadline,
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )

    res = MatterCreateApplyUseCase().execute(cmd, store)
    assert res.success is True
    assert res.matter_id

    filing = (
        DocketItem.query.filter_by(matter_id=str(res.matter_id), name_ref="Filing")
        .order_by(DocketItem.docket_id.desc())
        .first()
    )
    assert filing is not None
    assert filing.name_free == "Filing Deadline"
    assert (filing.due_date or "") == ""
    assert filing.extended_due_date == filing_deadline
