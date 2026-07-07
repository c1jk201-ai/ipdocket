from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models.workflow import Workflow
from app.services.case.cascade_delete_service import delete_workflow_fk_children


def delete_workflow_from_google(workflow: Workflow | dict[str, Any] | None) -> int:
    """Delete external workflow artifacts.

    Google workflow/calendar integration is optional in this deployment. Keep a
    stable hook so background deletion jobs can be patched by integrations and
    tests without changing the DB cleanup path.
    """
    _ = workflow
    return 0


def delete_workflow_background(
    workflow_id: int | str,
    *,
    thread_key: list[str] | tuple[str, str, str] | None = None,
    actor_id: int | str | None = None,
    case_id: int | str | None = None,
    workflow: dict[str, Any] | None = None,
    delete_db: bool = True,
) -> None:
    """Clean up external workflow artifacts, optionally deleting the DB row."""
    _ = actor_id, thread_key, case_id
    try:
        wf_id = int(workflow_id)
    except Exception:
        return

    wf = db.session.get(Workflow, wf_id)

    external_target: Workflow | dict[str, Any] | None = wf if wf is not None else workflow
    if external_target is not None:
        delete_workflow_from_google(external_target)

    if wf and delete_db:
        try:
            wf.status = "Abandoned"
            db.session.add(wf)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    try:
        if delete_db:
            delete_workflow_fk_children(wf_id)
            wf = db.session.get(Workflow, wf_id)
            if wf:
                db.session.delete(wf)
                db.session.flush()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def run_delete_workflow_job(payload: dict[str, Any]) -> None:
    delete_workflow_background(
        payload.get("workflow_id"),
        thread_key=payload.get("thread_key"),
        actor_id=payload.get("actor_id"),
        case_id=payload.get("case_id"),
        workflow=payload.get("workflow"),
        delete_db=bool(payload.get("delete_db", True)),
    )
