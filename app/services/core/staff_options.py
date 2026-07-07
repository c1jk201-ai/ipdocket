from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable

from flask import current_app
from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.party import Party, PartyStaff
from app.models.user import User
from app.services.core.config_service import ConfigService

_STAFF_EMAILS_CACHE: dict = {"data": None, "expires_at": None}


def clear_staff_assignment_cache() -> None:
    _STAFF_EMAILS_CACHE["data"] = None
    _STAFF_EMAILS_CACHE["expires_at"] = None


def _parse_csv(value: str | None) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _resolve_domain() -> str | None:
    raw = (
        ConfigService.get_str(
            "STAFF_EMAIL_DOMAINS",
            "",
            strip=True,
            allow_blank=True,
            prefer_env=True,
        )
        or ConfigService.get_str(
            "INTERNAL_EMAIL_DOMAINS",
            "",
            strip=True,
            allow_blank=True,
            prefer_env=True,
        )
        or ""
    ).strip()
    if raw:
        parts = [d.strip().lower() for d in raw.split(",") if d.strip()]
        if parts:
            return parts[0]
    return None


def _normalize_text(value: str | None) -> str:
    return (value or "").strip()


def _active_staff_party_lookup_query():
    return (
        db.session.query(Party.party_id, Party.name_display, PartyStaff.staff_code)
        .join(PartyStaff, PartyStaff.party_id == Party.party_id)
        .filter(Party.party_kind == "staff")
        .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
    )


def build_staff_owner_option(user: User) -> dict:
    """
    Build a single-select option for staff-owner style pickers.

    These pickers submit a human-readable string, not users.id, so keep the
    value resolvable via `resolve_staff_party_id`.
    """

    email = _normalize_text(getattr(user, "email", None))
    username = _normalize_text(getattr(user, "username", None))
    display_name = _normalize_text(getattr(user, "display_name", None))

    if display_name and username:
        value = f"{display_name}[{username}]"
    else:
        value = display_name or username or email or f"User#{getattr(user, 'id', '')}"

    return {
        "id": getattr(user, "id", None),
        "staff_party_id": _normalize_text(getattr(user, "staff_party_id", None)) or None,
        "staff_code": username,
        "value": value,
        "label": value,
        "email": email.lower() if email else "",
        "dept": _normalize_text(getattr(user, "department", None)),
    }


def build_staff_owner_options(*, category: str = "all") -> list[dict]:
    """
    Build deduplicated options for staff-owner single-select/search inputs.

    Categories map to the assignment lists already used elsewhere in the app.
    """

    try:
        lists = build_staff_assignment_lists()
    except Exception:
        return []

    category_map = {
        "all": lists.get("all_users") or [],
        "management": lists.get("management_users") or [],
        "professional": (lists.get("attorney_users") or [])
        or (lists.get("professional_users") or []),
        "processing": (lists.get("processing_users") or []) or (lists.get("all_users") or []),
    }
    users = category_map.get((category or "all").strip().lower(), category_map["all"])

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for user in users or []:
        option = build_staff_owner_option(user)
        value_key = _normalize_text(option.get("value")).lower()
        party_key = _normalize_text(option.get("staff_party_id"))
        dedupe_key = (party_key, value_key)
        if not value_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(option)
    return out


def default_staff_owner_value(user: User | None) -> str:
    if user is None:
        return ""
    return _normalize_text(build_staff_owner_option(user).get("value"))


def _resolve_staff_party_id_for_user(user: User | None) -> str | None:
    if user is None:
        return None

    staff_party_id = _normalize_text(getattr(user, "staff_party_id", None))
    if not staff_party_id:
        return None

    active_party_id = (
        _active_staff_party_lookup_query()
        .filter(Party.party_id == staff_party_id)
        .with_entities(Party.party_id)
        .first()
    )
    if active_party_id:
        return _normalize_text(active_party_id[0]) or None
    return None


def resolve_staff_party_id(raw: str | None) -> str | None:
    """
    Resolve a text picker value to `party.party_id` for active staff.

    Accepted inputs include:
    - party_id
    - staff code
    - `[hgildong]`
    - `(hgildong)`
    - `hgildong@company.com`
    - plain display names / usernames / emails
    """

    original = _normalize_text(raw)
    if not original:
        return None

    direct_party_id = (
        db.session.query(Party.party_id)
        .filter(Party.party_id == original, Party.party_kind == "staff")
        .first()
    )
    if direct_party_id:
        return _normalize_text(direct_party_id[0]) or None

    lowered = original.lower()
    staff_code = ""

    bracket_match = re.search(r"\[([A-Za-z0-9._-]+)\]", original)
    if bracket_match:
        staff_code = _normalize_text(bracket_match.group(1))

    email_match = re.search(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)", original)
    if email_match:
        staff_code = staff_code or _normalize_text(email_match.group(1))

    paren_match = re.search(r"\(([A-Za-z][A-Za-z0-9._-]{1,50})\)", original)
    if paren_match:
        staff_code = staff_code or _normalize_text(paren_match.group(1))

    if not staff_code and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{1,50}", original):
        staff_code = original

    if staff_code:
        staff_by_code = (
            _active_staff_party_lookup_query()
            .filter(func.lower(func.trim(PartyStaff.staff_code)) == staff_code.lower())
            .with_entities(Party.party_id)
            .first()
        )
        if staff_by_code:
            return _normalize_text(staff_by_code[0]) or None

        user_by_code = (
            db.session.query(User)
            .filter(User.is_active.is_(True))
            .filter(func.lower(func.trim(User.username)) == staff_code.lower())
            .first()
        )
        resolved = _resolve_staff_party_id_for_user(user_by_code)
        if resolved:
            return resolved

    user_by_raw = (
        db.session.query(User)
        .filter(User.is_active.is_(True))
        .filter(
            or_(
                func.lower(func.trim(User.username)) == lowered,
                func.lower(func.trim(User.email)) == lowered,
                func.lower(func.trim(User.display_name)) == lowered,
            )
        )
        .first()
    )
    resolved = _resolve_staff_party_id_for_user(user_by_raw)
    if resolved:
        return resolved

    display_name = re.sub(r"\s*\[[^\]]+\]\s*", " ", original)
    display_name = re.sub(r"\s*\([^)]+\)\s*", " ", display_name)
    display_name = _normalize_text(display_name)
    if not display_name:
        return None

    staff_by_name = (
        _active_staff_party_lookup_query()
        .filter(func.lower(func.trim(Party.name_display)) == display_name.lower())
        .with_entities(Party.party_id)
        .first()
    )
    if staff_by_name:
        return _normalize_text(staff_by_name[0]) or None

    user_by_display = (
        db.session.query(User)
        .filter(User.is_active.is_(True))
        .filter(func.lower(func.trim(User.display_name)) == display_name.lower())
        .first()
    )
    return _resolve_staff_party_id_for_user(user_by_display)


def _fetch_active_users(*, domain: str | None, include_unlinked: bool) -> list[User]:
    """
    Fetch active users suitable for staff assignment pickers.

    Rules:
    - Always exclude inactive app accounts (User.is_active != True)
    - If a user is linked to the staff directory (User.staff_party_id):
        - Exclude when the directory record is missing (deleted)
        - Exclude when PartyStaff.active == 0 (deactivated)
        - Allow when PartyStaff.active == 1 or NULL (legacy data)
    - Optionally include unlinked users (User.staff_party_id is NULL/blank) as a fallback.
    """
    q = db.session.query(User).filter(User.is_active.is_(True))

    # Exclude deactivated/deleted staff directory records.
    # Use OUTER JOIN so we can optionally keep unlinked users.
    q = q.outerjoin(PartyStaff, PartyStaff.party_id == User.staff_party_id)
    staff_dir_ok = and_(
        PartyStaff.party_id.isnot(None),
        or_(PartyStaff.active == 1, PartyStaff.active.is_(None)),
    )
    if include_unlinked:
        q = q.filter(
            or_(
                User.staff_party_id.is_(None),
                User.staff_party_id == "",
                staff_dir_ok,
            )
        )
    else:
        q = q.filter(staff_dir_ok)

    if domain:
        q = q.filter(
            or_(
                User.email.ilike(f"%@{domain}"),
                User.email.is_(None),
                User.email == "",
                ~User.email.contains("@"),
            )
        )

    return q.distinct().order_by(User.department.asc(), User.username.asc()).all()


def _fetch_staff_emails_uncached() -> tuple[dict, bool]:
    """
    (Refactored) Now just returns empty config as Workspace integration is removed.
    We return empty sets so the fallback logic (role-based) takes over.
    """
    return {
        "mgmt": set(),
        "prof": set(),
    }, False


def _role_filtered(users: list[User], roles: set[str]) -> list[User]:
    normalized_roles = {
        (str(r or "")).strip().lower() for r in (roles or set()) if str(r or "").strip()
    }
    if not normalized_roles:
        return []
    out: list[User] = []
    for u in users:
        user_roles = set()
        try:
            user_roles.update(
                {str(v).strip().lower() for v in (u.role_names or set()) if str(v).strip()}
            )
        except Exception:
            raw = str(getattr(u, "role", "") or "")
            for item in raw.split(","):
                name = item.strip().lower()
                if name:
                    user_roles.add(name)
        if user_roles.intersection(normalized_roles):
            out.append(u)
    return out


def _combine_unique_users(*user_lists: Iterable[User]) -> list[User]:
    out: list[User] = []
    seen_user_ids: set[int] = set()
    seen_staff_party_ids: set[str] = set()
    seen_emails: set[str] = set()
    for user_list in user_lists:
        for user in user_list or []:
            uid = getattr(user, "id", None)
            if isinstance(uid, int) and uid > 0:
                if uid in seen_user_ids:
                    continue
                seen_user_ids.add(uid)
                out.append(user)
                continue
            staff_party_id = str(getattr(user, "staff_party_id", "") or "").strip()
            if staff_party_id:
                if staff_party_id in seen_staff_party_ids:
                    continue
                seen_staff_party_ids.add(staff_party_id)
                out.append(user)
                continue
            email = str(getattr(user, "email", "") or "").strip().lower()
            if email:
                if email in seen_emails:
                    continue
                seen_emails.add(email)
                out.append(user)
                continue
            out.append(user)
    return out


def build_staff_assignment_lists() -> dict:
    """
    Returns categorized user lists for staff assignment on case view.
    Fetches fresh User objects bound to the current session using cached email definitions.
    """
    now = datetime.utcnow()
    cache = _STAFF_EMAILS_CACHE

    # 1. Update cache if needed
    if not (cache["data"] and cache["expires_at"] and cache["expires_at"] > now):
        data, external_error = _fetch_staff_emails_uncached()
        ttl_seconds = int(current_app.config.get("STAFF_OPTIONS_CACHE_TTL_SECONDS", 600))

        # If error, reuse old cache if available (graceful degradation)
        if external_error and cache.get("data"):
            cache["expires_at"] = now + timedelta(seconds=ttl_seconds)
            # data remains old data
        else:
            cache["data"] = data
            cache["expires_at"] = now + timedelta(seconds=ttl_seconds)

    emails_map = cache["data"] or {"mgmt": set(), "prof": set()}
    mgmt_emails = emails_map.get("mgmt") or set()
    prof_emails = emails_map.get("prof") or set()

    # 2. Fetch users (cached)
    domain = _resolve_domain()

    def _fetch_with_fallback(domain: str | None) -> list[User]:
        # Prefer directory-linked users (real staff) to avoid showing stale/unlinked accounts.
        users = _fetch_active_users(domain=domain, include_unlinked=False)
        if users:
            return users

        # If domain filtering yields empty, drop domain first (common misconfig footgun).
        if domain:
            users = _fetch_active_users(domain=None, include_unlinked=False)
            if users:
                return users

        # Safety fallback: keep UI usable in fresh/legacy environments without staff linkage.
        users = _fetch_active_users(domain=domain, include_unlinked=True)
        if users:
            return users
        if domain:
            return _fetch_active_users(domain=None, include_unlinked=True)
        return []

    all_users = _fetch_with_fallback(domain)
    by_email = {(u.email or "").strip().lower(): u for u in all_users if (u.email or "").strip()}

    # 3. Categorize users using cached emails
    management_users: list[User]
    attorney_users: list[User]
    processing_users: list[User]

    if mgmt_emails:
        management_users = [by_email[e] for e in sorted(mgmt_emails) if e in by_email]
    else:
        # Fallback to role-based filtering
        configured_mgmt_roles_str = (
            ConfigService.get_str(
                "STAFF_MANAGEMENT_ROLES",
                "admin,mgmt_director,mgmt_staff",
                strip=True,
                allow_blank=False,
            )
            or ""
        ).strip()
        mgmt_roles = {r.strip().lower() for r in configured_mgmt_roles_str.split(",") if r.strip()}
        management_users = _role_filtered(all_users, mgmt_roles)

    if prof_emails:
        professional_seed = [by_email[e] for e in sorted(prof_emails) if e in by_email]
        attorney_users = list(professional_seed)
    else:
        # Admin UI persists the "professional" picker roles under STAFF_PROFESSIONAL_ROLES.
        # Keep STAFF_ATTORNEY_ROLES as a legacy fallback for older environments.
        configured_attorney_roles_str = (
            ConfigService.get_str(
                "STAFF_PROFESSIONAL_ROLES",
                None,
                strip=True,
                allow_blank=False,
            )
            or ConfigService.get_str(
                "STAFF_ATTORNEY_ROLES",
                "lead_attorney,partner_attorney",
                strip=True,
                allow_blank=False,
            )
            or ""
        ).strip()
        attorney_roles = {
            r.strip().lower() for r in configured_attorney_roles_str.split(",") if r.strip()
        }
        attorney_users = _role_filtered(all_users, attorney_roles)

    configured_processing_roles_str = (
        ConfigService.get_str(
            "STAFF_PROCESSING_ROLES",
            "patent_staff",
            strip=True,
            allow_blank=False,
        )
        or ""
    ).strip()
    processing_roles = {
        r.strip().lower() for r in configured_processing_roles_str.split(",") if r.strip()
    }
    processing_users = _role_filtered(all_users, processing_roles)

    # Final fallback
    if not management_users:
        management_users = list(all_users)
    if not attorney_users:
        attorney_users = list(all_users)
    if not processing_users:
        processing_users = list(all_users)

    professional_users = list(attorney_users)
    case_staff_users = _combine_unique_users(attorney_users, processing_users)

    return {
        "domain": domain or "",
        "all_users": all_users,
        "management_users": management_users,
        "professional_users": professional_users,
        "attorney_users": attorney_users,
        "processing_users": processing_users,
        "case_staff_users": case_staff_users,
    }
