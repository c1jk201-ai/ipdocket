from __future__ import annotations

from datetime import date

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.ip_records import DocketItem, FileAsset, MatterMemo, MatterMemoFileAsset
from app.utils.annuity_deadline_routing import calendar_endpoint_for_docket
from app.utils.docket_dates import effective_due_for_work, effective_due_text_expr
from app.utils.docket_visibility import is_visible_by_date


def build_deadlines_panel_context(ctx: dict) -> dict:
    """Build only the data needed by the case deadlines HTMX partial."""
    mid_str = ctx["_mid_str"]

    effective_due = effective_due_text_expr(
        DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
    )
    docket_q = DocketItem.query.filter_by(matter_id=mid_str)
    if hasattr(DocketItem, "is_deleted"):
        docket_q = docket_q.filter(
            or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
        )
    docket_items = docket_q.order_by(effective_due.asc(), DocketItem.docket_id.asc()).all()
    docket_open = [d for d in docket_items if not (d.done_date or "").strip()]
    docket_done = [d for d in docket_items if (d.done_date or "").strip()]
    docket_due = [d for d in docket_open if is_visible_by_date(d)]
    docket_scheduled = [d for d in docket_open if not is_visible_by_date(d)]

    next_docket = None
    next_docket_due = None
    for d in docket_due:
        eff_due = effective_due_for_work(
            getattr(d, "due_date", None),
            getattr(d, "extended_due_date", None),
        )
        if not eff_due:
            continue
        eff_due_str = eff_due.isoformat()
        if next_docket_due is None or eff_due_str < next_docket_due:
            next_docket_due = eff_due_str
            next_docket = d

    for di in docket_items:
        di.calendar_month_endpoint = calendar_endpoint_for_docket(
            name_ref=getattr(di, "name_ref", None),
            title=getattr(di, "name_free", None),
        )

    return {
        "docket_items": docket_items,
        "docket_open": docket_open,
        "docket_done": docket_done,
        "docket_due": docket_due,
        "docket_scheduled": docket_scheduled,
        "next_docket": next_docket,
        "docket_history": docket_items,
        "notice_send_semi_close_prompt": None,
        "today_iso": date.today().isoformat(),
    }


def build_memo_panel_context(ctx: dict) -> dict:
    """Build only the data needed by the case memo HTMX partial."""
    mid_str = ctx["_mid_str"]
    memos = (
        MatterMemo.query.filter_by(matter_id=mid_str)
        .options(joinedload(MatterMemo.created_by))
        .order_by(MatterMemo.created_at.desc(), MatterMemo.id.desc())
        .all()
    )

    memo_attachments = {}
    memo_ids = [m.id for m in memos if m.id]
    if memo_ids:
        try:
            rows = (
                db.session.query(MatterMemoFileAsset, FileAsset)
                .join(FileAsset, FileAsset.file_asset_id == MatterMemoFileAsset.file_asset_id)
                .filter(MatterMemoFileAsset.memo_id.in_(memo_ids))
                .order_by(
                    MatterMemoFileAsset.created_at.asc(),
                    MatterMemoFileAsset.memo_file_id.asc(),
                )
                .all()
            )
            for mmfa, fa in rows:
                memo_attachments.setdefault(mmfa.memo_id, []).append(
                    {
                        "memo_file_id": mmfa.memo_file_id,
                        "file_asset_id": fa.file_asset_id,
                        "original_name": fa.original_name,
                        "byte_size": fa.byte_size,
                        "mime_type": fa.mime_type,
                        "created_at": mmfa.created_at,
                    }
                )
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                _ = None
            current_app.logger.error("Error loading memo attachments: %s", exc)
            memo_attachments = {}

    for memo in memos:
        memo.attachments = memo_attachments.get(memo.id, [])

    return {"memos": memos}
