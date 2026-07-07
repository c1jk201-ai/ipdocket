from __future__ import annotations

from datetime import date, datetime, timedelta

from app.extensions import db


class WorkflowChecklistItem(db.Model):
    __tablename__ = "workflow_checklist_item"

    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(
        db.Integer, db.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    is_done = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    done_at = db.Column(db.DateTime)
    done_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    __table_args__ = (
        db.UniqueConstraint("workflow_id", "title", name="uq_workflow_checklist_item_title"),
        db.Index("ix_workflow_checklist_item_workflow_done", "workflow_id", "is_done"),
    )


class WorkflowReminderSent(db.Model):
    """
    Idempotency log for scheduled reminders.

    A reminder is unique per (workflow_id, due_date, kind). If the workflow due date changes,
    a new reminder series may be sent for the new due_date.
    """

    __tablename__ = "workflow_reminder_sent"

    id = db.Column(db.Integer, primary_key=True)
    workflow_id = db.Column(
        db.Integer, db.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind = db.Column(db.String(20), nullable=False)  # e.g. D14/D7/D2/D0
    due_date = db.Column(db.Date, nullable=False)
    remind_on = db.Column(db.Date, nullable=False, index=True)

    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint(
            "workflow_id", "kind", "due_date", name="uq_workflow_reminder_sent_workflow_kind_due"
        ),
        db.Index("ix_workflow_reminder_sent_remind_on_sent_at", "remind_on", "sent_at"),
    )

    @staticmethod
    def build_kind(offset_days: int) -> str:
        return f"D{int(offset_days)}"

    @staticmethod
    def compute_remind_on(*, due: date, offset_days: int) -> date:
        return due - timedelta(days=max(0, int(offset_days)))
