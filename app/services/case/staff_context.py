from __future__ import annotations

from flask_login import current_user
from sqlalchemy import or_

from app.extensions import db
from app.models.party import PartyStaff
from app.models.user import User
from app.services.core.staff_options import build_staff_assignment_lists


def build_staff_picker_context() -> dict:
    domain = ""
    local_users = []
    management_local_users = []
    attorney_local_users = []
    processing_local_users = []
    try:
        q = (
            db.session.query(User)
            .filter(User.is_active.is_(True))
            .filter(User.staff_party_id.isnot(None))
            .filter(User.staff_party_id != "")
            .join(PartyStaff, PartyStaff.party_id == User.staff_party_id)
            .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
        )
        rows = q.distinct().order_by(User.department.asc(), User.username.asc()).all()
        for user in rows:
            display_name = (user.display_name or "").strip()
            username = (user.username or "").strip()
            email = (user.email or "").strip()
            value = display_name or username or email
            if display_name and username:
                label = f"{display_name}({username})"
            elif display_name:
                label = display_name
            else:
                label = username or email or f"User#{user.id}"
            local_users.append(
                {
                    "id": user.id,
                    "staff_party_id": (
                        str(user.staff_party_id).strip()
                        if user.staff_party_id is not None
                        else None
                    ),
                    "value": value,
                    "label": label,
                    "dept": (user.department or "").strip() or None,
                    "email": email.lower() if email else "",
                }
            )
        try:
            lists = build_staff_assignment_lists()
            management_ids = {
                int(user.id)
                for user in (lists.get("management_users") or [])
                if getattr(user, "id", None)
            }
            attorney_ids = {
                int(user.id)
                for user in (
                    (lists.get("attorney_users") or []) or (lists.get("professional_users") or [])
                )
                if getattr(user, "id", None)
            }
            processing_ids = {
                int(user.id)
                for user in (
                    (lists.get("processing_users") or []) or (lists.get("all_users") or [])
                )
                if getattr(user, "id", None)
            }
            management_local_users = [
                user for user in local_users if int(user.get("id") or 0) in management_ids
            ]
            attorney_local_users = [
                user for user in local_users if int(user.get("id") or 0) in attorney_ids
            ]
            processing_local_users = [
                user for user in local_users if int(user.get("id") or 0) in processing_ids
            ]
        except Exception:
            management_local_users = list(local_users)
            attorney_local_users = list(local_users)
            processing_local_users = list(local_users)
    except Exception:
        local_users = []
        management_local_users = []
        attorney_local_users = []
        processing_local_users = []

    return {
        "domain": domain or "",
        "local_users": local_users,
        "management_local_users": management_local_users or local_users,
        "attorney_local_users": attorney_local_users or local_users,
        "processing_local_users": processing_local_users or local_users,
        "groups": [],
        "org_units": [],
        "has_any": bool(local_users),
    }


def build_staff_assignment_context() -> dict:
    def _to_opt(user: User) -> dict:
        email = (user.email or "").strip()
        username = (user.username or "").strip()
        display_name = (user.display_name or "").strip()
        value = display_name or username or email
        if display_name and username:
            label = f"{display_name}({username})"
        elif display_name:
            label = display_name
        else:
            label = username or email or f"User#{user.id}"
        return {
            "id": user.id,
            "staff_party_id": (
                str(user.staff_party_id).strip() if user.staff_party_id is not None else None
            ),
            "value": value,
            "label": label,
            "email": email.lower() if email else "",
            "dept": (user.department or "").strip(),
        }

    try:
        lists = build_staff_assignment_lists()
        all_users = [_to_opt(user) for user in (lists.get("all_users") or [])]
        management_users = [_to_opt(user) for user in (lists.get("management_users") or [])]
        professional_users = [_to_opt(user) for user in (lists.get("professional_users") or [])]
        attorney_users = [_to_opt(user) for user in (lists.get("attorney_users") or [])]
        processing_users = [_to_opt(user) for user in (lists.get("processing_users") or [])]
    except Exception:
        all_users = []
        management_users = []
        professional_users = []
        attorney_users = []
        processing_users = []

    return {
        "all_users": all_users,
        "management_users": management_users,
        "professional_users": professional_users,
        "attorney_users": attorney_users or professional_users,
        "processing_users": processing_users or all_users,
        "has_any": bool(all_users),
    }
