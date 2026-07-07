import uuid


def test_matter_create_requires_retained_at(app, sample_user) -> None:
    from app.services.matter.matter_domain import MatterCreateCommand
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    store = SessionIdempotencyStore({})
    cmd = MatterCreateCommand(
        division="DOM",
        case_type="PATENT",
        form_data={
            "client_name": "Text",
            "client_id": "1",
            "our_ref": f"TESTPD{uuid.uuid4().hex[:8].upper()}US",
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )

    res = MatterCreateApplyUseCase().execute(cmd, store)

    assert res.success is False
    assert res.validation_errors
    assert any((e.get("key") == "retained_at") for e in res.validation_errors)
