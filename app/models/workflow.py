from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import validates

from app.extensions import db
from app.models.ip_records import Matter
from app.models.workflow_checklist import WorkflowChecklistItem
from app.utils.timezone import today_local


class Workflow(db.Model):
    __tablename__ = "workflows"
    __table_args__ = (db.UniqueConstraint("business_code", name="ux_workflows_business_code"),)
    STATUS_PENDING = "Pending"
    STATUS_IN_PROGRESS = "In Progress"
    STATUS_COMPLETED = "Completed"
    STATUS_ABANDONED = "Abandoned"
    STATUSES = frozenset({STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED, STATUS_ABANDONED})
    TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_ABANDONED}

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(
        db.Text, db.ForeignKey("matter.matter_id", ondelete="CASCADE"), nullable=True
    )
    matter = db.relationship(
        Matter,
        primaryjoin="Workflow.case_id == Matter.matter_id",
        lazy="joined",
    )

    name = db.Column(db.String(100), nullable=False)  # e.g., "OA Received"
    status = db.Column(db.String(20), default=STATUS_PENDING)

    # Optional extended fields for "MatterResponsibleTask" (best-effort, mostly for ipm-like UX)
    business_code = db.Column(db.String(50))  # optional structured code
    category = db.Column(db.String(20))  # MGMT, WORK, FILING, EXAM, etc.
    priority = db.Column(db.String(10))  # normal / important / urgent

    request_start_date = db.Column(db.Date)
    legal_due_date = db.Column(db.Date)
    source_docket_due_date = db.Column(db.Date)
    source_docket_legal_due_date = db.Column(db.Date)
    draft_due_date = db.Column(db.Date)
    draft_due_date2 = db.Column(db.Date)
    submit_due_date = db.Column(db.Date)
    draft_sent_date = db.Column(db.Date)
    submit_date = db.Column(db.Date)

    difficulty = db.Column(db.Float)
    page_count = db.Column(db.Integer)
    work_hours = db.Column(db.Float)

    due_date = db.Column(db.Date)
    completed_date = db.Column(db.Date)

    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    assignee = db.relationship("User", foreign_keys=[assignee_id])
    attorney_assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    attorney_assignee = db.relationship("User", foreign_keys=[attorney_assignee_id])

    inspector_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    inspector = db.relationship("User", foreign_keys=[inspector_id])

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    completed_by = db.relationship("User", foreign_keys=[completed_by_id])

    note = db.Column(db.Text)
    send_memo = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True
    )
    snapshot_attorney = db.Column(db.Text)
    snapshot_handler = db.Column(db.Text)
    snapshot_manager = db.Column(db.Text)

    checklist_items = db.relationship(
        WorkflowChecklistItem,
        primaryjoin=lambda: Workflow.id == WorkflowChecklistItem.workflow_id,
        lazy="select",
        order_by=lambda: (
            WorkflowChecklistItem.sort_order.asc(),
            WorkflowChecklistItem.id.asc(),
        ),
        cascade="all, delete-orphan",
    )

    @validates("status")
    def _validate_status(self, key, value):
        raw = (value or "").strip()
        if not raw:
            return "Pending"
        normalized = raw.lower().replace("_", " ").replace("-", " ")
        status_map = {
            "pending": self.STATUS_PENDING,
            "in progress": self.STATUS_IN_PROGRESS,
            "completed": self.STATUS_COMPLETED,
            "complete": self.STATUS_COMPLETED,
            "done": self.STATUS_COMPLETED,
            "abandoned": self.STATUS_ABANDONED,
            "cancelled": self.STATUS_ABANDONED,
            "canceled": self.STATUS_ABANDONED,
        }
        return status_map.get(normalized, raw)

    @property
    def is_urgent(self):
        if self.due_date and self.status not in self.TERMINAL_STATUSES:
            days_left = (self.due_date - today_local()).days
            return days_left <= 3 and days_left >= 0
        return False

    @property
    def is_overdue(self):
        if self.due_date and self.status not in self.TERMINAL_STATUSES:
            return self.due_date < today_local()
        return False

    @property
    def assigned_user_ids(self) -> list[int]:
        seen: set[int] = set()
        ordered: list[int] = []
        for raw in (self.assignee_id, self.attorney_assignee_id, self.inspector_id):
            if raw is None:
                continue
            try:
                uid = int(raw)
            except Exception:
                continue
            if uid <= 0 or uid in seen:
                continue
            seen.add(uid)
            ordered.append(uid)
        return ordered

    def __repr__(self):
        return f"<Workflow {self.name} for Matter {self.case_id}>"


_ASSIGNMENT_REQUEST_ROLE_FIELDS: dict[str, str] = {
    "handler": "assignee_id",
    "attorney": "attorney_assignee_id",
    "manager": "inspector_id",
}


def _coerce_positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _terminal_assignment_request_resolution(status: object) -> tuple[str | None, str | None]:
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest

    normalized = str(status or "").strip()
    if normalized == Workflow.STATUS_COMPLETED:
        return WorkflowAssignmentRequest.STATUS_ACCEPTED, "workflow-completed"
    if normalized == Workflow.STATUS_ABANDONED:
        return WorkflowAssignmentRequest.STATUS_CANCELLED, "workflow-abandoned"
    return None, None


def _close_pending_assignment_request(
    req,
    *,
    status: str,
    note: str,
    now: datetime,
) -> bool:
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest

    if str(getattr(req, "status", "") or "").strip() != WorkflowAssignmentRequest.STATUS_PENDING:
        return False

    req.status = status
    req.responded_at = now
    req.response_note = note
    return True


def _assignment_request_matches_current_workflow(req, workflow: Workflow) -> bool:
    role = str(getattr(req, "role_code", "") or "").strip()
    field = _ASSIGNMENT_REQUEST_ROLE_FIELDS.get(role)
    if not field:
        return True
    return _coerce_positive_int(getattr(workflow, field, None)) == _coerce_positive_int(
        getattr(req, "target_user_id", None)
    )


def _workflow_for_assignment_request(
    session: SASession,
    req,
    workflow_by_id: dict[int, Workflow],
) -> Workflow | None:
    workflow = getattr(req, "workflow", None)
    if workflow is not None:
        return workflow

    workflow_id = _coerce_positive_int(getattr(req, "workflow_id", None))
    if workflow_id is None:
        return None
    workflow = workflow_by_id.get(workflow_id)
    if workflow is not None:
        return workflow

    with session.no_autoflush:
        return session.get(Workflow, workflow_id)


def _finalize_assignment_request_for_workflow(req, workflow: Workflow, *, now: datetime) -> bool:
    status, note = _terminal_assignment_request_resolution(getattr(workflow, "status", None))
    if status and note:
        return _close_pending_assignment_request(req, status=status, note=note, now=now)

    if not _assignment_request_matches_current_workflow(req, workflow):
        from app.models.workflow_assignment_request import WorkflowAssignmentRequest

        return _close_pending_assignment_request(
            req,
            status=WorkflowAssignmentRequest.STATUS_CANCELLED,
            note="workflow-assignment-changed",
            now=now,
        )
    return False


@event.listens_for(SASession, "before_flush")
def _finalize_stale_assignment_requests_for_workflows(
    session: SASession,
    flush_context,
    instances,
) -> None:
    from app.models.workflow_assignment_request import WorkflowAssignmentRequest

    workflow_by_id: dict[int, Workflow] = {}
    now = datetime.utcnow()

    for obj in tuple(session.new) + tuple(session.dirty):
        if not isinstance(obj, Workflow) or obj in session.deleted:
            continue
        workflow_id = getattr(obj, "id", None)
        if workflow_id is not None:
            workflow_by_id[int(workflow_id)] = obj

    for obj in tuple(session.new):
        if not isinstance(obj, WorkflowAssignmentRequest) or obj in session.deleted:
            continue
        workflow = _workflow_for_assignment_request(session, obj, workflow_by_id)
        if workflow is not None and _finalize_assignment_request_for_workflow(
            obj,
            workflow,
            now=now,
        ):
            session.add(obj)

    if not workflow_by_id:
        return

    with session.no_autoflush:
        pending_rows = (
            session.query(WorkflowAssignmentRequest)
            .filter(WorkflowAssignmentRequest.workflow_id.in_(sorted(workflow_by_id)))
            .filter(
                WorkflowAssignmentRequest.status == WorkflowAssignmentRequest.STATUS_PENDING
            )
            .all()
        )

    for req in pending_rows:
        workflow = _workflow_for_assignment_request(session, req, workflow_by_id)
        if workflow is not None and _finalize_assignment_request_for_workflow(
            req,
            workflow,
            now=now,
        ):
            session.add(req)
