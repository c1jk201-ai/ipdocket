from __future__ import annotations

from datetime import datetime

from app.extensions import db


class MatterFacts(db.Model):
    """
    Normalized facts for a Matter, used for fast/robust deadline and annuity filtering.
    """

    __tablename__ = "matter_facts"

    matter_id = db.Column(db.Text, primary_key=True)

    registration_date = db.Column(db.Date, index=True)
    registration_date_source = db.Column(db.Text)

    right_type_norm = db.Column(db.Text, index=True)  # PATENT/UTILITY/DESIGN/...

    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True
    )
