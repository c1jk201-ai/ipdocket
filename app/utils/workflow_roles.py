from __future__ import annotations

from sqlalchemy import func, or_


def _workflow_model():
    from app.models.workflow import Workflow

    return Workflow


def workflow_assignee_columns():
    """Return all workflow user-assignment columns in priority order."""
    Workflow = _workflow_model()
    return (
        Workflow.assignee_id,
        Workflow.attorney_assignee_id,
        Workflow.inspector_id,
    )


def workflow_user_filter(user_id: int | None):
    """
    SQL filter matching workflows assigned to a user in any role.

    Roles:
    - assignee_id (Handler)
    - attorney_assignee_id (Responsible attorney)
    - inspector_id (Manager)
    """
    Workflow = _workflow_model()
    if user_id is None:
        return Workflow.id == -1
    try:
        uid = int(user_id)
    except Exception:
        return Workflow.id == -1
    if uid <= 0:
        return Workflow.id == -1
    return or_(
        Workflow.assignee_id == uid,
        Workflow.attorney_assignee_id == uid,
        Workflow.inspector_id == uid,
    )


def workflow_has_any_assignee():
    """SQL filter for workflows with at least one assigned user."""
    Workflow = _workflow_model()
    return or_(
        Workflow.assignee_id.isnot(None),
        Workflow.attorney_assignee_id.isnot(None),
        Workflow.inspector_id.isnot(None),
    )


def workflow_primary_assignee_expr():
    """
    SQL expression for a single 'primary' assignee id.

    Priority:
    1. assignee_id
    2. attorney_assignee_id
    3. inspector_id
    """
    Workflow = _workflow_model()
    return func.coalesce(
        Workflow.assignee_id,
        Workflow.attorney_assignee_id,
        Workflow.inspector_id,
    )
