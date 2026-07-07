from __future__ import annotations

from datetime import datetime

from app.extensions import db


class MatterStatusRecalcQueue(db.Model):
    """
    Durable queue for matter auto-status cache recalculation.

    - Dedup key: matter_id
    - Retries with backoff via next_run_at / attempts
    - Soft lock for concurrent workers via locked_at / lock_token
    """

    __tablename__ = "matter_status_recalc_queue"

    matter_id = db.Column(db.Text, primary_key=True)

    payload = db.Column(db.Text)
    attempts = db.Column(db.Integer, default=0, nullable=False)

    next_run_at = db.Column(db.DateTime, index=True)
    locked_at = db.Column(db.DateTime, index=True)
    lock_token = db.Column(db.Text)

    last_error = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True
    )
