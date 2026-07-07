from __future__ import annotations

from datetime import datetime

from app.extensions import db


class UserAccessLog(db.Model):
    """
    User access/activity logs.

    - Intended for security/operations: "who did what, when" at the request/page level.
    - This table is separate from `audit_log` to avoid polluting business audit trails.
    """

    __tablename__ = "user_access_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    request_id = db.Column(db.Text, index=True)

    method = db.Column(db.String(10), nullable=False, index=True)
    path = db.Column(db.Text, nullable=False)
    endpoint = db.Column(db.Text)
    blueprint = db.Column(db.Text)

    status_code = db.Column(db.Integer, index=True)
    duration_ms = db.Column(db.Integer)

    remote_addr = db.Column(db.Text)
    user_agent = db.Column(db.Text)
    referer = db.Column(db.Text)

    user = db.relationship("User", foreign_keys=[user_id])
