from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.ip_records import DeadlineReviewQueue, DocketItem, Matter
from app.models.system_config import SystemConfig
from app.models.workflow import Workflow
from app.services.workflow.status_sync import docket_due_values_for_workflow_sync
from app.utils.docket_dates import (
    adjusted_legal_due_for_docket,
    effective_due_for_work,
    internal_due_for_docket,
    parse_date,
)

_SOURCE = "deadline_verification"
_OPEN_STATES = {"OPEN", "REOPENED"}


@dataclass(frozen=True)
class DeadlineIssue:
    matter_id: str
    issue_type: str
    severity: str
    docket_id: str | None = None
    workflow_id: int | None = None
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None


def _date_to_text(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def current_deadline_rule_version() -> str:
    raw = SystemConfig.get_config("RULE_REGISTRY_JSON", "") or ""
    if raw:
        try:
            payload = json.loads(raw)
            version = str((payload or {}).get("version") or "").strip()
            if version:
                return version
        except Exception:
            return "builtin"
    return "builtin"


def _signature(issue: DeadlineIssue, *, rule_version: str) -> str:
    payload = {
        "rule_version": rule_version,
        "matter_id": issue.matter_id,
        "docket_id": issue.docket_id,
        "workflow_id": issue.workflow_id,
        "issue_type": issue.issue_type,
        "expected": _json_safe(issue.expected or {}),
        "actual": _json_safe(issue.actual or {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_active_docket(item: DocketItem) -> bool:
    return not bool(getattr(item, "is_deleted", False))


def _is_open_docket(item: DocketItem) -> bool:
    done = str(getattr(item, "done_date", "") or "").strip()
    return _is_active_docket(item) and not done


def _is_open_workflow(workflow: Workflow) -> bool:
    status = str(getattr(workflow, "status", "") or "").strip()
    return status not in Workflow.TERMINAL_STATUSES


def _workflow_docket_id(workflow: Workflow) -> str | None:
    business_code = str(getattr(workflow, "business_code", "") or "").strip()
    if not business_code.upper().startswith("DOCKET:"):
        return None
    return business_code.split(":", 2)[1].strip() or None


def _docket_evidence(item: DocketItem) -> dict[str, Any]:
    legal_due = adjusted_legal_due_for_docket(item.due_date, item.extended_due_date)
    internal_due = internal_due_for_docket(item.due_date, item.extended_due_date)
    effective_due = effective_due_for_work(item.due_date, item.extended_due_date)
    return {
        "source_table": "docket_item",
        "docket_id": item.docket_id,
        "name_ref": item.name_ref,
        "name_free": item.name_free,
        "raw": {
            "due_date": item.due_date,
            "extended_due_date": item.extended_due_date,
            "done_date": item.done_date,
        },
        "derived": {
            "legal_due_date": _date_to_text(legal_due),
            "internal_due_date": _date_to_text(internal_due),
            "effective_due_date": _date_to_text(effective_due),
        },
    }


def _build_docket_issues(dockets: list[DocketItem]) -> list[DeadlineIssue]:
    issues: list[DeadlineIssue] = []
    seen_open_keys: dict[tuple[str, str, str], DocketItem] = {}

    for item in dockets:
        if not _is_open_docket(item):
            continue
        matter_id = str(getattr(item, "matter_id", "") or "").strip()
        if not matter_id:
            continue
        legal_due = adjusted_legal_due_for_docket(item.due_date, item.extended_due_date)
        internal_due = internal_due_for_docket(item.due_date, item.extended_due_date)
        effective_due = effective_due_for_work(item.due_date, item.extended_due_date)

        if not effective_due:
            issues.append(
                DeadlineIssue(
                    matter_id=matter_id,
                    docket_id=item.docket_id,
                    issue_type="deadline.missing_effective_due",
                    severity="HIGH",
                    expected={"effective_due_date": "required for open docket"},
                    actual={
                        "due_date": item.due_date,
                        "extended_due_date": item.extended_due_date,
                    },
                    evidence=_docket_evidence(item),
                )
            )

        if legal_due and internal_due and internal_due > legal_due:
            issues.append(
                DeadlineIssue(
                    matter_id=matter_id,
                    docket_id=item.docket_id,
                    issue_type="deadline.internal_after_legal_due",
                    severity="HIGH",
                    expected={"internal_due_date": "<= legal_due_date"},
                    actual={
                        "legal_due_date": legal_due.isoformat(),
                        "internal_due_date": internal_due.isoformat(),
                    },
                    evidence=_docket_evidence(item),
                )
            )

        key = (
            matter_id,
            str(item.name_ref or item.name_free or "").strip(),
            _date_to_text(effective_due) or "",
        )
        if key[1] and key[2]:
            previous = seen_open_keys.get(key)
            if previous is not None:
                issues.append(
                    DeadlineIssue(
                        matter_id=matter_id,
                        docket_id=item.docket_id,
                        issue_type="deadline.duplicate_open_docket",
                        severity="MEDIUM",
                        expected={"unique_open_docket": key[1], "effective_due_date": key[2]},
                        actual={
                            "duplicate_docket_ids": [previous.docket_id, item.docket_id],
                        },
                        evidence={
                            "source_table": "docket_item",
                            "docket_id": item.docket_id,
                            "previous_docket_id": previous.docket_id,
                        },
                    )
                )
            else:
                seen_open_keys[key] = item

    return issues


def _build_workflow_issues(
    workflows: list[Workflow],
    dockets_by_id: dict[str, DocketItem],
) -> list[DeadlineIssue]:
    issues: list[DeadlineIssue] = []
    for workflow in workflows:
        if not _is_open_workflow(workflow):
            continue
        matter_id = str(getattr(workflow, "case_id", "") or "").strip()
        if not matter_id:
            continue
        due = getattr(workflow, "due_date", None)
        legal_due = getattr(workflow, "legal_due_date", None)
        if due and legal_due and due > legal_due:
            issues.append(
                DeadlineIssue(
                    matter_id=matter_id,
                    workflow_id=workflow.id,
                    issue_type="deadline.workflow_internal_after_legal_due",
                    severity="HIGH",
                    expected={"workflow_due_date": "<= workflow_legal_due_date"},
                    actual={
                        "due_date": _date_to_text(due),
                        "legal_due_date": _date_to_text(legal_due),
                    },
                    evidence={
                        "source_table": "workflows",
                        "workflow_id": workflow.id,
                        "name": workflow.name,
                        "business_code": workflow.business_code,
                    },
                )
            )

        docket_id = _workflow_docket_id(workflow)
        if not docket_id:
            continue
        docket = dockets_by_id.get(docket_id)
        if docket is None or not _is_active_docket(docket):
            issues.append(
                DeadlineIssue(
                    matter_id=matter_id,
                    workflow_id=workflow.id,
                    docket_id=docket_id,
                    issue_type="deadline.workflow_missing_source_docket",
                    severity="HIGH",
                    expected={"source_docket": "active docket_item"},
                    actual={"business_code": workflow.business_code},
                    evidence={
                        "source_table": "workflows",
                        "workflow_id": workflow.id,
                        "business_code": workflow.business_code,
                    },
                )
            )
            continue

        expected_due, expected_legal_due = docket_due_values_for_workflow_sync(docket)
        actual_due = getattr(workflow, "due_date", None)
        actual_legal_due = getattr(workflow, "legal_due_date", None)
        if actual_due != expected_due or actual_legal_due != expected_legal_due:
            issues.append(
                DeadlineIssue(
                    matter_id=matter_id,
                    docket_id=docket_id,
                    workflow_id=workflow.id,
                    issue_type="deadline.workflow_docket_recalc_mismatch",
                    severity="HIGH",
                    expected={
                        "due_date": _date_to_text(expected_due),
                        "legal_due_date": _date_to_text(expected_legal_due),
                    },
                    actual={
                        "due_date": _date_to_text(actual_due),
                        "legal_due_date": _date_to_text(actual_legal_due),
                        "source_docket_due_date": _date_to_text(
                            getattr(workflow, "source_docket_due_date", None)
                        ),
                        "source_docket_legal_due_date": _date_to_text(
                            getattr(workflow, "source_docket_legal_due_date", None)
                        ),
                    },
                    evidence={
                        "workflow": {
                            "source_table": "workflows",
                            "workflow_id": workflow.id,
                            "name": workflow.name,
                            "business_code": workflow.business_code,
                        },
                        "docket": _docket_evidence(docket),
                    },
                )
            )

    return issues


def _upsert_issue(issue: DeadlineIssue, *, rule_version: str) -> DeadlineReviewQueue:
    sig = _signature(issue, rule_version=rule_version)
    now = datetime.utcnow()
    row = DeadlineReviewQueue.query.filter_by(signature=sig).first()
    if row is None:
        row = DeadlineReviewQueue(
            signature=sig,
            matter_id=issue.matter_id,
            docket_id=issue.docket_id,
            workflow_id=issue.workflow_id,
            issue_type=issue.issue_type,
            severity=issue.severity,
            status="OPEN",
            rule_version=rule_version,
            source=_SOURCE,
            evidence_json=_json_safe(issue.evidence or {}),
            expected_json=_json_safe(issue.expected or {}),
            actual_json=_json_safe(issue.actual or {}),
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            row = DeadlineReviewQueue.query.filter_by(signature=sig).first()
            if row is None:
                raise
    row.matter_id = issue.matter_id
    row.docket_id = issue.docket_id
    row.workflow_id = issue.workflow_id
    row.issue_type = issue.issue_type
    row.severity = issue.severity
    row.status = "OPEN"
    row.rule_version = rule_version
    row.source = _SOURCE
    row.evidence_json = _json_safe(issue.evidence or {})
    row.expected_json = _json_safe(issue.expected or {})
    row.actual_json = _json_safe(issue.actual or {})
    row.updated_at = now
    row.resolved_at = None
    row.resolved_by = None
    row.resolution_note = None
    db.session.add(row)
    return row


def verify_deadlines_for_matter(matter_id: str, *, commit: bool = False) -> dict[str, int]:
    mid = str(matter_id or "").strip()
    if not mid:
        return {"checked": 0, "open_issues": 0, "resolved": 0}

    dockets = DocketItem.query.filter_by(matter_id=mid).all()
    workflows = Workflow.query.filter_by(case_id=mid).all()
    dockets_by_id = {
        str(docket.docket_id): docket for docket in dockets if getattr(docket, "docket_id", None)
    }
    issues = _build_docket_issues(dockets)
    issues.extend(_build_workflow_issues(workflows, dockets_by_id))

    rule_version = current_deadline_rule_version()
    active_signatures = set()
    for issue in issues:
        row = _upsert_issue(issue, rule_version=rule_version)
        active_signatures.add(row.signature)

    resolved = 0
    stale_rows = DeadlineReviewQueue.query.filter(
        DeadlineReviewQueue.matter_id == mid,
        DeadlineReviewQueue.source == _SOURCE,
        DeadlineReviewQueue.status.in_(list(_OPEN_STATES)),
    ).all()
    now = datetime.utcnow()
    for row in stale_rows:
        if row.signature in active_signatures:
            continue
        row.status = "RESOLVED"
        row.resolved_at = now
        row.resolution_note = "auto_resolved_no_longer_detected"
        row.updated_at = now
        db.session.add(row)
        resolved += 1

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return {
        "checked": len(dockets) + len(workflows),
        "open_issues": len(issues),
        "resolved": resolved,
    }


def verify_deadline_queue(
    *,
    matter_ids: list[str] | None = None,
    limit: int = 200,
    commit: bool = False,
) -> dict[str, int]:
    ids = [str(mid).strip() for mid in (matter_ids or []) if str(mid).strip()]
    if not ids:
        q = Matter.query
        if hasattr(Matter, "is_deleted"):
            q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
        ids = [row.matter_id for row in q.order_by(Matter.created_at.desc()).limit(limit).all()]

    total_checked = 0
    total_open = 0
    total_resolved = 0
    for mid in ids[: max(1, int(limit or 200))]:
        result = verify_deadlines_for_matter(mid, commit=False)
        total_checked += int(result.get("checked") or 0)
        total_open += int(result.get("open_issues") or 0)
        total_resolved += int(result.get("resolved") or 0)

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return {
        "matters": len(ids),
        "checked": total_checked,
        "open_issues": total_open,
        "resolved": total_resolved,
    }


def resolve_deadline_review_item(
    queue_id: int,
    *,
    actor_id: int | None = None,
    note: str | None = None,
    commit: bool = False,
) -> DeadlineReviewQueue | None:
    row = db.session.get(DeadlineReviewQueue, int(queue_id))
    if row is None:
        return None
    row.status = "RESOLVED"
    row.resolved_at = datetime.utcnow()
    row.resolved_by = actor_id
    row.resolution_note = str(note or "").strip() or "manual_resolved"
    row.updated_at = datetime.utcnow()
    db.session.add(row)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return row
