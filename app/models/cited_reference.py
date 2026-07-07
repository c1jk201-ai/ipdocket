from __future__ import annotations

from app.extensions import db
from app.utils.timezone import utcnow_naive


class CitedReference(db.Model):
    __tablename__ = "cited_reference"

    id = db.Column(db.Integer, primary_key=True)
    matter_id = db.Column(
        db.Text, db.ForeignKey("matter.matter_id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_id = db.Column(
        db.Integer, db.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True, index=True
    )
    office_action_id = db.Column(
        db.Text, db.ForeignKey("office_action.oa_id", ondelete="CASCADE"), nullable=True, index=True
    )
    source = db.Column(db.Text)
    ref_type = db.Column(db.Text)
    label = db.Column(db.Text)
    country = db.Column(db.Text)
    publication_number = db.Column(db.Text)
    published_date = db.Column(db.Text)
    title = db.Column(db.Text)
    raw_text = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)
