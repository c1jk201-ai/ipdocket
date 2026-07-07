from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_

from app.extensions import db
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.utils.error_logging import report_swallowed_exception
from app.utils.status_red_visibility import (
    is_non_action_status_red_label,
    status_red_label_from_ref,
)

_STATUS_RED_REF_PREFIX = "MGMT:STATUS_RED:"
_AUTO_WORKFLOW_NOTE_MARKERS = ("Auto Create", "USPTO Notice Auto Create")


@dataclass(frozen=True)
class PassiveStatusRedArtifactSet:
    dockets: list[DocketItem]
    workflows: list[Workflow]
    worklogs: list[WorkLog]
    passive_matter_rows: list[Matter]
    residual_red_date_matter_rows: list[Matter]
    mainline_parallel_action_rows: list[tuple[str, str, str]]


def _chunked(values: list[Any], size: int = 200) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _passive_status_red_label_for_docket(docket: DocketItem | None) -> str:
    if docket is None:
        return ""
    name_ref = (getattr(docket, "name_ref", None) or "").strip()
    if not name_ref.upper().startswith(_STATUS_RED_REF_PREFIX):
        return ""
    return status_red_label_from_ref(
        name_ref=name_ref,
        title=getattr(docket, "name_free", None),
    )


def _is_passive_status_red_docket(docket: DocketItem | None) -> bool:
    return is_non_action_status_red_label(_passive_status_red_label_for_docket(docket))


def _passive_status_red_dockets(*, include_deleted: bool = True) -> list[DocketItem]:
    q = DocketItem.query.filter(DocketItem.name_ref.ilike(f"{_STATUS_RED_REF_PREFIX}%"))
    if not include_deleted and hasattr(DocketItem, "is_deleted"):
        q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    rows = q.order_by(DocketItem.matter_id.asc(), DocketItem.docket_id.asc()).all()
    return [row for row in rows if _is_passive_status_red_docket(row)]


def _docket_workflows(dockets: list[DocketItem]) -> list[Workflow]:
    clauses = []
    for docket in dockets:
        docket_id = (getattr(docket, "docket_id", None) or "").strip()
        matter_id = str(getattr(docket, "matter_id", "") or "").strip()
        if not docket_id or not matter_id:
            continue
        prefix = f"DOCKET:{docket_id}"
        clauses.append(
            and_(
                Workflow.case_id == matter_id,
                or_(
                    Workflow.business_code == prefix,
                    Workflow.business_code.like(f"{prefix}:%"),
                ),
            )
        )
    if not clauses:
        return []

    rows: list[Workflow] = []
    for chunk in _chunked(clauses, 100):
        rows.extend(
            Workflow.query.filter(or_(*chunk))
            .order_by(Workflow.case_id.asc(), Workflow.id.asc())
            .all()
        )
    return rows


def _is_auto_workflow(workflow: Workflow | None) -> bool:
    note = (getattr(workflow, "note", None) or "").strip()
    return any(marker in note for marker in _AUTO_WORKFLOW_NOTE_MARKERS)


def _worklogs_for_artifacts(
    *,
    dockets: list[DocketItem],
    workflows: list[Workflow],
) -> list[WorkLog]:
    docket_ids = sorted(
        {
            (getattr(docket, "docket_id", None) or "").strip()
            for docket in dockets
            if (getattr(docket, "docket_id", None) or "").strip()
        }
    )
    workflow_ids = sorted(
        {
            int(getattr(workflow, "id", 0) or 0)
            for workflow in workflows
            if getattr(workflow, "id", 0)
        }
    )
    rows: list[WorkLog] = []
    for chunk in _chunked(docket_ids, 500):
        rows.extend(WorkLog.query.filter(WorkLog.docket_id.in_(chunk)).all())
    for chunk in _chunked(workflow_ids, 500):
        rows.extend(WorkLog.query.filter(WorkLog.workflow_id.in_(chunk)).all())

    seen: set[int] = set()
    deduped: list[WorkLog] = []
    for row in rows:
        row_id = int(getattr(row, "id", 0) or 0)
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        deduped.append(row)
    return deduped


def _passive_matter_rows() -> list[Matter]:
    rows = (
        Matter.query.filter(
            Matter.status_red.isnot(None),
            Matter.status_red != "",
        )
        .order_by(Matter.our_ref.asc())
        .all()
    )
    return [
        row
        for row in rows
        if is_non_action_status_red_label((getattr(row, "status_red", None) or "").strip())
    ]


def _residual_red_date_matter_rows() -> list[Matter]:
    return (
        Matter.query.filter(
            or_(Matter.status_red.is_(None), Matter.status_red == ""),
            or_(
                and_(
                    Matter.status_red_related_date.isnot(None), Matter.status_red_related_date != ""
                ),
                Matter.status_red_related_on.isnot(None),
            ),
        )
        .order_by(Matter.our_ref.asc())
        .all()
    )


def _mainline_parallel_action_rows() -> list[tuple[str, str, str]]:
    rows = (
        db.session.query(Matter.matter_id, Matter.our_ref, DocketItem.due_date)
        .join(DocketItem, DocketItem.matter_id == Matter.matter_id)
        .filter(Matter.status_blue == "Filing Examination In Progress")
        .filter(DocketItem.name_ref == "MGMT:STATUS_RED:ForeignFilingDeadline")
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .filter(or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""))
        .order_by(Matter.our_ref.asc(), DocketItem.due_date.asc())
        .all()
    )
    return [(str(mid or ""), str(our_ref or ""), str(due or "")) for mid, our_ref, due in rows]


def collect_passive_status_red_artifacts(
    *,
    include_deleted_dockets: bool = True,
) -> PassiveStatusRedArtifactSet:
    dockets = _passive_status_red_dockets(include_deleted=include_deleted_dockets)
    workflows = _docket_workflows(dockets)
    auto_workflows = [workflow for workflow in workflows if _is_auto_workflow(workflow)]
    return PassiveStatusRedArtifactSet(
        dockets=dockets,
        workflows=auto_workflows,
        worklogs=_worklogs_for_artifacts(dockets=dockets, workflows=auto_workflows),
        passive_matter_rows=_passive_matter_rows(),
        residual_red_date_matter_rows=_residual_red_date_matter_rows(),
        mainline_parallel_action_rows=_mainline_parallel_action_rows(),
    )


def _sample_dockets(dockets: list[DocketItem], *, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for docket in dockets[: max(0, limit)]:
        out.append(
            {
                "docket_id": str(getattr(docket, "docket_id", "") or ""),
                "matter_id": str(getattr(docket, "matter_id", "") or ""),
                "label": _passive_status_red_label_for_docket(docket),
                "due_date": str(getattr(docket, "due_date", "") or ""),
                "is_deleted": str(bool(getattr(docket, "is_deleted", False))),
            }
        )
    return out


def passive_status_red_audit_summary(*, sample_limit: int = 20) -> dict[str, Any]:
    artifacts = collect_passive_status_red_artifacts()
    active_docket_count = sum(
        1 for docket in artifacts.dockets if not bool(getattr(docket, "is_deleted", False))
    )
    return {
        "counts": {
            "passive_status_red_dockets": len(artifacts.dockets),
            "active_passive_status_red_dockets": active_docket_count,
            "passive_auto_workflows": len(artifacts.workflows),
            "passive_worklogs": len(artifacts.worklogs),
            "passive_matter_status_red": len(artifacts.passive_matter_rows),
            "status_red_related_without_red": len(artifacts.residual_red_date_matter_rows),
            "mainline_blue_parallel_foreign_action": len(artifacts.mainline_parallel_action_rows),
        },
        "samples": {
            "passive_status_red_dockets": _sample_dockets(
                artifacts.dockets,
                limit=sample_limit,
            ),
            "passive_auto_workflows": [
                {
                    "id": int(getattr(workflow, "id", 0) or 0),
                    "case_id": str(getattr(workflow, "case_id", "") or ""),
                    "name": str(getattr(workflow, "name", "") or ""),
                    "business_code": str(getattr(workflow, "business_code", "") or ""),
                    "status": str(getattr(workflow, "status", "") or ""),
                }
                for workflow in artifacts.workflows[: max(0, sample_limit)]
            ],
            "passive_matter_status_red": [
                {
                    "matter_id": str(getattr(matter, "matter_id", "") or ""),
                    "our_ref": str(getattr(matter, "our_ref", "") or ""),
                    "status_red": str(getattr(matter, "status_red", "") or ""),
                    "status_blue": str(getattr(matter, "status_blue", "") or ""),
                }
                for matter in artifacts.passive_matter_rows[: max(0, sample_limit)]
            ],
        },
    }


def cleanup_passive_status_red_artifacts(*, apply: bool = False) -> dict[str, Any]:
    artifacts = collect_passive_status_red_artifacts(include_deleted_dockets=True)
    summary = passive_status_red_audit_summary(sample_limit=10)
    if not apply:
        summary["applied"] = False
        return summary

    from app.services.workflow.task_sync import (
        _delete_workflow_for_distribution_cleanup,
        sync_from_docket_item,
    )

    deleted_worklog_ids: list[int] = []
    worklog_ids = sorted(
        {
            int(getattr(worklog, "id", 0) or 0)
            for worklog in artifacts.worklogs
            if getattr(worklog, "id", 0)
        }
    )
    for chunk in _chunked(worklog_ids, 500):
        deleted_worklog_ids.extend(chunk)
        WorkLog.query.filter(WorkLog.id.in_(chunk)).delete(synchronize_session=False)

    deleted_workflow_ids: list[int] = []
    for workflow in artifacts.workflows:
        workflow_id = int(getattr(workflow, "id", 0) or 0)
        if workflow_id:
            _delete_workflow_for_distribution_cleanup(workflow_id=workflow_id)
            deleted_workflow_ids.append(workflow_id)
            try:
                db.session.expunge(workflow)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="passive_status_red_cleanup.expunge_workflow",
                    log_key="passive_status_red_cleanup.expunge_workflow",
                    log_window_seconds=300,
                )
        else:
            db.session.delete(workflow)
    for chunk in _chunked(sorted(set(deleted_workflow_ids)), 500):
        Workflow.query.filter(Workflow.id.in_(chunk)).delete(synchronize_session=False)

    changed_docket_ids: list[str] = []
    for docket in artifacts.dockets:
        docket_id = (getattr(docket, "docket_id", None) or "").strip()
        if not docket_id:
            continue
        if hasattr(docket, "is_deleted") and not bool(getattr(docket, "is_deleted", False)):
            sync_from_docket_item(docket_item=docket, actor_id=None)
            docket.is_deleted = True
            changed_docket_ids.append(docket_id)
        if hasattr(docket, "deleted_at") and getattr(docket, "deleted_at", None) is None:
            docket.deleted_at = datetime.utcnow()
        if (
            hasattr(docket, "delete_reason")
            and not (getattr(docket, "delete_reason", None) or "").strip()
        ):
            docket.delete_reason = "passive_status_red_cleanup"
        db.session.add(docket)

    changed_matter_ids: list[str] = []
    for matter in artifacts.passive_matter_rows:
        matter_id = str(getattr(matter, "matter_id", "") or "").strip()
        if matter_id:
            changed_matter_ids.append(matter_id)
        passive_label = (getattr(matter, "status_red", None) or "").strip()
        if passive_label and not (getattr(matter, "status_blue", None) or "").strip():
            matter.status_blue = passive_label
        matter.status_red = None
        matter.status_red_related_date = None
        if hasattr(matter, "status_red_related_on"):
            matter.status_red_related_on = None
        db.session.add(matter)

    for matter in artifacts.residual_red_date_matter_rows:
        matter.status_red_related_date = None
        if hasattr(matter, "status_red_related_on"):
            matter.status_red_related_on = None
        db.session.add(matter)

    for workflow_id in sorted(set(deleted_workflow_ids)):
        workflow = db.session.get(Workflow, workflow_id)
        if workflow is not None:
            db.session.delete(workflow)

    summary["applied"] = True
    summary["changed"] = {
        "soft_deleted_docket_ids": sorted(set(changed_docket_ids)),
        "cleared_passive_matter_ids": sorted(set(changed_matter_ids)),
        "deleted_workflow_ids": sorted(set(deleted_workflow_ids)),
        "deleted_worklog_ids": sorted(set(deleted_worklog_ids)),
    }
    return summary
