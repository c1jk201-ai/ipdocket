"""
WorkLog Model - Task Log 

DocketItem(Deadline)  Contact Task Done   Log  .
/table   Confirm   .
"""

from datetime import datetime

from sqlalchemy.orm import validates

from app.extensions import db


class WorkLog(db.Model):
    """Task Log  - Todo List  """

    __tablename__ = "work_logs"
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_ABANDONED = "abandoned"
    STATUSES = frozenset({STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_ABANDONED})

    id = db.Column(db.Integer, primary_key=True)

    # Link DocketItem (Deadline)
    docket_id = db.Column(db.Text, index=True)
    workflow_id = db.Column(db.Integer, db.ForeignKey("workflows.id"), index=True)
    workflow = db.relationship("Workflow", foreign_keys=[workflow_id])

    # Quick Search  matter_id 
    matter_id = db.Column(db.Text, index=True)
    our_ref = db.Column(db.Text)  # Matter reference 

    # Task 
    task_name = db.Column(db.String(200))  # Task (DocketItem.name_free  name_ref)
    task_category = db.Column(db.String(50))  # MGMT, WORK 
    due_date = db.Column(db.Date)  # Deadline

    # Task Log
    action_type = db.Column(db.String(50), default="note")  # completed, note, started, etc.
    description = db.Column(db.Text)  #  Notes ("Notice Send Done", " " )

    # Status
    status = db.Column(db.String(20), default=STATUS_PENDING)

    # Contact 
    owner_staff_party_id = db.Column(db.Text, index=True)  # DocketItem Contact
    snapshot_attorney = db.Column(db.Text)  # Responsible attorney Name 
    snapshot_handler = db.Column(db.Text)  # Handler Name 
    snapshot_manager = db.Column(db.Text)  # Manager Name 
    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])

    # 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime)  # Done 

    @validates("status")
    def _validate_status(self, key, value):
        raw = (value or "").strip()
        if not raw:
            return self.STATUS_PENDING
        normalized = raw.lower().replace("-", "_").replace(" ", "_")
        status_map = {
            "pending": self.STATUS_PENDING,
            "open": self.STATUS_PENDING,
            "in_progress": self.STATUS_IN_PROGRESS,
            "started": self.STATUS_IN_PROGRESS,
            "completed": self.STATUS_COMPLETED,
            "complete": self.STATUS_COMPLETED,
            "done": self.STATUS_COMPLETED,
            "abandoned": self.STATUS_ABANDONED,
            "cancelled": self.STATUS_ABANDONED,
            "canceled": self.STATUS_ABANDONED,
        }
        return status_map.get(normalized, self.STATUS_PENDING)

    def __repr__(self):
        return f"<WorkLog {self.id}: {self.task_name} - {self.status}>"

    def to_dict(self):
        """JSON   """
        return {
            "id": self.id,
            "docket_id": self.docket_id,
            "workflow_id": self.workflow_id,
            "matter_id": self.matter_id,
            "our_ref": self.our_ref,
            "task_name": self.task_name,
            "task_category": self.task_category,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "action_type": self.action_type,
            "description": self.description,
            "status": self.status,
            "owner_staff_party_id": self.owner_staff_party_id,
            "snapshot_attorney": self.snapshot_attorney,
            "snapshot_handler": self.snapshot_handler,
            "snapshot_manager": self.snapshot_manager,
            "completed_by_id": self.completed_by_id,
            "completed_by_name": self.completed_by.display_name if self.completed_by else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
