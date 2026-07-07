from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import bindparam, or_

from app.extensions import db
from app.models.matter_facts import MatterFacts
from app.models.matter import Matter
from app.models.workflow import Workflow
from app.services.annuity.annuity_visibility import get_visible_cycle_count
from app.services.case.terminal_status import is_future_term_expiry_status
from app.services.workflow.task_sync import (
    _append_note_marker,
    _current_staff_snapshot,
    _resolve_assignee_id,
    _resolve_primary_staff_party_id_from_matter,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.renewal_labels import (
    normalize_renewal_jurisdiction,
    normalize_renewal_right_type,
    renewal_workflow_name,
)

logger = logging.getLogger(__name__)

_ANNUITY_BC_PREFIX = "ANNUITY:"
_ANNUITY_NOTE_AUTO_PRUNED = "[Renewal Auto]"
_ANNUITY_NOTE_DELETED = "[Renewal Delete]"
_ANNUITY_NOTE_MANAGEMENT_DISABLED = "[Renewal Management Disabled]"
_ANNUITY_AUTO_NOTE_TAGS = frozenset(
    {
        _ANNUITY_NOTE_AUTO_PRUNED,
        _ANNUITY_NOTE_DELETED,
        _ANNUITY_NOTE_MANAGEMENT_DISABLED,
        "[Renewal  ]",
    }
)


def _strip_annuity_auto_note_tags(note: str | None) -> str | None:
    lines = [str(line or "").strip() for line in str(note or "").splitlines()]
    kept: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line:
            continue
        if line in _ANNUITY_AUTO_NOTE_TAGS:
            continue
        if line.startswith("[Matter Status Change:") and "Task " in line:
            continue
        if line in seen:
            continue
        seen.add(line)
        kept.append(line)
    return "\n".join(kept) if kept else None


def _annuity_right_type(ai: Any, matter: Matter | None) -> str | None:
    mid = str(getattr(ai, "matter_id", "") or "").strip()
    facts = MatterFacts.query.get(mid) if mid else None
    return normalize_renewal_right_type(
        getattr(facts, "right_type_norm", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "right_group", None),
        getattr(matter, "our_ref", None),
    )


def _annuity_jurisdiction(ai: Any, matter: Matter | None) -> str | None:
    return normalize_renewal_jurisdiction(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "our_ref", None),
    )


def _abandon_annuity_workflows(*, workflow_ids: list[int], today: date, note_tag: str) -> None:
    """Close annuity workflows in bulk without triggering model-level side effects."""
    if not workflow_ids:
        return
    db.session.execute(
        text(
            """
            UPDATE workflows
            SET status = 'Abandoned',
                completed_date = :today,
                note = CASE
                    WHEN note IS NULL OR TRIM(note) = '' THEN :tag
                    WHEN note LIKE '%' || :tag || '%' THEN note
                    ELSE note || '\n' || :tag
                END
            WHERE id IN :ids
              AND status NOT IN ('Completed','Abandoned')
            """
        )
        .bindparams(bindparam("ids", expanding=True))
        .execution_options(policy_bypass=True),
        {"today": today, "tag": note_tag, "ids": workflow_ids},
    )


def _annuity_business_code(matter_id: str, cycle_no: int | None) -> str | None:
    mid = (matter_id or "").strip()
    if not mid:
        return None
    try:
        cycle_int = int(cycle_no)
    except (TypeError, ValueError):
        return None
    if cycle_int <= 0:
        return None
    return f"{_ANNUITY_BC_PREFIX}{mid}:{cycle_int}"


def _terminal_case_status_for_matter(matter_id: str | None) -> str:
    mid = (matter_id or "").strip()
    if not mid:
        return ""

    matter = Matter.query.get(mid)
    if matter is None:
        return ""

    try:
        from app.services.case.terminal_status import is_terminal_case_status
    except Exception:
        return ""

    today = date.today()

    for raw in (
        getattr(matter, "inhouse_status", None),
        getattr(matter, "status_blue", None),
    ):
        status = (raw or "").strip()
        if status and is_terminal_case_status(status):
            return status

    red_status = (getattr(matter, "status_red", None) or "").strip()
    if is_future_term_expiry_status(
        red_status,
        getattr(matter, "status_red_related_date", None),
        today=today,
    ):
        return ""
    if red_status and is_terminal_case_status(red_status):
        return red_status
    return ""


def sync_from_annuity_item(annuity_id: str | int) -> None:
    from app.models.ip_records import AnnuityItem

    ai = AnnuityItem.query.get(str(annuity_id))
    if not ai:
        return

    sync_annuity_workflows_for_matter(str(getattr(ai, "matter_id", "") or ""))


def sync_annuity_workflows_for_matter(matter_id: str) -> None:
    """Rebuild annuity workflows for a matter."""
    from app.models.ip_records import AnnuityItem
    from app.services.annuity.annuity_management import is_annuity_management_disabled_for_matter
    from app.services.annuity.annuity_policy import (
        compute_status,
        effective_due_date_str,
        parse_date,
    )

    mid = (matter_id or "").strip()
    if not mid:
        return

    visible_n = get_visible_cycle_count()
    today = date.today()

    terminal_status = _terminal_case_status_for_matter(mid)
    if terminal_status:
        rows = (
            Workflow.query.filter(Workflow.business_code.like(f"{_ANNUITY_BC_PREFIX}{mid}:%"))
            .filter(
                or_(Workflow.status.is_(None), Workflow.status.notin_(("Completed", "Abandoned")))
            )
            .all()
        )
        changed_ids: list[int] = []
        closure_marker = f"[Matter Status Change:{terminal_status}] Task closed"
        for wf in rows or []:
            wf.status = "Abandoned"
            wf.completed_date = today
            wf.note = _append_note_marker(wf.note, closure_marker)
            db.session.add(wf)
            if getattr(wf, "id", None):
                changed_ids.append(int(wf.id))

        if changed_ids:
            from app.services.workflow.sync_requests import enqueue_workflow_sync

            for wf_id in changed_ids:
                try:
                    enqueue_workflow_sync(workflow_id=int(wf_id))
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="task_sync.annuity.enqueue_terminal_workflow_sync",
                        log_key="task_sync.annuity.enqueue_terminal_workflow_sync",
                        log_window_seconds=300,
                    )
        return

    if is_annuity_management_disabled_for_matter(mid):
        rows = (
            db.session.query(Workflow.id)
            .filter(Workflow.business_code.like(f"{_ANNUITY_BC_PREFIX}{mid}:%"))
            .filter(Workflow.status.notin_(("Completed", "Abandoned")))
            .all()
        )
        hide_ids = [int(wf_id) for (wf_id,) in (rows or []) if wf_id]
        if hide_ids:
            _abandon_annuity_workflows(
                workflow_ids=hide_ids,
                today=today,
                note_tag=_ANNUITY_NOTE_MANAGEMENT_DISABLED,
            )
            from app.services.workflow.sync_requests import enqueue_workflow_sync

            for wf_id in hide_ids:
                try:
                    enqueue_workflow_sync(workflow_id=int(wf_id))
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="task_sync.annuity.enqueue_disabled_workflow_sync",
                        log_key="task_sync.annuity.enqueue_disabled_workflow_sync",
                        log_window_seconds=300,
                    )
        return

    items = (
        AnnuityItem.query.filter_by(matter_id=mid)
        .filter(or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None)))
        .all()
    )
    by_bc: dict[str, Any] = {}
    candidates: list[tuple[date, int, str]] = []

    for ai in items:
        bc = _annuity_business_code(ai.matter_id, ai.cycle_no)
        if not bc:
            logger.warning(
                "Skipping annuity workflow sync: missing cycle_no (matter_id=%s, annuity_id=%s)",
                ai.matter_id,
                getattr(ai, "annuity_id", None),
            )
            continue

        by_bc[bc] = ai

        st = compute_status(ai, today=today)
        if st not in ("pending", "overdue"):
            continue

        eff_str = effective_due_date_str(ai)
        eff_due = parse_date(eff_str) if eff_str else None
        if not eff_due:
            continue
        cycle_no = 0
        try:
            cycle_no = int(getattr(ai, "cycle_no", None) or 0)
        except Exception:
            cycle_no = 0
        candidates.append((eff_due, cycle_no, bc))

    desired_open_bcs: set[str] = set()
    ordered_open_bcs: list[str] = []
    if candidates:
        upcoming = [t for t in candidates if t[0] >= today]
        if upcoming:
            upcoming.sort(key=lambda t: (t[0], t[1]))
            anchor_cycle = upcoming[0][1]
            selected = sorted(
                (t for t in candidates if t[1] >= anchor_cycle),
                key=lambda t: (t[1], t[0]),
            )[:visible_n]
        else:
            selected = sorted(candidates, key=lambda t: (t[0], t[1]), reverse=True)[:visible_n]

        ordered_open_bcs = [t[2] for t in selected]
        desired_open_bcs = set(ordered_open_bcs)

    for bc in ordered_open_bcs:
        ai = by_bc.get(bc)
        if ai is not None:
            _upsert_workflow_for_annuity(ai)

    rows = (
        db.session.query(Workflow.id, Workflow.business_code, Workflow.status)
        .filter(Workflow.business_code.like(f"{_ANNUITY_BC_PREFIX}{mid}:%"))
        .all()
    )

    hide_auto_ids: list[int] = []
    hide_deleted_ids: list[int] = []
    for wf_id, bc, status in rows or []:
        if not wf_id:
            continue
        bc_str = (bc or "").strip()
        if not bc_str:
            continue
        wf_status = (status or "").strip()
        if wf_status in ("Completed", "Abandoned") or bc_str in desired_open_bcs:
            continue

        ai = by_bc.get(bc_str)
        if ai is not None:
            st = compute_status(ai, today=today)
            if st in ("paid", "giveup"):
                _upsert_workflow_for_annuity(ai)
                continue

        wf_id_int = int(wf_id)
        if ai is None:
            hide_deleted_ids.append(wf_id_int)
        else:
            hide_auto_ids.append(wf_id_int)

    hide_ids = hide_auto_ids + hide_deleted_ids
    if hide_ids:
        try:
            if hide_auto_ids:
                _abandon_annuity_workflows(
                    workflow_ids=hide_auto_ids,
                    today=today,
                    note_tag=_ANNUITY_NOTE_AUTO_PRUNED,
                )
            if hide_deleted_ids:
                _abandon_annuity_workflows(
                    workflow_ids=hide_deleted_ids,
                    today=today,
                    note_tag=_ANNUITY_NOTE_DELETED,
                )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="task_sync.annuity.hide_workflows",
                log_key="task_sync.annuity.hide_workflows",
                log_window_seconds=300,
            )

        from app.services.workflow.sync_requests import enqueue_workflow_sync

        for wf_id in hide_ids:
            try:
                enqueue_workflow_sync(workflow_id=int(wf_id))
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="task_sync.annuity.enqueue_workflow_sync",
                    log_key="task_sync.annuity.enqueue_workflow_sync",
                    log_window_seconds=300,
                )


def _upsert_workflow_for_annuity(ai: Any) -> None:
    from app.services.annuity.annuity_policy import (
        compute_status,
        effective_due_date_str,
        parse_date,
    )

    bc = _annuity_business_code(ai.matter_id, ai.cycle_no)
    if not bc:
        logger.warning(
            "Skipping annuity workflow upsert: missing cycle_no (matter_id=%s, annuity_id=%s)",
            getattr(ai, "matter_id", None),
            getattr(ai, "annuity_id", None),
        )
        return

    snap = _current_staff_snapshot(str(ai.matter_id))
    snapshot_attorney = (snap.get("attorney") or "").strip()
    snapshot_handler = (snap.get("handler") or "").strip()
    snapshot_manager = (snap.get("manager") or "").strip()

    wf = Workflow.query.filter_by(business_code=bc).first()
    if not wf:
        wf = Workflow(business_code=bc, case_id=str(ai.matter_id))

    matter = Matter.query.get(str(ai.matter_id)) if getattr(ai, "matter_id", None) else None
    right_type = _annuity_right_type(ai, matter)
    jurisdiction = _annuity_jurisdiction(ai, matter)
    wf.name = renewal_workflow_name(ai.cycle_no, right_type=right_type, jurisdiction=jurisdiction)
    wf.category = getattr(wf, "category", None) or "MGMT"

    legal_due = parse_date(getattr(ai, "due_date", None))
    eff_str = effective_due_date_str(ai)
    effective_due = parse_date(eff_str) if eff_str else None
    wf.legal_due_date = legal_due
    wf.due_date = effective_due or legal_due

    st = compute_status(ai, today=date.today())
    if st == "paid":
        wf.status = "Completed"
        wf.completed_date = parse_date(getattr(ai, "paid_date", None)) or date.today()
    elif st == "giveup":
        wf.status = "Abandoned"
        wf.completed_date = date.today()
    else:
        wf.status = "Pending"
        wf.completed_date = None
        wf.note = _strip_annuity_auto_note_tags(getattr(wf, "note", None))

    owner_staff_party_id = (getattr(ai, "owner_staff_party_id", None) or "").strip() or None
    if not owner_staff_party_id:
        try:
            owner_staff_party_id = _resolve_primary_staff_party_id_from_matter(
                str(getattr(ai, "matter_id", "") or ""),
                prefer_mgmt=True,
            )
        except Exception:
            owner_staff_party_id = None
        if owner_staff_party_id:
            ai.owner_staff_party_id = owner_staff_party_id
            db.session.add(ai)
    resolved_assignee_id = _resolve_assignee_id(owner_staff_party_id)
    if resolved_assignee_id is not None or getattr(wf, "assignee_id", None) is None:
        wf.assignee_id = resolved_assignee_id

    db.session.add(wf)

    if snapshot_attorney:
        wf.snapshot_attorney = snapshot_attorney
    if snapshot_handler:
        wf.snapshot_handler = snapshot_handler
    if snapshot_manager:
        wf.snapshot_manager = snapshot_manager

    if not wf.id:
        try:
            db.session.flush()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="task_sync.annuity.flush_workflow",
                log_key="task_sync.annuity.flush_workflow",
                log_window_seconds=300,
            )

    if wf.id:
        try:
            from app.services.workflow.sync_requests import enqueue_workflow_sync

            enqueue_workflow_sync(workflow_id=wf.id)
        except Exception as exc:
            logger.warning("Failed to enqueue workflow sync for annuity %s: %s", ai.matter_id, exc)
