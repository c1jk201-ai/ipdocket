from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.ip_records import (
    DeadlineReviewQueue,
    DocketItem,
    EmailMessage,
    Matter,
    MatterRiskFact,
    VMatterOverview,
)
from app.models.workflow import Workflow
from app.utils.docket_dates import effective_due_for_work, parse_date

URGENT_DAYS = 7
HIGH_OUTSTANDING_USD = 10_000_000
MEDIUM_OUTSTANDING_USD = 1_000_000


def _today() -> date:
    return date.today()


def _is_active_matter(matter: Matter) -> bool:
    return not bool(getattr(matter, "is_deleted", False))


def _is_open_docket(item: DocketItem) -> bool:
    if bool(getattr(item, "is_deleted", False)):
        return False
    return not str(getattr(item, "done_date", "") or "").strip()


def _is_open_workflow(workflow: Workflow) -> bool:
    return str(getattr(workflow, "status", "") or "").strip() not in Workflow.TERMINAL_STATUSES


def _risk_level(score: int) -> str:
    if score >= 80:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def _score_due_dates(
    due_dates: list[date],
    *,
    as_of: date,
    overdue_weight: int,
    urgent_weight: int,
) -> tuple[int, int, int, date | None]:
    overdue = 0
    urgent = 0
    next_due = None
    for due in due_dates:
        if next_due is None or due < next_due:
            next_due = due
        delta = (due - as_of).days
        if delta < 0:
            overdue += 1
        elif delta <= URGENT_DAYS:
            urgent += 1
    return overdue * overdue_weight + urgent * urgent_weight, overdue, urgent, next_due


def _outstanding_score(value: float) -> int:
    if value >= HIGH_OUTSTANDING_USD:
        return 30
    if value >= MEDIUM_OUTSTANDING_USD:
        return 15
    if value > 0:
        return 5
    return 0


def _retained_date(matter: Matter) -> date | None:
    parsed = getattr(matter, "retained_date", None)
    if isinstance(parsed, date):
        return parsed
    return parse_date(getattr(matter, "retained_at", None))


def _data_quality_flags(
    *,
    matter: Matter,
    open_deadline_count: int,
    open_workflow_count: int,
    as_of: date,
) -> list[str]:
    text = " ".join(
        str(v or "").lower()
        for v in (
            getattr(matter, "our_ref", None),
            getattr(matter, "right_name", None),
            getattr(matter, "memo", None),
        )
    )
    flags = []
    if any(token in text for token in ("test", "dummy", "zombie", "", "")):
        flags.append("dummy_keyword")
    retained = _retained_date(matter)
    if (
        retained
        and retained <= as_of - timedelta(days=90)
        and not open_deadline_count
        and not open_workflow_count
    ):
        flags.append("stale_no_open_work")
    return flags


def _matter_ids_for_refresh(matter_ids: list[str] | None, *, limit: int) -> list[str]:
    ids = [str(mid).strip() for mid in (matter_ids or []) if str(mid).strip()]
    if ids:
        return ids[: max(1, limit)]
    q = Matter.query
    if hasattr(Matter, "is_deleted"):
        q = q.filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
    return [row.matter_id for row in q.order_by(Matter.created_at.desc()).limit(limit).all()]


def _mail_review_counts(matter_ids: list[str]) -> dict[str, Counter[str]]:
    if not matter_ids:
        return {}
    statuses = ["REVIEW", "READY", "EXTRACTED"]
    rows = (
        db.session.query(
            func.coalesce(EmailMessage.selected_matter_id, EmailMessage.suggested_matter_id),
            EmailMessage.processing_status,
            func.count(EmailMessage.id),
        )
        .filter(EmailMessage.processing_status.in_(statuses))
        .filter(
            func.coalesce(EmailMessage.selected_matter_id, EmailMessage.suggested_matter_id).in_(
                matter_ids
            )
        )
        .group_by(
            func.coalesce(EmailMessage.selected_matter_id, EmailMessage.suggested_matter_id),
            EmailMessage.processing_status,
        )
        .all()
    )
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for matter_id, status, count in rows:
        mid = str(matter_id or "").strip()
        if not mid:
            continue
        out[mid][str(status or "").upper()] += int(count or 0)
    return out


def _deadline_review_counts(matter_ids: list[str]) -> dict[str, int]:
    if not matter_ids:
        return {}
    rows = (
        db.session.query(DeadlineReviewQueue.matter_id, func.count(DeadlineReviewQueue.id))
        .filter(DeadlineReviewQueue.matter_id.in_(matter_ids))
        .filter(DeadlineReviewQueue.status.in_(["OPEN", "REOPENED"]))
        .group_by(DeadlineReviewQueue.matter_id)
        .all()
    )
    return {str(matter_id): int(count or 0) for matter_id, count in rows}


def _overview_by_matter(matter_ids: list[str]) -> dict[str, VMatterOverview]:
    if not matter_ids:
        return {}
    return {
        str(row.matter_id): row
        for row in VMatterOverview.query.filter(VMatterOverview.matter_id.in_(matter_ids)).all()
    }


def _flat_index_by_matter(matter_ids: list[str]) -> dict[str, CaseFlatIndex]:
    if not matter_ids:
        return {}
    return {
        str(row.matter_id): row
        for row in CaseFlatIndex.query.filter(CaseFlatIndex.matter_id.in_(matter_ids)).all()
    }


def _open_dockets_by_matter(matter_ids: list[str]) -> dict[str, list[DocketItem]]:
    rows = DocketItem.query.filter(DocketItem.matter_id.in_(matter_ids)).all() if matter_ids else []
    out: dict[str, list[DocketItem]] = defaultdict(list)
    for row in rows:
        if _is_open_docket(row):
            out[str(row.matter_id)].append(row)
    return out


def _open_workflows_by_matter(matter_ids: list[str]) -> dict[str, list[Workflow]]:
    rows = Workflow.query.filter(Workflow.case_id.in_(matter_ids)).all() if matter_ids else []
    out: dict[str, list[Workflow]] = defaultdict(list)
    for row in rows:
        if _is_open_workflow(row):
            out[str(row.case_id)].append(row)
    return out


def refresh_matter_risk_facts(
    *,
    matter_ids: list[str] | None = None,
    limit: int = 500,
    as_of: date | None = None,
    commit: bool = False,
) -> dict[str, int]:
    as_of = as_of or _today()
    ids = _matter_ids_for_refresh(matter_ids, limit=max(1, int(limit or 500)))
    if not ids:
        return {"matters": 0, "updated": 0}

    matters = {
        str(row.matter_id): row for row in Matter.query.filter(Matter.matter_id.in_(ids)).all()
    }
    overviews = _overview_by_matter(ids)
    flat_indexes = _flat_index_by_matter(ids)
    dockets_by_matter = _open_dockets_by_matter(ids)
    workflows_by_matter = _open_workflows_by_matter(ids)
    mail_counts = _mail_review_counts(ids)
    review_counts = _deadline_review_counts(ids)

    updated = 0
    for mid in ids:
        matter = matters.get(mid)
        if matter is None or not _is_active_matter(matter):
            continue
        idx = flat_indexes.get(mid)
        overview = overviews.get(mid)
        dockets = dockets_by_matter.get(mid, [])
        workflows = workflows_by_matter.get(mid, [])

        docket_due_dates = [
            due
            for due in (
                effective_due_for_work(row.due_date, row.extended_due_date) for row in dockets
            )
            if due
        ]
        workflow_due_dates = [
            due
            for due in (
                getattr(row, "due_date", None) or getattr(row, "legal_due_date", None)
                for row in workflows
            )
            if due
        ]

        deadline_score, overdue_deadline_count, urgent_deadline_count, next_docket_due = (
            _score_due_dates(
                docket_due_dates,
                as_of=as_of,
                overdue_weight=30,
                urgent_weight=15,
            )
        )
        workflow_score, overdue_workflow_count, urgent_workflow_count, next_workflow_due = (
            _score_due_dates(
                workflow_due_dates,
                as_of=as_of,
                overdue_weight=25,
                urgent_weight=12,
            )
        )
        next_due_candidates = [d for d in (next_docket_due, next_workflow_due) if d]
        next_due = min(next_due_candidates) if next_due_candidates else None

        mail_bucket = mail_counts.get(mid, Counter())
        mail_review_count = int(sum(mail_bucket.values()))
        automation_review_count = int(mail_bucket.get("READY", 0) + mail_bucket.get("REVIEW", 0))
        mail_score = mail_review_count * 10
        automation_score = automation_review_count * 8

        outstanding_total = float(getattr(overview, "outstanding_total", None) or 0.0)
        billing_score = _outstanding_score(outstanding_total)
        deadline_review_count = int(review_counts.get(mid, 0))
        verification_score = deadline_review_count * 25

        dq_flags = _data_quality_flags(
            matter=matter,
            open_deadline_count=len(dockets),
            open_workflow_count=len(workflows),
            as_of=as_of,
        )
        data_quality_score = len(dq_flags) * 20

        score_parts = {
            "deadline": deadline_score + verification_score,
            "workflow": workflow_score,
            "mail": mail_score,
            "billing": billing_score,
            "automation": automation_score,
            "data_quality": data_quality_score,
        }
        score = int(sum(score_parts.values()))

        reasons = []
        if overdue_deadline_count:
            reasons.append({"type": "deadline_overdue", "count": overdue_deadline_count})
        if urgent_deadline_count:
            reasons.append({"type": "deadline_urgent", "count": urgent_deadline_count})
        if overdue_workflow_count:
            reasons.append({"type": "workflow_overdue", "count": overdue_workflow_count})
        if urgent_workflow_count:
            reasons.append({"type": "workflow_urgent", "count": urgent_workflow_count})
        if deadline_review_count:
            reasons.append({"type": "deadline_review", "count": deadline_review_count})
        if automation_review_count:
            reasons.append({"type": "automation_review", "count": automation_review_count})
        if outstanding_total > 0:
            reasons.append({"type": "outstanding", "amount": outstanding_total})
        for flag in dq_flags:
            reasons.append({"type": "data_quality", "flag": flag})

        owner_staff_party_id = None
        if idx is not None:
            owner_staff_party_id = (
                (idx.handler_id or "").strip()
                or (idx.attorney_id or "").strip()
                or (idx.manager_id or "").strip()
                or None
            )
        if not owner_staff_party_id:
            owner_staff_party_id = next(
                (
                    str(getattr(row, "owner_staff_party_id", "") or "").strip()
                    for row in dockets
                    if str(getattr(row, "owner_staff_party_id", "") or "").strip()
                ),
                None,
            )

        fact = db.session.get(MatterRiskFact, mid) or MatterRiskFact(matter_id=mid)
        fact.score = score
        fact.risk_level = _risk_level(score)
        fact.owner_staff_party_id = owner_staff_party_id
        fact.attorney_id = (getattr(idx, "attorney_id", None) or None) if idx else None
        fact.handler_id = (getattr(idx, "handler_id", None) or None) if idx else None
        fact.manager_id = (getattr(idx, "manager_id", None) or None) if idx else None
        fact.team_key = (
            (
                (getattr(idx, "department", None) or "").strip()
                or (getattr(idx, "namespace", None) or "").strip()
                or None
            )
            if idx
            else None
        )
        fact.deadline_score = score_parts["deadline"]
        fact.workflow_score = score_parts["workflow"]
        fact.mail_score = score_parts["mail"]
        fact.billing_score = score_parts["billing"]
        fact.automation_score = score_parts["automation"]
        fact.data_quality_score = score_parts["data_quality"]
        fact.overdue_deadline_count = overdue_deadline_count
        fact.urgent_deadline_count = urgent_deadline_count
        fact.overdue_workflow_count = overdue_workflow_count
        fact.urgent_workflow_count = urgent_workflow_count
        fact.mail_review_count = mail_review_count
        fact.automation_review_count = automation_review_count
        fact.deadline_review_count = deadline_review_count
        fact.outstanding_total = outstanding_total
        fact.next_due_date = next_due
        fact.risk_reasons_json = reasons
        fact.facts_json = {
            "as_of": as_of.isoformat(),
            "score_parts": score_parts,
            "open_docket_count": len(dockets),
            "open_workflow_count": len(workflows),
            "data_quality_flags": dq_flags,
            "our_ref": getattr(matter, "our_ref", None),
            "right_name": getattr(matter, "right_name", None),
        }
        fact.computed_at = datetime.utcnow()
        db.session.add(fact)
        updated += 1

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return {"matters": len(ids), "updated": updated}


def risk_queue_query(*, owner_staff_party_id: str | None = None, team_key: str | None = None):
    q = (
        db.session.query(MatterRiskFact, Matter, CaseFlatIndex)
        .join(Matter, MatterRiskFact.matter_id == Matter.matter_id)
        .outerjoin(CaseFlatIndex, CaseFlatIndex.matter_id == MatterRiskFact.matter_id)
    )
    if owner_staff_party_id:
        pid = owner_staff_party_id.strip()
        q = q.filter(
            or_(
                MatterRiskFact.owner_staff_party_id == pid,
                MatterRiskFact.attorney_id == pid,
                MatterRiskFact.handler_id == pid,
                MatterRiskFact.manager_id == pid,
            )
        )
    if team_key:
        q = q.filter(MatterRiskFact.team_key == team_key.strip())
    return q.order_by(
        MatterRiskFact.score.desc(),
        MatterRiskFact.next_due_date.is_(None),
        MatterRiskFact.next_due_date.asc(),
        MatterRiskFact.computed_at.desc(),
    )
