from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from app.extensions import db


class UndoAction(db.Model):
    """
    1 Undo    
    - 'Create//Delete/Change'   Actions    
    - MVP: inserts In Progress( from Document  Apply )
    """

    __tablename__ = "undo_action"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    user_id = db.Column(db.String(64), nullable=False, index=True)
    action_type = db.Column(db.String(40), nullable=False, index=True)

    snapshot_json = db.Column(db.Text, nullable=False, default="{}")

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    undone_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        action_type: str,
        snapshot: dict,
        ttl_seconds: int = 600,
    ) -> "UndoAction":
        now = datetime.utcnow()
        token = secrets.token_urlsafe(24)
        row = cls(
            token=token,
            user_id=str(user_id),
            action_type=action_type,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            is_active=True,
        )
        db.session.add(row)
        return row

    def snapshot(self) -> dict:
        try:
            return json.loads(self.snapshot_json or "{}")
        except Exception:
            return {}
