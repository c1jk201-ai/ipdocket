from datetime import datetime

from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    request_id = db.Column(db.Text, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    user_id = db.Column(db.Integer, index=True)  # legacy column for compatibility
    action = db.Column(db.Text, nullable=False, index=True)
    target_type = db.Column(db.Text, index=True)
    target_id = db.Column(db.Integer, index=True)
    meta_json = db.Column("meta", db.Text)

    actor = db.relationship("User", foreign_keys=[actor_id])
