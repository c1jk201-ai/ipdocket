from datetime import datetime

from app.extensions import db


class Deadline(db.Model):
    __tablename__ = "deadlines"
    __table_args__ = (
        db.Index(
            "ux_deadlines_open_natural",
            "case_id",
            "type",
            "due_date",
            unique=True,
            postgresql_where=db.text("(status IS NULL OR status <> 'done')"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(50))
    due_date = db.Column(db.Date, nullable=False)
    internal_due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="new")  # new/done/overdue
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id"))
    priority = db.Column(db.String(20), default="normal")  # low/normal/high
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True
    )


class Reminder(db.Model):
    __tablename__ = "reminders"

    id = db.Column(db.Integer, primary_key=True)
    deadline_id = db.Column(
        db.Integer, db.ForeignKey("deadlines.id", ondelete="CASCADE"), nullable=False
    )
    remind_at = db.Column(db.DateTime, nullable=False)
    channel = db.Column(db.String(20), default="ui")  # ui/email
    sent_at = db.Column(db.DateTime)


class RenewalFee(db.Model):
    __tablename__ = "renewal_fees"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    fee_amount = db.Column(db.Numeric(15, 2), default=0)
    currency = db.Column(db.String(8), default="USD")
    status = db.Column(db.String(20), default="pending")  # pending/paid/overdue
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True
    )
