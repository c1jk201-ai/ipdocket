from __future__ import annotations

from datetime import date, datetime

from flask import current_app
from sqlalchemy import func

from app import db
from app.models.user import User
from app.models.user_access_log import UserAccessLog


def get_usage_logs_metrics(
    *,
    page: int,
    per_page: int,
    user_filter: str,
    method_filter: str,
    status_filter: str,
    path_filter: str,
    date_from_raw: str,
    date_to_raw: str,
) -> dict:
    page = max(page, 1)

    user_filter = (user_filter or "").strip()
    method_filter = (method_filter or "").strip().upper()
    status_filter = (status_filter or "").strip().lower()
    path_filter = (path_filter or "").strip()
    date_from_raw = (date_from_raw or "").strip()
    date_to_raw = (date_to_raw or "").strip()

    q = (
        db.session.query(UserAccessLog, User)
        .outerjoin(User, User.id == UserAccessLog.user_id)
        .order_by(UserAccessLog.created_at.desc(), UserAccessLog.id.desc())
    )

    if user_filter.isdigit():
        q = q.filter(UserAccessLog.user_id == int(user_filter))

    allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
    if method_filter and method_filter in allowed_methods:
        q = q.filter(UserAccessLog.method == method_filter)

    if status_filter in {"2xx", "3xx", "4xx", "5xx"}:
        base = int(status_filter[0]) * 100
        q = q.filter(UserAccessLog.status_code >= base, UserAccessLog.status_code < base + 100)

    if path_filter:
        term = f"%{path_filter}%"
        q = q.filter(UserAccessLog.path.ilike(term))

    date_from_val = None
    date_to_val = None
    if date_from_raw:
        try:
            d = date.fromisoformat(date_from_raw)
            date_from_val = datetime.combine(d, datetime.min.time())
            q = q.filter(UserAccessLog.created_at >= date_from_val)
        except Exception:
            date_from_val = None
    if date_to_raw:
        try:
            d = date.fromisoformat(date_to_raw)
            date_to_val = datetime.combine(d, datetime.max.time())
            q = q.filter(UserAccessLog.created_at <= date_to_val)
        except Exception:
            date_to_val = None

    total_count = q.order_by(None).count()
    total_pages = (total_count + per_page - 1) // per_page
    if total_pages < 1:
        total_pages = 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    rows = q.offset(offset).limit(per_page).all()

    logs = []
    for row, user in rows:
        logs.append(
            {
                "id": row.id,
                "created_at": row.created_at,
                "user_id": row.user_id,
                "username": getattr(user, "username", None) if user else None,
                "method": row.method,
                "path": row.path,
                "endpoint": row.endpoint,
                "blueprint": row.blueprint,
                "status_code": row.status_code,
                "duration_ms": row.duration_ms,
                "remote_addr": row.remote_addr,
                "user_agent": row.user_agent,
                "referer": row.referer,
                "request_id": row.request_id,
            }
        )

    users = [{"id": u.id, "username": u.username} for u in User.query.order_by(User.username).all()]

    return {
        "logs": logs,
        "users": users,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,
        "user_filter": user_filter,
        "method_filter": method_filter,
        "status_filter": status_filter,
        "path_filter": path_filter,
        "date_from": date_from_raw,
        "date_to": date_to_raw,
    }
