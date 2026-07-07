from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, case, func, or_

from app import db
from app.models.error_report import ErrorReport


def _normalize_source(value: str | None) -> str:
    source = (value or "all").strip().lower()
    if source in {"all", "swallowed", "logged", "request", "system"}:
        return source
    return "all"


def _source_filters():
    endpoint_text = func.coalesce(ErrorReport.endpoint, "")
    message_text = func.coalesce(ErrorReport.message, "")
    swallowed_expr = or_(
        endpoint_text.ilike("swallowed:%"),
        message_text.ilike("swallowed:%"),
    )
    logged_expr = or_(
        endpoint_text.ilike("logged:%"),
        message_text.ilike("logged:%"),
    )
    request_expr = and_(
        or_(ErrorReport.method.is_(None), ErrorReport.method != "SYSTEM"),
        ~swallowed_expr,
        ~logged_expr,
    )
    system_expr = and_(
        ErrorReport.method == "SYSTEM",
        ~swallowed_expr,
        ~logged_expr,
    )
    return swallowed_expr, logged_expr, request_expr, system_expr


def _apply_source_filter(
    query, swallowed_expr, logged_expr, request_expr, system_expr, *, source: str
):
    if source == "swallowed":
        return query.filter(swallowed_expr)
    if source == "logged":
        return query.filter(logged_expr)
    if source == "request":
        return query.filter(request_expr)
    if source == "system":
        return query.filter(system_expr)
    return query


def _apply_common_filters(
    query,
    swallowed_expr,
    logged_expr,
    request_expr,
    system_expr,
    source_filter: str,
    type_filter: str,
    status_filter: int | None,
    text_filter: str,
):
    query = _apply_source_filter(
        query, swallowed_expr, logged_expr, request_expr, system_expr, source=source_filter
    )
    if type_filter:
        query = query.filter(ErrorReport.error_type.ilike(f"%{type_filter}%"))
    if status_filter is not None:
        query = query.filter(ErrorReport.status_code == int(status_filter))
    if text_filter:
        token = f"%{text_filter}%"
        query = query.filter(
            or_(
                ErrorReport.message.ilike(token),
                ErrorReport.endpoint.ilike(token),
                ErrorReport.path.ilike(token),
                ErrorReport.error_type.ilike(token),
            )
        )
    return query


def _infer_source(endpoint: str | None, message: str | None, method: str | None) -> str:
    endpoint_text = (endpoint or "").strip().lower()
    message_text = (message or "").strip().lower()
    if endpoint_text.startswith("swallowed:") or message_text.startswith("swallowed:"):
        return "swallowed"
    if endpoint_text.startswith("logged:") or message_text.startswith("logged:"):
        return "logged"
    if (method or "").strip().upper() == "SYSTEM":
        return "system"
    return "request"


def get_error_report_metrics(
    *,
    window_minutes: int,
    summary_limit: int,
    recent_limit: int,
    source_filter_raw: str | None,
    type_filter: str,
    text_filter: str,
    status_filter: int | None,
) -> dict:
    window_minutes = max(1, int(window_minutes or 60))
    summary_limit = max(1, int(summary_limit or 20))
    recent_limit = max(1, int(recent_limit or 50))

    source_filter = _normalize_source(source_filter_raw)

    if status_filter is not None and not (100 <= int(status_filter) <= 599):
        status_filter = None

    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    swallowed_expr, logged_expr, request_expr, system_expr = _source_filters()

    window_base = ErrorReport.query.filter(ErrorReport.created_at >= cutoff)
    window_total_count = window_base.count()
    window_count = _apply_common_filters(
        window_base,
        swallowed_expr,
        logged_expr,
        request_expr,
        system_expr,
        source_filter,
        type_filter,
        status_filter,
        text_filter,
    ).count()

    source_counts = {
        "all": int(window_total_count or 0),
        "swallowed": int(window_base.filter(swallowed_expr).count() or 0),
        "logged": int(window_base.filter(logged_expr).count() or 0),
        "request": int(window_base.filter(request_expr).count() or 0),
        "system": int(window_base.filter(system_expr).count() or 0),
    }

    message_prefix = func.substr(ErrorReport.message, 1, 220).label("message_prefix")
    summary_base = db.session.query(
        ErrorReport.error_type.label("error_type"),
        ErrorReport.endpoint.label("endpoint"),
        message_prefix,
        func.count().label("count"),
        func.max(ErrorReport.created_at).label("last_seen"),
    ).filter(ErrorReport.created_at >= cutoff)

    summary_rows = (
        _apply_common_filters(
            summary_base,
            swallowed_expr,
            logged_expr,
            request_expr,
            system_expr,
            source_filter,
            type_filter,
            status_filter,
            text_filter,
        )
        .group_by(ErrorReport.error_type, ErrorReport.endpoint, message_prefix)
        .order_by(func.count().desc(), func.max(ErrorReport.created_at).desc())
        .limit(summary_limit)
        .all()
    )
    summary = [
        (row, _infer_source(row.endpoint, row.message_prefix, None)) for row in (summary_rows or [])
    ]

    recent_base = ErrorReport.query
    recent_reports = (
        _apply_common_filters(
            recent_base,
            swallowed_expr,
            logged_expr,
            request_expr,
            system_expr,
            source_filter,
            type_filter,
            status_filter,
            text_filter,
        )
        .order_by(ErrorReport.created_at.desc())
        .limit(recent_limit)
        .all()
    )
    recent = [
        (report, _infer_source(report.endpoint, report.message, report.method))
        for report in (recent_reports or [])
    ]

    return {
        "summary": summary,
        "recent": recent,
        "window_minutes": window_minutes,
        "summary_limit": summary_limit,
        "recent_limit": recent_limit,
        "window_count": window_count,
        "window_total_count": window_total_count,
        "source_filter": source_filter,
        "type_filter": type_filter,
        "status_filter": status_filter,
        "text_filter": text_filter,
        "source_counts": source_counts,
    }
