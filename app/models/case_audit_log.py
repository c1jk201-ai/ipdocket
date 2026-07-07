from datetime import datetime

from app.extensions import db


class CaseAuditLog(db.Model):
    __tablename__ = "case_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Text, nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.Text, nullable=False, default="PATCH", index=True)
    field_name = db.Column(db.Text, nullable=False, index=True)
    old_value = db.Column(db.JSON)
    new_value = db.Column(db.JSON)
    request_id = db.Column(db.Text, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    actor = db.relationship("User", foreign_keys=[actor_user_id])
