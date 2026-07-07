from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.ip_records import DocketItem
from app.models.workflow import Workflow
from app.services.case.status_task_cleanup import reconcile_terminal_case_open_items
from app.services.workflow.status_sync import (
    linked_docket_item_for_workflow,
    reconcile_linked_docket_workflow_fields,
    sync_linked_docket_done_date_from_workflow,
)
from app.services.workflow.task_sync import (
    _apply_docket_updates,
    _backfill_owner_for_known_auto_dockets,
    _effective_due,
    _legal_due,
    ensure_workflow_for_docket,
    ensure_worklog_for_docket,
)
from app.utils.docket_dates import effective_due_text_expr
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

_LAST_BACKFILL_AT: datetime | None = None
URGENT_WINDOW_DAYS = 7


def _effective_docket_due_expr():
    # DocketItem stores due dates as ISO strings (YYYY-MM-DD). Keep comparisons lexicographic.
    return effective_due_text_expr(DocketItem, dialect_name=getattr(db.engine.dialect, "name", ""))


def _extract_docket_id_from_business_code(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if not upper.startswith("DOCKET:"):
        return ""
    rest = raw.split(":", 1)[1].strip()
    if not rest:
        return ""
    return rest.split(":", 1)[0].strip()


def _worklog_mismatched_docket_ids(*, limit: int) -> list[str]:
    safe_limit = max(1, min(int(limit or 0), 5000))
    sql = text(
        """
        SELECT DISTINCT wl.docket_id
        FROM work_logs wl
        JOIN docket_item d ON d.docket_id = wl.docket_id
        WHERE wl.docket_id IS NOT NULL
          AND TRIM(wl.docket_id) <> ''
          AND (
            (
              COALESCE(TRIM(d.done_date), '') <> ''
              AND LOWER(COALESCE(TRIM(wl.status), '')) NOT IN ('completed', 'abandoned')
            )
            OR (
              COALESCE(TRIM(d.done_date), '') = ''
              AND LOWER(COALESCE(TRIM(wl.status), '')) IN ('completed', 'abandoned')
            )
          )
        ORDER BY wl.docket_id ASC
        LIMIT :limit
        """
    )
    return [
        (row[0] or "").strip()
        for row in db.session.execute(sql, {"limit": safe_limit}).fetchall()
        if (row[0] or "").strip()
    ]


def _reconcile_docket_workflow_field_drift(*, limit: int) -> int:
    safe_limit = max(1, min(int(limit or 0), 5000))
    changed = 0
    workflow_batch: list[Workflow] = []

    def _flush_batch(batch: list[Workflow]) -> int:
        if not batch:
            return 0
        docket_ids = {
            _extract_docket_id_from_business_code(getattr(workflow, "business_code", None))
            for workflow in batch
        }
        docket_ids.discard("")
        if not docket_ids:
            return 0
        docket_map = {
            docket.docket_id: docket
            for docket in DocketItem.query.filter(DocketItem.docket_id.in_(list(docket_ids))).all()
        }

        batch_changed = 0
        for workflow in batch:
            docket_id = _extract_docket_id_from_business_code(
                getattr(workflow, "business_code", None)
            )
            docket_item = docket_map.get(docket_id)
            if docket_item is None:
                continue
            if reconcile_linked_docket_workflow_fields(
                workflow,
                linked_docket_item=docket_item,
            ):
                batch_changed += 1
        return batch_changed

    rows = (
        Workflow.query.filter(Workflow.business_code.like("DOCKET:%"))
        .order_by(Workflow.id.asc())
        .yield_per(200)
    )
    for workflow in rows:
        workflow_batch.append(workflow)
        if len(workflow_batch) < 200:
            continue
        changed += _flush_batch(workflow_batch)
        workflow_batch = []
        if changed >= safe_limit:
            return changed

    if workflow_batch and changed < safe_limit:
        changed += _flush_batch(workflow_batch)
    return changed


def _reconcile_open_dockets_for_terminal_workflows(*, limit: int) -> int:
    safe_limit = max(1, min(int(limit or 0), 5000))
    changed = 0

    rows = (
        Workflow.query.filter(Workflow.business_code.like("DOCKET:%"))
        .filter(Workflow.status.in_(("Completed", "Abandoned")))
        .order_by(Workflow.id.asc())
        .yield_per(200)
    )
    for workflow in rows:
        before_done = ""
        linked_before = linked_docket_item_for_workflow(workflow)
        if linked_before is not None:
            before_done = (getattr(linked_before, "done_date", None) or "").strip()

        docket_item = sync_linked_docket_done_date_from_workflow(
            workflow,
            completed_on=getattr(workflow, "completed_date", None),
        )
        if docket_item is None:
            continue
        after_done = (getattr(docket_item, "done_date", None) or "").strip()
        if not after_done:
            continue

        ensure_worklog_for_docket(docket_item=docket_item, actor_id=None)
        if before_done != after_done:
            changed += 1
        if changed >= safe_limit:
            break

    return changed


def backfill_workflows_from_open_dockets(
    *,
    today: date | None = None,
    end_date: date | None = None,
    bucket: str = "",
    limit: int = 200,
    commit: bool = True,
    throttle_seconds: int | None = None,
) -> int:
    """
    Ensure Workflow rows exist for open DocketItem rows.

    Intended for background/scheduler use (avoid calling from GET requests).
    Returns number of docket items processed (best-effort).
    """
    enabled = current_app.config.get("WORKLOG_AUTO_BACKFILL_FROM_DOCKETS_ENABLED", False)
    if not enabled:
        return 0

    global _LAST_BACKFILL_AT
    if throttle_seconds is not None and throttle_seconds > 0:
        now = datetime.utcnow()
        if _LAST_BACKFILL_AT and (now - _LAST_BACKFILL_AT).total_seconds() < throttle_seconds:
            return 0
        _LAST_BACKFILL_AT = now

    today = today or date.today()
    if end_date is None:
        lookahead_days = current_app.config.get("WORKLOG_AUTO_BACKFILL_LOOKAHEAD_DAYS", 30) or 30
        try:
            lookahead_days = int(lookahead_days)
        except Exception:
            lookahead_days = 30
        lookahead_days = max(0, min(3650, lookahead_days))
        end_date = today + timedelta(days=lookahead_days)

    try:
        processed = 0
        overdue_window_days = current_app.config.get(
            "WORKLOG_AUTO_BACKFILL_OVERDUE_WINDOW_DAYS", 3650
        )
        try:
            overdue_window_days = int(overdue_window_days)
        except (TypeError, ValueError):
            overdue_window_days = 365
        overdue_window_days = max(0, min(overdue_window_days, 9999))
        from_date = today - timedelta(days=overdue_window_days)
        safe_limit = max(1, min(int(limit or 0), 2000))

        try:
            terminal_cleanup = reconcile_terminal_case_open_items(
                limit=safe_limit,
                commit=False,
                logger_override=current_app.logger,
            )
            processed += (
                terminal_cleanup.docket_closed
                + terminal_cleanup.workflow_closed
                + terminal_cleanup.worklog_closed
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_terminal_cases",
                log_key="workflow.docket_backfill.reconcile_terminal_cases",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_terminal_cases.rollback",
                    log_key="workflow.docket_backfill.reconcile_terminal_cases.rollback",
                    log_window_seconds=300,
                )

        # Recovery path first: if a DOCKET workflow was already terminal but the linked
        # docket/worklog stayed open, re-apply the workflow terminal state before we
        # scan open dockets for workflow creation. Otherwise the open-docket pass can
        # incorrectly reopen a terminal workflow.
        try:
            processed += _reconcile_open_dockets_for_terminal_workflows(limit=safe_limit)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_terminal_workflows",
                log_key="workflow.docket_backfill.reconcile_terminal_workflows",
                log_window_seconds=300,
            )
            processed = 0
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_terminal_workflows.rollback",
                    log_key="workflow.docket_backfill.reconcile_terminal_workflows.rollback",
                    log_window_seconds=300,
                )

        effective_due = _effective_docket_due_expr()
        visible_from = func.nullif(func.trim(DocketItem.visible_from_date), "")
        q = (
            db.session.query(DocketItem)
            .filter(or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""))
            .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
            .filter(effective_due.isnot(None), effective_due != "")
        )

        bucket = (bucket or "").strip().lower()
        if bucket == "urgent":
            urgent_date = today + timedelta(days=URGENT_WINDOW_DAYS)
            q = q.filter(
                effective_due >= today.isoformat(),
                effective_due <= urgent_date.isoformat(),
            )
        elif bucket == "overdue":
            q = q.filter(
                effective_due >= from_date.isoformat(),
                effective_due < today.isoformat(),
            )
        else:
            # Default window: by due date (look-ahead) OR explicit visible_from_date gate.
            # This allows long-horizon deadlines to "enter Task" earlier than due-date lookahead
            # when the docket explicitly sets visible_from_date (e.g., ForeignFilingDeadline 1items ).
            q = q.filter(effective_due >= from_date.isoformat())
            q = q.filter(
                or_(
                    effective_due <= end_date.isoformat(),
                    and_(visible_from.isnot(None), visible_from <= today.isoformat()),
                )
            )

        items = q.order_by(effective_due.asc()).limit(safe_limit).all()

        for di in items or []:
            try:
                _backfill_owner_for_known_auto_dockets(di)
                workflows = ensure_workflow_for_docket(docket_item=di, created_by_id=None)
                ensure_worklog_for_docket(docket_item=di, actor_id=None)
                if workflows:
                    # Ensure calendar sync can pick up newly-created workflows.
                    from app.services.workflow.sync_requests import enqueue_workflow_sync

                    for wf in workflows:
                        wf_id = getattr(wf, "id", None)
                        if wf_id:
                            enqueue_workflow_sync(workflow_id=int(wf_id))
                processed += 1
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="workflow.docket_backfill.ensure_workflow_for_docket",
                    log_key="workflow.docket_backfill.ensure_workflow_for_docket",
                    log_window_seconds=300,
                )
                try:
                    db.session.rollback()
                except Exception as rollback_exc:
                    report_swallowed_exception(
                        rollback_exc,
                        context="workflow.docket_backfill.ensure_workflow_for_docket.rollback",
                        log_key="workflow.docket_backfill.ensure_workflow_for_docket.rollback",
                        log_window_seconds=300,
                    )

        # Recovery path: if a docket was closed (done/auto-cancelled/deleted) but a linked
        # DOCKET workflow is still non-terminal, re-sync from docket state.
        try:
            stale_workflows = (
                db.session.query(Workflow)
                .filter(Workflow.business_code.like("DOCKET:%"))
                .filter(Workflow.status.notin_(("Completed", "Abandoned")))
                .order_by(Workflow.id.asc())
                .all()
            )
            stale_ids = {
                did
                for wf in stale_workflows
                for did in [
                    _extract_docket_id_from_business_code(getattr(wf, "business_code", None))
                ]
                if did
            }
            if stale_ids:
                docket_map = {
                    row.docket_id: row
                    for row in DocketItem.query.filter(
                        DocketItem.docket_id.in_(list(stale_ids))
                    ).all()
                }
                for wf in stale_workflows:
                    did = _extract_docket_id_from_business_code(getattr(wf, "business_code", None))
                    if not did:
                        continue
                    di = docket_map.get(did)
                    if not di:
                        continue
                    is_deleted = bool(getattr(di, "is_deleted", False))
                    if not is_deleted and not (di.done_date or "").strip():
                        continue

                    task_name = (di.name_free or di.name_ref or "Task").strip()
                    _apply_docket_updates(
                        wf=wf,
                        docket_item=di,
                        task_name=task_name,
                        due_date=_effective_due(di),
                        legal_due=_legal_due(di),
                        assignee_id=getattr(wf, "assignee_id", None),
                    )
                    db.session.add(wf)
                    processed += 1
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_closed_dockets",
                log_key="workflow.docket_backfill.reconcile_closed_dockets",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_closed_dockets.rollback",
                    log_key="workflow.docket_backfill.reconcile_closed_dockets.rollback",
                    log_window_seconds=300,
                )

        # Recovery path: if a docket and its WorkLog drifted out of sync, re-apply
        # the current docket terminal/open state to the single canonical WorkLog row.
        try:
            mismatched_ids = _worklog_mismatched_docket_ids(limit=safe_limit)
            if mismatched_ids:
                docket_map = {
                    row.docket_id: row
                    for row in DocketItem.query.filter(
                        DocketItem.docket_id.in_(list(mismatched_ids))
                    ).all()
                }
                for did in mismatched_ids:
                    di = docket_map.get(did)
                    if not di:
                        continue
                    ensure_worklog_for_docket(docket_item=di, actor_id=None)
                    processed += 1
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_worklog_status",
                log_key="workflow.docket_backfill.reconcile_worklog_status",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_worklog_status.rollback",
                    log_key="workflow.docket_backfill.reconcile_worklog_status.rollback",
                    log_window_seconds=300,
                )

        # Run the terminal-workflow recovery again after the open-docket pass.
        # Earlier recoveries can be rolled back by later per-item failures, and this
        # second pass re-applies the terminal state without relying on scheduler retries.
        try:
            processed += _reconcile_open_dockets_for_terminal_workflows(limit=safe_limit)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_terminal_workflows.final",
                log_key="workflow.docket_backfill.reconcile_terminal_workflows.final",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_terminal_workflows.final.rollback",
                    log_key="workflow.docket_backfill.reconcile_terminal_workflows.final.rollback",
                    log_window_seconds=300,
                )

        # Recovery path: keep linked workflow due fields aligned with docket-side
        # deadline changes while preserving workflow-side manual edits when the
        # source docket dates are unchanged.
        try:
            processed += _reconcile_docket_workflow_field_drift(limit=safe_limit)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="workflow.docket_backfill.reconcile_docket_workflow_field_drift",
                log_key="workflow.docket_backfill.reconcile_docket_workflow_field_drift",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="workflow.docket_backfill.reconcile_docket_workflow_field_drift.rollback",
                    log_key="workflow.docket_backfill.reconcile_docket_workflow_field_drift.rollback",
                    log_window_seconds=300,
                )

        if commit:
            try:
                db.session.commit()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="workflow.docket_backfill.commit",
                    log_key="workflow.docket_backfill.commit",
                    log_window_seconds=300,
                )
                try:
                    db.session.rollback()
                except Exception as rollback_exc:
                    report_swallowed_exception(
                        rollback_exc,
                        context="workflow.docket_backfill.commit.rollback",
                        log_key="workflow.docket_backfill.commit.rollback",
                        log_window_seconds=300,
                    )
        return processed
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="workflow.docket_backfill",
            log_key="workflow.docket_backfill",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="workflow.docket_backfill.rollback",
                log_key="workflow.docket_backfill.rollback",
                log_window_seconds=300,
            )
        return 0
