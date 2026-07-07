from __future__ import annotations

from datetime import datetime

from app.extensions import db


class WorkflowPlaybookTemplate(db.Model):
    """Reusable workflow playbook for document/task types."""

    __tablename__ = "workflow_playbook_template"
    __table_args__ = (
        db.UniqueConstraint("name", "doc_type", name="uq_workflow_playbook_name_doc_type"),
        db.Index("ix_workflow_playbook_active_doc_type", "is_active", "doc_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.Text, nullable=False)
    doc_type = db.Column(db.String(80), nullable=False, default="", index=True)
    matter_type = db.Column(db.String(80), default="", index=True)
    right_group = db.Column(db.String(80), default="", index=True)
    event_key = db.Column(db.String(120), default="", index=True)
    category = db.Column(db.String(20), default="", index=True)
    description = db.Column(db.Text)

    checklist_json = db.Column(db.JSON, default=list)
    schedule_json = db.Column(db.JSON, default=dict)
    request_template = db.Column(db.Text)
    memo_template = db.Column(db.Text)

    is_active = db.Column(
        db.Boolean, nullable=False, default=True, server_default="true", index=True
    )
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        index=True,
    )
