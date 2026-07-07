from __future__ import annotations

import uuid
from datetime import datetime

from app.extensions import db


class UserUiPreference(db.Model):
    __tablename__ = "user_ui_preference"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    key = db.Column(db.Text, nullable=False, index=True)
    value = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_user_ui_preference_user_key"),)


class AutomationReviewTemplate(db.Model):
    __tablename__ = "automation_review_template"

    id = db.Column(db.Text, primary_key=True, default=lambda: uuid.uuid4().hex)

    # "user" or "team"
    scope = db.Column(db.Text, nullable=False, default="user")
    scope_key = db.Column(db.Text, nullable=False)

    from_domain = db.Column(db.Text, index=True)
    doc_type = db.Column(db.Text, index=True)
    jurisdiction = db.Column(db.Text, index=True)
    label = db.Column(db.Text)

    template_json = db.Column(db.JSON)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_by = db.Column(db.Integer, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "scope",
            "scope_key",
            "from_domain",
            "doc_type",
            "jurisdiction",
            name="uq_automation_review_template_key",
        ),
    )
