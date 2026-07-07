from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from flask import g, has_request_context
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.operation import Operation, OperationChange


def _current_request_id() -> Optional[str]:
    if not has_request_context():
        return None
    return getattr(g, "request_id", None)


def _current_actor_id() -> Optional[int]:
    try:
        if current_user and current_user.is_authenticated:
            return int(current_user.get_id())
    except Exception:
        return None
    return None


def _namespace_request_id(request_id: Optional[str], actor_id: Optional[int]) -> Optional[str]:
    """
    Namespace user-supplied idempotency keys by actor to avoid cross-user collisions.

    The DB enforces uniqueness on (request_id, action). If request_id is client-supplied
    (e.g., Idempotency-Key), two different users could otherwise collide and either:
      - incorrectly get "duplicate_request" responses, or
      - (worse) receive another user's cached operation summary.
    """
    rid = (request_id or "").strip()
    if not rid:
        return None
    if actor_id is None:
        return rid
    prefix = f"u{int(actor_id)}:"
    if rid.startswith(prefix):
        return rid
    return prefix + rid


def namespace_idempotency_key(
    idempotency_key: Optional[str], actor_id: Optional[int]
) -> Optional[str]:
    """
    Generate a request_id value safe to store in Operation for idempotency purposes.

    Use this only for *client-supplied* idempotency keys (e.g., "Idempotency-Key" header).
    Do not apply it to server-generated request IDs used for observability, since those
    are already unique and may be used to correlate logs across tables.
    """
    return _namespace_request_id(idempotency_key, actor_id)


class OperationRecorder:
    def __init__(
        self,
        action: str,
        *,
        actor_id: Optional[int] = None,
        request_id: Optional[str] = None,
        risk_level: str = "LOW",
        undo_supported: bool = False,
        undo_deadline_at: Optional[datetime] = None,
        targets_json: Optional[dict[str, Any]] = None,
        summary_json: Optional[dict[str, Any]] = None,
        status: str = "prepared",
    ) -> None:
        self.operation = Operation(
            request_id=request_id or _current_request_id(),
            actor_id=actor_id if actor_id is not None else _current_actor_id(),
            action=action,
            risk_level=risk_level,
            status=status,
            undo_supported=undo_supported,
            undo_deadline_at=undo_deadline_at,
            targets_json=targets_json,
            summary_json=summary_json,
            created_at=datetime.utcnow(),
        )

    def begin(self) -> Operation:
        if self.operation.id is None:
            db.session.add(self.operation)
            db.session.flush()
        return self.operation

    def add_change(
        self,
        *,
        entity_type: str,
        entity_id: str,
        change_type: str,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        patch: Optional[dict[str, Any]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> OperationChange:
        self.begin()
        change = OperationChange(
            operation_id=self.operation.id,
            entity_type=entity_type,
            entity_id=entity_id,
            change_type=change_type,
            before_json=before,
            after_json=after,
            patch_json=patch,
            meta_json=meta,
            created_at=datetime.utcnow(),
        )
        db.session.add(change)
        return change

    def mark_applied(self) -> None:
        self.operation.status = "applied"
        self.operation.applied_at = datetime.utcnow()

    def mark_failed(self, error: Optional[str] = None) -> None:
        self.operation.status = "failed"
        if error:
            self.operation.error_text = error

    def mark_partial(self, error: Optional[str] = None) -> None:
        self.operation.status = "partial"
        if error:
            self.operation.error_text = error

    def mark_undone(self) -> None:
        self.operation.status = "undone"
        self.operation.undone_at = datetime.utcnow()


def reserve_operation(
    action: str,
    *,
    actor_id: Optional[int] = None,
    request_id: Optional[str] = None,
    risk_level: str = "LOW",
    undo_supported: bool = False,
    undo_deadline_at: Optional[datetime] = None,
    targets_json: Optional[dict[str, Any]] = None,
    summary_json: Optional[dict[str, Any]] = None,
    status: str = "prepared",
) -> tuple[Operation | None, bool]:
    actor = actor_id if actor_id is not None else _current_actor_id()
    # Only namespace explicit idempotency keys. If no idempotency key is provided, keep
    # the server-generated per-request ID unchanged for log correlation.
    explicit_rid = _namespace_request_id(request_id, actor) if request_id is not None else None
    rid = explicit_rid or _current_request_id()
    if rid:
        existing = (
            Operation.query.filter(Operation.request_id == rid)
            .filter(Operation.action == action)
            .first()
        )
        if existing:
            return existing, False
    # Backward-compat: before namespacing we stored raw idempotency keys. If an explicit key is
    # provided, allow the same actor to hit the legacy row (but do not return another actor's op).
    legacy_raw = (request_id or "").strip() if request_id is not None else ""
    if legacy_raw and actor is not None and rid != legacy_raw:
        legacy = (
            Operation.query.filter(Operation.request_id == legacy_raw)
            .filter(Operation.action == action)
            .filter(Operation.actor_id == actor)
            .first()
        )
        if legacy:
            return legacy, False

    op = Operation(
        request_id=rid,
        actor_id=actor,
        action=action,
        risk_level=risk_level,
        status=status,
        undo_supported=undo_supported,
        undo_deadline_at=undo_deadline_at,
        targets_json=targets_json,
        summary_json=summary_json,
        created_at=datetime.utcnow(),
    )
    db.session.add(op)
    try:
        with db.session.begin_nested():
            db.session.flush()
    except IntegrityError:
        db.session.rollback()
        if rid:
            existing = (
                Operation.query.filter(Operation.request_id == rid)
                .filter(Operation.action == action)
                .first()
            )
            if existing:
                return existing, False
        if legacy_raw and actor is not None and rid != legacy_raw:
            legacy = (
                Operation.query.filter(Operation.request_id == legacy_raw)
                .filter(Operation.action == action)
                .filter(Operation.actor_id == actor)
                .first()
            )
            if legacy:
                return legacy, False
        raise
    return op, True


def mark_operation_applied(
    op: Operation | None,
    *,
    summary_updates: Optional[dict[str, Any]] = None,
) -> None:
    if not op:
        return
    if summary_updates:
        summary = dict(op.summary_json or {})
        summary.update(summary_updates)
        op.summary_json = summary
    op.status = "applied"
    op.applied_at = datetime.utcnow()


def mark_operation_failed(op: Operation | None, *, error: Optional[str] = None) -> None:
    if not op:
        return
    op.status = "failed"
    if error:
        op.error_text = error
