from __future__ import annotations

from datetime import datetime

from app.extensions import db


class BillingGuardrailFinding(db.Model):
    """Review queue for billing omissions, under-billing, and collection risks."""

    __tablename__ = "billing_guardrail_finding"
    __table_args__ = (
        db.UniqueConstraint("finding_key", name="uq_billing_guardrail_finding_key"),
        db.Index("ix_billing_guardrail_status_severity", "status", "severity"),
    )

    STATUS_OPEN = "open"
    STATUS_REVIEWING = "reviewing"
    STATUS_RESOLVED = "resolved"
    STATUS_DISMISSED = "dismissed"

    id = db.Column(db.Integer, primary_key=True)
    finding_key = db.Column(db.Text, nullable=False)
    finding_type = db.Column(db.String(40), nullable=False, index=True)
    severity = db.Column(db.String(10), nullable=False, default="medium", index=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_OPEN, index=True)

    matter_id = db.Column(db.Text, db.ForeignKey("matter.matter_id"), index=True)
    our_ref = db.Column(db.Text, index=True)
    source_type = db.Column(db.String(30), index=True)
    source_id = db.Column(db.Text, index=True)

    currency = db.Column(db.String(10), default="USD")
    expected_amount_minor = db.Column(db.Integer)
    actual_amount_minor = db.Column(db.Integer)
    gap_amount_minor = db.Column(db.Integer)
    confidence = db.Column(db.Integer, nullable=False, default=70)

    title = db.Column(db.Text, nullable=False)
    detail = db.Column(db.Text)
    evidence_json = db.Column(db.JSON, default=dict)

    first_detected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        index=True,
    )
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    resolution_note = db.Column(db.Text)
