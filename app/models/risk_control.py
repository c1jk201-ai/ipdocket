from __future__ import annotations

from datetime import datetime

from app.extensions import db


class MatterRiskFact(db.Model):
    __tablename__ = "matter_risk_facts"

    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), primary_key=True)
    score = db.Column(db.Integer, nullable=False, default=0, index=True)
    risk_level = db.Column(db.String(10), nullable=False, default="LOW", index=True)
    owner_staff_party_id = db.Column(db.Text, index=True)
    attorney_id = db.Column(db.Text, index=True)
    handler_id = db.Column(db.Text, index=True)
    manager_id = db.Column(db.Text, index=True)
    team_key = db.Column(db.Text, index=True)

    deadline_score = db.Column(db.Integer, nullable=False, default=0)
    workflow_score = db.Column(db.Integer, nullable=False, default=0)
    mail_score = db.Column(db.Integer, nullable=False, default=0)
    billing_score = db.Column(db.Integer, nullable=False, default=0)
    automation_score = db.Column(db.Integer, nullable=False, default=0)
    data_quality_score = db.Column(db.Integer, nullable=False, default=0)

    overdue_deadline_count = db.Column(db.Integer, nullable=False, default=0)
    urgent_deadline_count = db.Column(db.Integer, nullable=False, default=0)
    overdue_workflow_count = db.Column(db.Integer, nullable=False, default=0)
    urgent_workflow_count = db.Column(db.Integer, nullable=False, default=0)
    mail_review_count = db.Column(db.Integer, nullable=False, default=0)
    automation_review_count = db.Column(db.Integer, nullable=False, default=0)
    deadline_review_count = db.Column(db.Integer, nullable=False, default=0)
    outstanding_total = db.Column(db.Float, nullable=False, default=0.0)

    next_due_date = db.Column(db.Date, index=True)
    risk_reasons_json = db.Column(db.JSON, default=list)
    facts_json = db.Column(db.JSON, default=dict)
    computed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class DeadlineReviewQueue(db.Model):
    __tablename__ = "deadline_review_queue"
    __table_args__ = (
        db.UniqueConstraint("signature", name="uq_deadline_review_queue_signature"),
        db.Index("ix_deadline_review_queue_status_severity", "status", "severity"),
    )

    id = db.Column(db.Integer, primary_key=True)
    signature = db.Column(db.Text, nullable=False)
    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), nullable=False, index=True)
    docket_id = db.Column(db.Text, index=True)
    workflow_id = db.Column(db.Integer, index=True)
    issue_type = db.Column(db.Text, nullable=False, index=True)
    severity = db.Column(db.String(10), nullable=False, default="MEDIUM", index=True)
    status = db.Column(db.String(20), nullable=False, default="OPEN", index=True)
    rule_version = db.Column(db.Text, nullable=False, default="unknown", index=True)
    source = db.Column(db.Text)
    evidence_json = db.Column(db.JSON, default=dict)
    expected_json = db.Column(db.JSON, default=dict)
    actual_json = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    resolution_note = db.Column(db.Text)
