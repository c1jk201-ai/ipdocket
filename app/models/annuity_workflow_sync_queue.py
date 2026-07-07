from __future__ import annotations

from datetime import datetime

from app.extensions import db


class AnnuityWorkflowSyncQueue(db.Model):
    """
    Durable queue for Annuity -> Workflow rebuild (matter-scoped).

    - Dedup key: matter_id (PK)
    - backoff via next_run_at
    - max attempts + dead-letter on exhaustion
    """

    __tablename__ = "annuity_workflow_sync_queue"

    matter_id = db.Column(db.Text, primary_key=True)

    payload = db.Column(db.Text)  # optional JSON string
    attempts = db.Column(db.Integer, default=0, nullable=False)

    next_run_at = db.Column(db.DateTime, index=True)  # when eligible to run next
    locked_at = db.Column(db.DateTime, index=True)  # soft lock timestamp
    lock_token = db.Column(db.Text)  # debug / lock owner token

    last_error = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True
    )
