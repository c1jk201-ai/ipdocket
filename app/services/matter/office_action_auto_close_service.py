from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from flask import current_app
from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.communication import OfficeAction
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.services.matter.matter_auto_status import (
    _fetch_office_action_superseding_dates,
    date_only_str,
    get_handled_open_office_action_done_dates,
    normalize_red_status,
)
from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

_OA_ID_FROM_NAME_REF_RE = re.compile(r"^(?:MGMT:)?NOTICE:OA:([^:]+)", re.IGNORECASE)
_USPTO_ID_FROM_NAME_REF_RE = re.compile(r"^USPTO:([^:]+)$", re.IGNORECASE)
_STATUS_RED_NAME_REF_PREFIX = "MGMT:STATUS_RED:"
_SUPERSEDED_NON_RESPONSE_DEADLINE_TOKENS = (
    "Period",
    "Period",
    "Period",
    "StatutoryPeriod",
    "StatutoryPeriod",
    "",
    "target",
    "target",
)


def _parse_memo_json(value: str | None) -> dict:
    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service._parse_memo_json",
            log_key="office_action_auto_close_service._parse_memo_json",
            log_window_seconds=300,
        )
        return {}
    return obj if isinstance(obj, dict) else {}


def _extract_office_action_id_from_docket(d: Any) -> str | None:
    memo = _parse_memo_json(getattr(d, "memo", None))
    if (memo.get("trigger") or "").strip() == "office_action_due":
        oa_id = (memo.get("oa_id") or "").strip()
        if oa_id:
            return oa_id

    name_ref = (getattr(d, "name_ref", None) or "").strip()
    if name_ref:
        m = _OA_ID_FROM_NAME_REF_RE.match(name_ref)
        if m:
            oa_id = (m.group(1) or "").strip()
            if oa_id:
                return oa_id
        m2 = _USPTO_ID_FROM_NAME_REF_RE.match(name_ref)
        if m2:
            oa_id = (m2.group(1) or "").strip()
            if oa_id:
                return oa_id

    # Legacy migration pattern: OA-derived docket items may have `category=V2_LIMIT`
    # and reuse `docket_id == office_action.oa_id`.
    cat = (getattr(d, "category", None) or "").strip().upper()
    if cat == "V2_LIMIT":
        did = (getattr(d, "docket_id", None) or getattr(d, "id", None) or "").strip()
        return did or None

    return None


def _effective_due_token(row: Any) -> str:
    return (getattr(row, "extended_due_date", None) or "").strip() or (
        getattr(row, "due_date", None) or ""
    ).strip()


def _parse_auto_close_date(value: Any) -> date | None:
    token = date_only_str(str(value) if value is not None else "")
    if not token:
        return None
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def _looks_like_superseded_non_response_deadline_label(value: str | None) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    if not compact:
        return False
    if any(token in compact for token in _SUPERSEDED_NON_RESPONSE_DEADLINE_TOKENS):
        return True
    return "Period" in compact and ("Guidance" in compact or compact.endswith("Guidance"))


def _get_superseded_non_response_office_action_done_dates(matter_id: str) -> dict[str, str]:
    mid = (matter_id or "").strip()
    if not mid:
        return {}

    progress_dates = _fetch_office_action_superseding_dates(mid)
    if not progress_dates:
        return {}

    try:
        rows = (
            db.session.execute(
                text(
                    """
                SELECT oa_id, doc_name, received_date, notified_date, due_date, extended_due_date
                FROM office_action
                WHERE matter_id = :mid
                  AND (done_date IS NULL OR TRIM(done_date) = '')
                  AND doc_name IS NOT NULL
                  AND TRIM(doc_name) <> ''
                  AND (
                    (due_date IS NOT NULL AND TRIM(due_date) <> '')
                    OR (extended_due_date IS NOT NULL AND TRIM(extended_due_date) <> '')
                  )
                """
                ).execution_options(policy_bypass=True),
                {"mid": mid},
            )
            .mappings()
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.query_superseded_non_response_deadlines",
            log_key="office_action_auto_close_service.query_superseded_non_response_deadlines",
            log_window_seconds=300,
        )
        return {}

    out: dict[str, str] = {}
    for row in rows:
        oa_id = (row.get("oa_id") or "").strip()
        doc_name = row.get("doc_name") or ""
        if not oa_id or not _looks_like_superseded_non_response_deadline_label(doc_name):
            continue
        anchor = (
            _parse_auto_close_date(row.get("notified_date"))
            or _parse_auto_close_date(row.get("received_date"))
            or _parse_auto_close_date(row.get("extended_due_date"))
            or _parse_auto_close_date(row.get("due_date"))
        )
        if not anchor:
            continue
        later = [dt for dt in progress_dates if dt > anchor]
        if later:
            out[oa_id] = min(later).strftime("%Y-%m-%d")
    return out


def _status_red_label_for_docket(docket_item: DocketItem) -> str:
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip()
    if name_ref.upper().startswith(_STATUS_RED_NAME_REF_PREFIX):
        return normalize_red_status(name_ref[len(_STATUS_RED_NAME_REF_PREFIX) :].strip())
    return normalize_red_status(getattr(docket_item, "name_free", None))


def _sync_docket_item_state(docket_item: DocketItem) -> None:
    synced = False
    try:
        from app.services.workflow.task_sync import sync_from_docket_item

        sync_from_docket_item(docket_item=docket_item, actor_id=None)
        synced = True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.sync_from_docket_item",
            log_key="office_action_auto_close_service.sync_from_docket_item",
            log_window_seconds=300,
        )

    if synced:
        return

    try:
        from app.services.workflow.sync_requests import enqueue_docket_sync_for_item

        enqueue_docket_sync_for_item(docket_item=docket_item)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.enqueue_docket_sync",
            log_key="office_action_auto_close_service.enqueue_docket_sync",
            log_window_seconds=300,
        )


def _close_matching_status_red_dockets(*, matter_id: str, done_by_oa: dict[str, str]) -> int:
    mid = (matter_id or "").strip()
    if not mid or not done_by_oa:
        return 0

    try:
        oa_rows = (
            OfficeAction.query.filter(OfficeAction.matter_id == mid)
            .filter(OfficeAction.oa_id.in_(list(done_by_oa.keys())))
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.query_matching_office_actions",
            log_key="office_action_auto_close_service.query_matching_office_actions",
            log_window_seconds=300,
        )
        return 0

    done_by_label_due: dict[tuple[str, str], str] = {}
    done_by_label: dict[str, set[str]] = {}
    for oa in oa_rows:
        done = (done_by_oa.get(str(getattr(oa, "oa_id", "") or "")) or "").strip()
        if not done:
            continue
        label = normalize_red_status(getattr(oa, "doc_name", None))
        if not label:
            continue
        due_token = _effective_due_token(oa)
        if due_token:
            key = (label, due_token)
            prev = done_by_label_due.get(key)
            if not prev or done > prev:
                done_by_label_due[key] = done
        done_by_label.setdefault(label, set()).add(done)

    if not done_by_label_due and not done_by_label:
        return 0

    try:
        q = db.session.query(DocketItem).filter(DocketItem.matter_id == mid)
        q = q.filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
        q = q.filter(DocketItem.name_ref.like(f"{_STATUS_RED_NAME_REF_PREFIX}%"))
        if hasattr(DocketItem, "is_deleted"):
            q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        items = q.limit(200).all()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.query_status_red_dockets",
            log_key="office_action_auto_close_service.query_status_red_dockets",
            log_window_seconds=300,
        )
        return 0

    closed = 0
    for di in items:
        label = _status_red_label_for_docket(di)
        if not label:
            continue
        due_token = _effective_due_token(di)
        done = done_by_label_due.get((label, due_token)) if due_token else None
        if not done:
            label_done_dates = done_by_label.get(label) or set()
            if len(label_done_dates) == 1:
                done = next(iter(label_done_dates))
        if not done:
            continue
        di.done_date = done
        db.session.add(di)
        _sync_docket_item_state(di)
        closed += 1

    return closed


def auto_close_handled_office_actions(
    *,
    matter_id: str | None = None,
    limit_matters: int = 200,
    commit: bool = False,
) -> dict:
    """
    Close OfficeAction rows (and linked DocketItem deadlines) when response signals indicate
    the notice has been handled.

    This is the DB-level reconciliation that prevents divergence across:
    - Matter auto status (computed)
    - Productivity todos / reminders
    - Notifications / calendar sync

    The ``commit`` argument is kept for call-site compatibility during the
    transaction-boundary migration. This service no longer commits; callers that
    own a route/usecase/worker boundary should commit after this function returns.
    """
    _ = commit
    matter_ids: list[str] = []
    if matter_id:
        matter_ids = [str(matter_id).strip()]
    else:
        try:
            limit = int(limit_matters or 0)
        except Exception:
            limit = 200
        limit = max(1, min(2000, limit))
        try:
            matter_ids = [
                str(r[0])
                for r in db.session.execute(
                    text(
                        """
                        SELECT DISTINCT matter_id
                        FROM office_action
                        WHERE (done_date IS NULL OR TRIM(done_date) = '')
                          AND (
                            (due_date IS NOT NULL AND TRIM(due_date) <> '')
                            OR (extended_due_date IS NOT NULL AND TRIM(extended_due_date) <> '')
                          )
                        ORDER BY matter_id
                        LIMIT :limit
                        """
                    ).execution_options(policy_bypass=True),
                    {"limit": limit},
                ).all()
                if r and r[0]
            ]
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="office_action_auto_close_service.select_matter_ids",
                log_key="office_action_auto_close_service.select_matter_ids",
                log_window_seconds=300,
            )
            matter_ids = []

    scanned = 0
    closed_matters = 0
    closed_office_actions = 0
    closed_dockets = 0
    updated_matters = 0

    for mid in [m for m in matter_ids if (m or "").strip()]:
        scanned += 1

        done_by_oa = get_handled_open_office_action_done_dates(mid)
        for oa_id, done in _get_superseded_non_response_office_action_done_dates(mid).items():
            prev = done_by_oa.get(oa_id)
            if not prev or done < prev:
                done_by_oa[oa_id] = done
        if not done_by_oa:
            continue

        changed = False
        for oa_id, done in done_by_oa.items():
            if not (oa_id or "").strip() or not (done or "").strip():
                continue
            try:
                res = db.session.execute(
                    text(
                        """
                        UPDATE office_action
                        SET done_date = :done
                        WHERE oa_id = :oid
                          AND (done_date IS NULL OR TRIM(done_date) = '')
                        """
                    ).execution_options(policy_bypass=True),
                    {"done": done, "oid": oa_id},
                )
                if getattr(res, "rowcount", 0):
                    closed_office_actions += int(res.rowcount or 0)
                    changed = True
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="office_action_auto_close_service.update_office_action",
                    log_key="office_action_auto_close_service.update_office_action",
                    log_window_seconds=300,
                )

        # Close linked docket items so todo/notifications/calendar are consistent.
        try:
            q = db.session.query(DocketItem).filter(DocketItem.matter_id == str(mid))
            q = q.filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
            q = q.filter(
                or_(
                    and_(
                        DocketItem.extended_due_date.isnot(None),
                        func.trim(DocketItem.extended_due_date) != "",
                    ),
                    and_(DocketItem.due_date.isnot(None), func.trim(DocketItem.due_date) != ""),
                )
            )
            if hasattr(DocketItem, "is_deleted"):
                q = q.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
            items = q.limit(500).all()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="office_action_auto_close_service.query_docket_items",
                log_key="office_action_auto_close_service.query_docket_items",
                log_window_seconds=300,
            )
            items = []

        for di in items:
            oa_id = _extract_office_action_id_from_docket(di)
            if not oa_id:
                continue
            done = done_by_oa.get(oa_id)
            if not done:
                continue
            if (getattr(di, "done_date", None) or "").strip():
                continue
            setattr(di, "done_date", done)
            db.session.add(di)
            closed_dockets += 1
            changed = True
            _sync_docket_item_state(di)

        extra_closed = _close_matching_status_red_dockets(matter_id=str(mid), done_by_oa=done_by_oa)
        if extra_closed:
            closed_dockets += extra_closed
            changed = True

        if changed:
            closed_matters += 1
            # Also keep persisted Matter.status_* in sync, so list views don't drift.
            try:
                matter = Matter.query.get(str(mid))
            except Exception:
                matter = None
            if matter is not None:
                try:
                    result = apply_auto_status_cache_to_matter(
                        matter=matter,
                        memo=(getattr(matter, "memo", None) or "").strip(),
                    )
                    if result.changed:
                        db.session.add(matter)
                        updated_matters += 1
                        try:
                            from app.services.deadlines.mgmt_deadlines import (
                                ensure_mgmt_deadlines_for_matter,
                            )

                            ensure_mgmt_deadlines_for_matter(str(mid), commit=False)
                        except Exception as exc:
                            report_swallowed_exception(
                                exc,
                                context="office_action_auto_close_service.ensure_mgmt_deadlines",
                                log_key="office_action_auto_close_service.ensure_mgmt_deadlines",
                                log_window_seconds=300,
                            )
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="office_action_auto_close_service.update_matter_status",
                        log_key="office_action_auto_close_service.update_matter_status",
                        log_window_seconds=300,
                    )

    try:
        db.session.flush()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.flush",
            log_key="office_action_auto_close_service.flush",
            log_window_seconds=300,
        )
        db.session.rollback()

    try:
        current_app.logger.info(
            "OA auto-close: scanned=%s matters_closed=%s oa_closed=%s dockets_closed=%s",
            scanned,
            closed_matters,
            closed_office_actions,
            closed_dockets,
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="office_action_auto_close_service.log_summary",
            log_key="office_action_auto_close_service.log_summary",
            log_window_seconds=300,
        )

    return {
        "scanned_matters": scanned,
        "closed_matters": closed_matters,
        "closed_office_actions": closed_office_actions,
        "closed_docket_items": closed_dockets,
        "updated_matters": updated_matters,
    }
