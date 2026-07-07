from __future__ import annotations

import uuid
from datetime import datetime

from app.extensions import db


class UserSavedView(db.Model):
    __tablename__ = "user_saved_view"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    scope = db.Column(db.Text, nullable=False, default="private")
    # For team-scoped views, this stores the scope key (e.g. department/team identifier).
    # For private views, this is NULL.
    scope_key = db.Column(db.Text, index=True)
    module = db.Column(db.Text, nullable=False, index=True)
    name = db.Column(db.Text, nullable=False)
    payload_json = db.Column(db.JSON, nullable=False, default=dict)
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.Index("ix_user_saved_view_user_module", "user_id", "module"),
        db.Index("ix_user_saved_view_scope_scope_key_module", "scope", "scope_key", "module"),
    )
