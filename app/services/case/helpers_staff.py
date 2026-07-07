from __future__ import annotations

from datetime import datetime
import re
import uuid

from flask import current_app, flash
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.party import Party, PartyStaff
from app.models.ip_records import MatterCustomField, MatterStaffAssignment
from app.models.user import User
from app.utils.error_logging import report_swallowed_exception

__all__ = [
    "_BASIC_CANONICAL_STAFF_KEYS",
    "_overlay_basic_staff_fields",
    "_split_staff_tokens",
    "_normalize_staff_token",
    "_format_staff_value",
    "_resolve_user_from_id",
    "_resolve_user_from_staff_token",
    "_resolve_users_from_staff_fields",
    "_sync_matter_staff_assignments",
    "_update_basic_matter_info",
]

_BASIC_CANONICAL_STAFF_KEYS = {"attorney", "manager", "handler"}


def _overlay_basic_staff_fields(data: dict, basic_data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    if not isinstance(basic_data, dict):
        return data

    for key in _BASIC_CANONICAL_STAFF_KEYS:
        if key in basic_data:
            data[key] = (basic_data.get(key) or "").strip()
    return data


def _split_staff_tokens(raw: str) -> list[str]:
    return [token.strip() for token in re.split(r"[;,]+", raw or "") if token.strip()]


def _normalize_staff_token(raw: str) -> str:
    cleaned = re.sub(r"\s*\[[^\]]+\]\s*", " ", raw or "")
    cleaned = re.sub(r"\s*\([^)]+\)\s*", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def _format_staff_value(user: User) -> str:
    display_name = (user.display_name or "").strip()
    username = (user.username or "").strip()
    email = (user.email or "").strip()
    return display_name or username or email or f"User#{user.id}"


def _staff_code_for_user(user: User) -> str:
    username = (getattr(user, "username", None) or "").strip()
    if username:
        return username
    email = (getattr(user, "email", None) or "").strip()
    if "@" in email:
        return email.split("@", 1)[0].strip()
    return f"user-{getattr(user, 'id', '')}".strip("-")


def _ensure_staff_party_for_user(user: User | None) -> str | None:
    if not user or not getattr(user, "is_active", False):
        return None

    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip()
    if staff_party_id:
        try:
            staff_row = db.session.get(PartyStaff, staff_party_id)
        except SQLAlchemyError:
            staff_row = PartyStaff.query.filter_by(party_id=staff_party_id).first()
        if staff_row and getattr(staff_row, "active", None) in (None, 1):
            party = db.session.get(Party, staff_party_id)
            if not party:
                db.session.add(
                    Party(
                        party_id=staff_party_id,
                        name_display=_format_staff_value(user),
                        party_kind="staff",
                        created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
            elif not (getattr(party, "party_kind", None) or "").strip():
                party.party_kind = "staff"
            return staff_party_id

    staff_code = _staff_code_for_user(user)
    if staff_code:
        staff_row = (
            PartyStaff.query.filter(
                func.lower(func.trim(PartyStaff.staff_code)) == staff_code.lower()
            )
            .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
            .first()
        )
        if staff_row:
            party_id = (staff_row.party_id or "").strip()
            if party_id:
                party = db.session.get(Party, party_id)
                if not party:
                    db.session.add(
                        Party(
                            party_id=party_id,
                            name_display=_format_staff_value(user),
                            party_kind="staff",
                            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                    )
                elif not (getattr(party, "party_kind", None) or "").strip():
                    party.party_kind = "staff"
                user.staff_party_id = party_id
                if not (getattr(user, "department", None) or "").strip() and staff_row.dept:
                    user.department = staff_row.dept
                return party_id

    party_id = uuid.uuid4().hex
    db.session.add(
        Party(
            party_id=party_id,
            name_display=_format_staff_value(user),
            party_kind="staff",
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    db.session.add(
        PartyStaff(
            party_id=party_id,
            staff_code=staff_code or party_id,
            dept=(getattr(user, "department", None) or "").strip() or None,
            active=1,
        )
    )
    user.staff_party_id = party_id
    return party_id


def _is_assignable_staff_user(user: User | None) -> bool:
    if not user or not getattr(user, "is_active", False):
        return False
    party_id = (getattr(user, "staff_party_id", None) or "").strip()
    if not party_id:
        return True
    try:
        staff_row = db.session.get(PartyStaff, party_id)
    except SQLAlchemyError:
        staff_row = PartyStaff.query.filter_by(party_id=party_id).first()
    if not staff_row:
        return False
    try:
        return staff_row.active in (None, 1)
    except Exception:
        return True


def _resolve_user_from_id(raw: str) -> User | None:
    token = (raw or "").strip()
    if not token:
        return None
    try:
        user_id = int(token)
    except (TypeError, ValueError):
        return None
    user = User.query.filter_by(id=user_id, is_active=True).first()
    return user if _is_assignable_staff_user(user) else None


def _resolve_user_from_staff_token(raw: str) -> User | None:
    token = (raw or "").strip()
    if not token:
        return None

    if token.isdigit():
        user = User.query.filter_by(id=int(token), is_active=True).first()
        if _is_assignable_staff_user(user):
            return user

    email_match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)", token)
    if email_match:
        email = email_match.group(1).strip().lower()
        user = User.query.filter(User.email.ilike(email), User.is_active.is_(True)).first()
        if _is_assignable_staff_user(user):
            return user

    name = _normalize_staff_token(token)
    if name:
        user = User.query.filter(User.username.ilike(name), User.is_active.is_(True)).first()
        if _is_assignable_staff_user(user):
            return user
        user = User.query.filter(User.display_name.ilike(name), User.is_active.is_(True)).first()
        if _is_assignable_staff_user(user):
            return user
        if len(name) >= 2:
            like = f"%{name}%"
            candidates = (
                db.session.query(User)
                .outerjoin(PartyStaff, PartyStaff.party_id == User.staff_party_id)
                .filter(User.is_active.is_(True))
                .filter(
                    or_(
                        User.staff_party_id.is_(None),
                        User.staff_party_id == "",
                        and_(
                            PartyStaff.party_id.isnot(None),
                            or_(PartyStaff.active == 1, PartyStaff.active.is_(None)),
                        ),
                    )
                )
                .filter(or_(User.display_name.ilike(like), User.username.ilike(like)))
                .all()
            )
            if candidates:
                normalized = name.lower()
                normalized_matches = [
                    user
                    for user in candidates
                    if _normalize_staff_token(user.display_name or "").lower() == normalized
                    or _normalize_staff_token(user.username or "").lower() == normalized
                ]
                if len(normalized_matches) == 1:
                    return normalized_matches[0]
                if len(candidates) == 1:
                    return candidates[0]

    if "@" in token:
        user = User.query.filter(User.email.ilike(token), User.is_active.is_(True)).first()
        if _is_assignable_staff_user(user):
            return user

    try:
        from app.services.deadlines.mgmt_deadlines import AssigneeResolver

        staff_party_id = AssigneeResolver().resolve(token)
    except Exception:
        staff_party_id = None

    if staff_party_id:
        user = User.query.filter_by(staff_party_id=staff_party_id, is_active=True).first()
        if _is_assignable_staff_user(user):
            return user

    return None


def _resolve_users_from_staff_fields(*values: str) -> list[User]:
    users: list[User] = []
    seen_ids: set[int] = set()
    for raw in values:
        for token in _split_staff_tokens(raw or ""):
            user = _resolve_user_from_staff_token(token)
            if not user or user.id in seen_ids:
                continue
            users.append(user)
            seen_ids.add(user.id)
    return users


def _sync_matter_staff_assignments(
    matter_id: str,
    data: dict,
    *,
    staff_party_ids: dict[str, list[str]] | None = None,
) -> None:
    role_map = {
        "attorney": "attorney",
        "manager": "manager",
        "handler": "handler",
    }
    staff_party_ids = staff_party_ids or {}

    for key, role_code in role_map.items():
        if key not in data and key not in staff_party_ids:
            continue

        raw_value = (data.get(key) or "").strip()
        explicit_ids = [pid for pid in (staff_party_ids.get(key) or []) if pid]

        if explicit_ids:
            MatterStaffAssignment.query.filter_by(
                matter_id=str(matter_id),
                staff_role_code=role_code,
            ).delete()
            rows = [
                MatterStaffAssignment(
                    matter_id=str(matter_id),
                    staff_party_id=str(pid),
                    staff_role_code=role_code,
                    seq=idx + 1,
                    raw_text=raw_value,
                )
                for idx, pid in enumerate(explicit_ids)
            ]
            if rows:
                db.session.add_all(rows)
            continue

        if not raw_value:
            MatterStaffAssignment.query.filter_by(
                matter_id=str(matter_id),
                staff_role_code=role_code,
            ).delete()
            continue

        users = [
            user
            for user in _resolve_users_from_staff_fields(raw_value)
            if _ensure_staff_party_for_user(user)
        ]
        if not users:
            message = f"Contact Matching Failed: {role_code}={raw_value}"
            current_app.logger.warning(message)
            try:
                flash(message, "warning")
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="case.helpers_staff._sync_matter_staff_assignments.flash_warning",
                    log_key="case.helpers_staff._sync_matter_staff_assignments.flash_warning",
                    log_window_seconds=300,
                )
            continue

        MatterStaffAssignment.query.filter_by(
            matter_id=str(matter_id),
            staff_role_code=role_code,
        ).delete()
        rows = [
            MatterStaffAssignment(
                matter_id=str(matter_id),
                staff_party_id=user.staff_party_id,
                staff_role_code=role_code,
                seq=idx + 1,
                raw_text=raw_value,
            )
            for idx, user in enumerate(users)
        ]
        if rows:
            db.session.add_all(rows)


def _update_basic_matter_info(matter_id: str, form_data: dict) -> None:
    row = MatterCustomField.query.filter_by(matter_id=matter_id, namespace="basic").first()
    if not row:
        row = MatterCustomField(matter_id=matter_id, namespace="basic", data={})
        db.session.add(row)

    data = dict(row.data or {})
    updates = 0
    staff_party_ids: dict[str, list[str]] = {}
    sync_data: dict[str, str] = {}
    for key in _BASIC_CANONICAL_STAFF_KEYS:
        id_key = f"{key}_id"
        has_text = key in form_data
        has_id = id_key in form_data
        if not (has_text or has_id):
            continue

        raw_text = (form_data.get(key) or "").strip()
        raw_id = (form_data.get(id_key) or "").strip()
        existing_text = (data.get(key) or "").strip()

        if raw_id:
            user = _resolve_user_from_id(raw_id)
            staff_party_id = _ensure_staff_party_for_user(user)
            if user and staff_party_id:
                new_text = _format_staff_value(user)
                if new_text != existing_text:
                    data[key] = new_text
                    updates += 1
                staff_party_ids[key] = [str(staff_party_id)]
                sync_data[key] = data.get(key) or new_text
                continue
            if user and not user.staff_party_id:
                message = f"Contact ID Matching Failed: {id_key}={raw_id}"
                current_app.logger.warning(message)
                try:
                    flash(message, "warning")
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="case.helpers_staff._update_basic_matter_info.flash_warning",
                        log_key="case.helpers_staff._update_basic_matter_info.flash_warning",
                        log_window_seconds=300,
                    )
                if raw_text and raw_text != existing_text:
                    data[key] = raw_text
                    updates += 1
                    sync_data[key] = raw_text
                continue
            if raw_text:
                if raw_text != existing_text:
                    data[key] = raw_text
                    updates += 1
                    sync_data[key] = raw_text
            else:
                message = f"Contact ID Matching Failed: {id_key}={raw_id}"
                current_app.logger.warning(message)
                try:
                    flash(message, "warning")
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="case.helpers_staff._update_basic_matter_info.flash_warning",
                        log_key="case.helpers_staff._update_basic_matter_info.flash_warning",
                        log_window_seconds=300,
                    )
            continue

        if raw_text != existing_text:
            data[key] = raw_text
            updates += 1
            sync_data[key] = raw_text

    if updates > 0:
        row.data = data

    if sync_data or staff_party_ids:
        _sync_matter_staff_assignments(matter_id, sync_data, staff_party_ids=staff_party_ids)
