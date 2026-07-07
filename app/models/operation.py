from datetime import datetime

from app.extensions import db


class Operation(db.Model):
    __tablename__ = "operations"
    __table_args__ = (
        db.UniqueConstraint("request_id", "action", name="uq_operations_request_action"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Text, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.Text, nullable=False, index=True)
    risk_level = db.Column(db.String(10), nullable=False, default="LOW", index=True)
    status = db.Column(db.String(20), nullable=False, default="prepared", index=True)
    undo_supported = db.Column(db.Boolean, default=False)
    undo_deadline_at = db.Column(db.DateTime, index=True)
    targets_json = db.Column(db.JSON)
    summary_json = db.Column(db.JSON)
    error_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    applied_at = db.Column(db.DateTime)
    undone_at = db.Column(db.DateTime)

    actor = db.relationship("User", foreign_keys=[actor_id])
    changes = db.relationship(
        "OperationChange",
        back_populates="operation",
        cascade="all, delete-orphan",
    )


class OperationChange(db.Model):
    __tablename__ = "operation_changes"
    __table_args__ = (db.Index("ix_operation_changes_entity", "entity_type", "entity_id"),)

    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey("operations.id"), nullable=False, index=True)
    entity_type = db.Column(db.Text, nullable=False)
    entity_id = db.Column(db.Text, nullable=False)
    change_type = db.Column(db.String(30), nullable=False)
    before_json = db.Column(db.JSON)
    after_json = db.Column(db.JSON)
    patch_json = db.Column(db.JSON)
    meta_json = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    operation = db.relationship("Operation", back_populates="changes")
