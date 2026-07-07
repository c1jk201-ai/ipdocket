from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.extensions import db


class WorkflowAssignmentRequest(db.Model):
    __tablename__ = "workflow_assignment_requests"
    __table_args__ = (
        db.Index("ix_workflow_assignment_requests_workflow_id", "workflow_id"),
        db.Index("ix_workflow_assignment_requests_target_user_id", "target_user_id"),
        db.Index("ix_workflow_assignment_requests_requested_by_id", "requested_by_id"),
        db.Index("ix_workflow_assignment_requests_status", "status"),
        db.Index("ix_workflow_assignment_requests_requested_at", "requested_at"),
        db.Index(
            "ux_workflow_assignment_requests_pending_role",
            "workflow_id",
            "role_code",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUSES = frozenset({STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_CANCELLED})

    ROLE_HANDLER = "handler"
    ROLE_ATTORNEY = "attorney"
    ROLE_MANAGER = "manager"
    ROLE_CODES = frozenset({ROLE_HANDLER, ROLE_ATTORNEY, ROLE_MANAGER})

    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(
        db.Integer,
        db.ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_code = db.Column(db.String(20), nullable=False)
    previous_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False)
    source = db.Column(db.String(40))
    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    responded_at = db.Column(db.DateTime)
    response_note = db.Column(db.Text)

    workflow = db.relationship(
        "Workflow", backref=db.backref("assignment_requests", lazy="dynamic")
    )
    previous_user = db.relationship("User", foreign_keys=[previous_user_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])

    def __repr__(self) -> str:
        return (
            f"<WorkflowAssignmentRequest workflow={self.workflow_id} "
            f"role={self.role_code} status={self.status}>"
        )
