import uuid
from datetime import date


class _DummySession(dict):
    modified: bool = False


def _build_cmd(*, sample_user, client_id: int, our_ref: str):
    from app.services.matter.matter_domain import MatterCreateCommand

    return MatterCreateCommand(
        division="DOM",
        case_type="PATENT",
        form_data={
            "our_ref": our_ref,
            "retained_at": date.today().isoformat(),
            "client_name": "Test Client",
            "client_id": str(client_id),
        },
        files={},
        actor_user_id=getattr(sample_user, "_test_id", None) or sample_user.id,
        idempotency_key=uuid.uuid4().hex,
    )


def test_create_reuses_our_ref_from_soft_deleted_matter(app, db_session, sample_user) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    deleted = Matter(
        matter_id="deleted_ref_holder_1",
        our_ref="26PD0142US",
        right_group="DOM",
        matter_type="PATENT",
        retained_at="2026-02-19",
        entered_at="2026-02-19",
        is_deleted=True,
    )
    db_session.add(deleted)
    db_session.commit()

    store = SessionIdempotencyStore(_DummySession())
    cmd = _build_cmd(sample_user=sample_user, client_id=client.id, our_ref="26PD0142US")
    res = MatterCreateApplyUseCase().execute(cmd, store)

    assert res.success is True
    created = Matter.query.get(str(res.matter_id))
    assert created is not None
    assert created.our_ref == "26PD0142US"

    db_session.refresh(deleted)
    assert deleted.our_ref != "26PD0142US"
    assert deleted.our_ref.startswith("26PD0142US#deleted-")
    assert "26PD0142US" in (deleted.old_our_ref or "")


def test_create_rejects_duplicate_our_ref_when_existing_matter_is_active(
    app, db_session, sample_user
) -> None:
    from app.models.client import Client
    from app.models.ip_records import Matter
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        SessionIdempotencyStore,
    )

    client = Client(name="Test Client")
    db_session.add(client)
    db_session.commit()

    active = Matter(
        matter_id="active_ref_holder_1",
        our_ref="26PD0142US",
        right_group="DOM",
        matter_type="PATENT",
        retained_at="2026-02-19",
        entered_at="2026-02-19",
        is_deleted=False,
    )
    db_session.add(active)
    db_session.commit()

    store = SessionIdempotencyStore(_DummySession())
    cmd = _build_cmd(sample_user=sample_user, client_id=client.id, our_ref="26PD0142US")
    res = MatterCreateApplyUseCase().execute(cmd, store)

    assert res.success is False
    assert "Our Ref." in (res.error or "")
