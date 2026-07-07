from __future__ import annotations

from typing import Any, Iterable

from app.services.workflow import deferred_task_sync as _runtime


def set_deferred_sync_meta(meta: dict[str, Any] | None) -> None:
    """Record correlation metadata for the next deadline/workflow sync request."""
    _runtime.set_deferred_sync_meta(meta)


def enqueue_docket_sync(*, docket_item_id: str, actor_id: int | None = None) -> None:
    """Request workflow/worklog follow-up for a docket change."""
    _runtime.enqueue_docket_sync(docket_item_id=docket_item_id, actor_id=actor_id)


def enqueue_docket_sync_for_item(*, docket_item: Any, actor_id: int | None = None) -> None:
    """Request workflow/worklog follow-up for a docket ORM instance."""
    _runtime.enqueue_docket_sync_for_item(docket_item=docket_item, actor_id=actor_id)


def enqueue_workflow_sync(*, workflow_id: int, commit: bool = True) -> None:
    """Request calendar follow-up for a workflow change."""
    _runtime.enqueue_workflow_sync(workflow_id=workflow_id, commit=commit)


def enqueue_workflow_task_sync(*, workflow_id: int, actor_id: int | None = None) -> None:
    """Request docket/worklog follow-up for a workflow change."""
    _runtime.enqueue_workflow_task_sync(workflow_id=workflow_id, actor_id=actor_id)


def enqueue_annuity_sync(annuity_id: str | int | None) -> None:
    """Request annuity-driven workflow rebuild."""
    _runtime.enqueue_annuity_sync(annuity_id)


def enqueue_annuity_sync_for_item(annuity_item: Any) -> None:
    """Request annuity-driven workflow rebuild for an ORM instance."""
    _runtime.enqueue_annuity_sync_for_item(annuity_item)


def enqueue_annuity_sync_for_matter(matter_id: str | None) -> None:
    """Request annuity-driven workflow rebuild for a matter."""
    _runtime.enqueue_annuity_sync_for_matter(matter_id)


def enqueue_annuity_sync_for_matter_ids_after_commit(
    matter_ids: Iterable[str | None],
    *,
    allow_testing_durable: bool = True,
) -> None:
    """Request annuity workflow rebuild after the current commit finishes."""
    _runtime.enqueue_annuity_sync_for_matter_ids_after_commit(
        matter_ids,
        allow_testing_durable=allow_testing_durable,
    )


def enqueue_annuity_ensure_for_matter_ids_after_commit(
    matter_ids: Iterable[str | None],
    *,
    allow_testing_durable: bool = True,
) -> None:
    """Request annuity row generation and workflow rebuild after commit."""
    _runtime.enqueue_annuity_ensure_for_matter_ids_after_commit(
        matter_ids,
        allow_testing_durable=allow_testing_durable,
    )


# Backward-compatible names. New code should use enqueue_* / set_deferred_sync_meta.
request_sync_meta = set_deferred_sync_meta
request_docket_workflow_sync = enqueue_docket_sync
request_docket_workflow_sync_for_item = enqueue_docket_sync_for_item
request_workflow_calendar_sync = enqueue_workflow_sync
request_workflow_task_sync = enqueue_workflow_task_sync
request_annuity_workflow_sync = enqueue_annuity_sync
request_annuity_workflow_sync_for_item = enqueue_annuity_sync_for_item
request_annuity_workflow_sync_for_matter = enqueue_annuity_sync_for_matter
request_annuity_workflow_sync_for_matters_after_commit = (
    enqueue_annuity_sync_for_matter_ids_after_commit
)


__all__ = [
    "enqueue_annuity_sync",
    "enqueue_annuity_ensure_for_matter_ids_after_commit",
    "enqueue_annuity_sync_for_item",
    "enqueue_annuity_sync_for_matter",
    "enqueue_annuity_sync_for_matter_ids_after_commit",
    "enqueue_docket_sync",
    "enqueue_docket_sync_for_item",
    "enqueue_workflow_sync",
    "enqueue_workflow_task_sync",
    "set_deferred_sync_meta",
]
