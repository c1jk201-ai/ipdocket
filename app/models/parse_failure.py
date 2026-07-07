from __future__ import annotations

from datetime import datetime

from app.extensions import db


class ParseFailure(db.Model):
    """
    Data-quality log for parsing failures (date/int/float/decimal etc).
    NOTE: no commit here; caller owns the transaction.
    """

    __tablename__ = "parse_failure"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)

    # e.g. "date" | "int" | "float" | "decimal"
    kind = db.Column(db.String(20), nullable=False, index=True)

    # e.g. "docket_dates.normalize_date_str"
    source = db.Column(db.String(255), index=True)
    field_name = db.Column(db.String(255), index=True)

    raw_value = db.Column(db.Text)
    normalized_value = db.Column(db.Text)
    error = db.Column(db.Text)

    entity_type = db.Column(db.String(64), index=True)
    entity_id = db.Column(db.Text, index=True)

    request_id = db.Column(db.Text, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=True)
    extra = db.Column(db.Text)  # JSON string (optional)
