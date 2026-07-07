"""
Notification Log Model

Tracks sent notifications to prevent duplicate sending.
"""

from datetime import datetime

from app.extensions import db


class NotificationLog(db.Model):
    """Track sent notifications to prevent duplicates."""

    __tablename__ = "notification_log"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(30), nullable=False, index=True)  # docket_item, annuity_item
    entity_id = db.Column(db.Text, nullable=False, index=True)
    channel = db.Column(db.String(30), nullable=False, index=True)  # email
    days_before = db.Column(db.Integer, nullable=False)  # 30, 14, 7, 1
    due_date = db.Column(db.Date, index=True)  # dedupe key: due-date snapshot at send time
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    recipient = db.Column(db.Text)  # email address
    status = db.Column(db.String(20), default="sent")  # sent, failed
    error_message = db.Column(db.Text)

    __table_args__ = (
        db.UniqueConstraint(
            "entity_type",
            "entity_id",
            "channel",
            "days_before",
            "due_date",
            name="uq_notification_entity_channel_days_due",
        ),
    )

    def __repr__(self):
        return f"<NotificationLog {self.entity_type}:{self.entity_id} {self.channel} {self.days_before}d>"
