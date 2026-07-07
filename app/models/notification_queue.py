from __future__ import annotations

from app.extensions import db
from app.utils.timezone import utcnow_naive


class NotificationQueue(db.Model):
    """
      Notice 
    - D-7/D-3/D-1 (Deadline )  
    -  / Send () Extend 
    """

    __tablename__ = "notification_queue"

    id = db.Column(db.Integer, primary_key=True)

    # User Identifiers( id     stringto)
    user_id = db.Column(db.String(64), nullable=False, index=True)

    kind = db.Column(db.String(40), nullable=False, index=True)  # e.g., DOCKET_REMINDER
    priority = db.Column(db.Integer, nullable=False, default=0)  # 0=normal, 10=urgent(StatutoryDeadline)

    title = db.Column(db.String(255), nullable=False, default="")
    message = db.Column(db.Text, nullable=False, default="")

    matter_id = db.Column(db.String(64), nullable=True, index=True)
    docket_id = db.Column(db.Text, nullable=True, index=True)

    remind_on = db.Column(db.Date, nullable=True, index=True)
    due_date = db.Column(db.Date, nullable=True, index=True)

    # idempotent upsert key (unique)
    dedupe_key = db.Column(db.String(180), nullable=False, unique=True, index=True)

    is_read = db.Column(db.Boolean, nullable=False, default=False)
    read_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    def mark_read(self) -> None:
        self.is_read = True
        self.read_at = utcnow_naive()
