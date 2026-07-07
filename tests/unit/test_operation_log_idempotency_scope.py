from __future__ import annotations


def test_reserve_operation_namespaces_idempotency_key_by_actor(
    app, db_session, sample_user, admin_user
):
    """
    Regression/P1: operations were deduped globally by (request_id, action) with no actor scoping.
    A colliding Idempotency-Key could cause cross-user "duplicate_request" behavior and leak cached summaries.
    """
    from app.models.operation import Operation
    from app.services.ops.operation_log import reserve_operation

    raw_request_id = "idem-key-123"
    action = "test.idempotency"

    op1, created1 = reserve_operation(
        action,
        request_id=raw_request_id,
        actor_id=int(sample_user.id),
        summary_json={"v": 1},
    )
    assert created1 is True
    db_session.commit()

    op1_db = db_session.get(Operation, op1.id)
    assert op1_db is not None
    assert (op1_db.request_id or "").startswith(f"u{int(sample_user.id)}:")

    # Same actor + same raw key => duplicate (returns existing op)
    op1_again, created1_again = reserve_operation(
        action,
        request_id=raw_request_id,
        actor_id=int(sample_user.id),
    )
    assert created1_again is False
    assert op1_again.id == op1.id

    # Different actor + same raw key should not collide.
    op2, created2 = reserve_operation(
        action,
        request_id=raw_request_id,
        actor_id=int(admin_user.id),
        summary_json={"v": 2},
    )
    assert created2 is True
    db_session.commit()

    op2_db = db_session.get(Operation, op2.id)
    assert op2_db is not None
    assert op2_db.id != op1.id
    assert (op2_db.request_id or "").startswith(f"u{int(admin_user.id)}:")


def test_reserve_operation_does_not_namespace_server_request_id(app, db_session, sample_user):
    """
    Regression guard: reserve_operation() should not rewrite server-generated per-request IDs,
    since those are used for observability/log correlation and are already unique.
    """
    from flask import g

    from app.models.operation import Operation
    from app.services.ops.operation_log import reserve_operation

    action = "test.server_request_id"
    with app.test_request_context("/"):
        g.request_id = "req-abc123"
        op, created = reserve_operation(action, actor_id=int(sample_user.id))
        assert created is True
        db_session.commit()

    op_db = db_session.get(Operation, op.id)
    assert op_db is not None
    assert op_db.request_id == "req-abc123"


def test_reserve_operation_falls_back_to_legacy_raw_key_for_same_actor(
    app, db_session, sample_user
):
    """
    Backward-compat: before namespacing, operations stored raw idempotency keys. A repeated request
    should still be treated as duplicate for the same actor.
    """
    from datetime import datetime

    from app.models.operation import Operation
    from app.services.ops.operation_log import reserve_operation

    legacy_key = "legacy-idem-key"
    action = "test.legacy_idempotency"

    legacy_op = Operation(
        request_id=legacy_key,
        actor_id=int(sample_user.id),
        action=action,
        risk_level="LOW",
        status="applied",
        summary_json={"v": 1},
        created_at=datetime.utcnow(),
        applied_at=datetime.utcnow(),
    )
    db_session.add(legacy_op)
    db_session.commit()

    op, created = reserve_operation(action, request_id=legacy_key, actor_id=int(sample_user.id))
    assert created is False
    assert op.id == legacy_op.id
