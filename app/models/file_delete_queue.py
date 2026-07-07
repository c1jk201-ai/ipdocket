from __future__ import annotations

import uuid
from datetime import datetime

from app.extensions import db


class FileDeleteQueue(db.Model):
    """
    Durable-ish retry queue for failed physical file deletes.

    Rows are created when a DB FileAsset row is deleted but the underlying file could not be removed
    (e.g., transient Windows lock). Housekeeping drains this table and retries deletion.
    """

    __tablename__ = "file_delete_queue"

    delete_id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    file_path = db.Column(db.Text, nullable=False, unique=True)
    file_asset_id = db.Column(db.Text, index=True)

    attempts = db.Column(db.Integer, default=0, nullable=False)
    next_run_at = db.Column(db.DateTime, index=True)

    locked_at = db.Column(db.DateTime, index=True)
    lock_token = db.Column(db.Text)

    last_error = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True
    )
