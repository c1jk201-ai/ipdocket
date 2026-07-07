from __future__ import annotations

from datetime import datetime

from app.extensions import db


class AnnuityWorkflowSyncDeadLetter(db.Model):
    """
    Dead-letter storage for failed annuity workflow rebuilds.
    """

    __tablename__ = "annuity_workflow_sync_dead_letter"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(db.Text, nullable=False, index=True)

    payload = db.Column(db.Text)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text)

    dead_letter_reason = db.Column(db.Text)
    dead_lettered_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
