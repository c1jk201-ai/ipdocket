from __future__ import annotations

from datetime import datetime

from app.extensions import db


class CRMClientMergeLog(db.Model):
    """Merge log for CRM Client merges."""

    __tablename__ = "crm_client_merge_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    target_client_id = db.Column(db.Integer, nullable=False, index=True)
    source_client_ids_json = db.Column(db.Text, nullable=False)
    payload_json = db.Column(db.Text, nullable=False)

    merged_by = db.Column(db.Integer, nullable=True, index=True)
    undone_at = db.Column(db.DateTime, nullable=True, index=True)
