from __future__ import annotations

from flask import current_app

from app.services.workflow.sync_requests import enqueue_workflow_task_sync


def sync_workflow_task_immediately(*, workflow, actor_id: int | None) -> None:
    """Keep workflow-generated docket rows aligned before the response returns."""
    workflow_id = int(getattr(workflow, "id", 0) or 0)
    if workflow_id <= 0:
        return

    try:
        enqueue_workflow_task_sync(workflow_id=workflow_id, actor_id=actor_id)
    except Exception as exc:
        current_app.logger.warning(
            "Workflow task sync enqueue failed for wf=%s: %s",
            workflow_id,
            exc,
        )

    try:
        from app.services.workflow.task_sync import sync_from_workflow

        sync_from_workflow(workflow=workflow, actor_id=actor_id)
    except Exception as exc:
        current_app.logger.warning(
            "Immediate workflow task sync failed for wf=%s: %s",
            workflow_id,
            exc,
        )
