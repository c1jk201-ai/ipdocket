from __future__ import annotations

import json

from flask import current_app

from app.models.case_audit_log import CaseAuditLog


def build_audit_section(ctx: dict) -> dict:
    mid_str = ctx["_mid_str"]
    try:
        rows = (
            CaseAuditLog.query.filter_by(case_id=mid_str)
            .order_by(CaseAuditLog.created_at.desc())
            .limit(5)
            .all()
        )
        user_map = {
            u.id: ((getattr(u, "display_name", None) or "").strip() or u.username or f"User {u.id}")
            for u in (ctx.get("users", []) or [])
            if getattr(u, "id", None) is not None
        }

        audit_rows = []
        for row in rows:
            user_name = (
                (user_map.get(row.actor_user_id) or f"User {row.actor_user_id}")
                if row.actor_user_id
                else (row.action or "SYSTEM")
            )
            details = []
            if row.old_value:
                details.append(f"[Old] {json.dumps(row.old_value, ensure_ascii=False)}")
            if row.new_value:
                details.append(f"[New] {json.dumps(row.new_value, ensure_ascii=False)}")
            audit_rows.append(
                {
                    "created_at": row.created_at,
                    "user_name": user_name,
                    "field_name": row.field_name,
                    "details_display": "\n".join(details),
                }
            )

        last_audit = rows[0].created_at if rows else None
        return {
            "case_audit_rows": audit_rows,
            "last_audit_display": last_audit.strftime("%Y-%m-%d %H:%M") if last_audit else None,
        }
    except Exception as exc:
        current_app.logger.warning("Failed to load audit logs: %s", exc)
        return {
            "case_audit_rows": [],
            "last_audit_display": None,
        }
