from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Iterable, Optional

from flask import abort, current_app, has_app_context, request
from flask_login import current_user
from sqlalchemy import bindparam, func, or_, select

from app.extensions import db
from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

# NOTE:
# - below   "Role + Responsible + " 3   Default .
# -     fit    .


def _user_role(user) -> str:
    return (getattr(user, "role", None) or getattr(user, "user_role", None) or "").lower()


def _normalize_role(role: Optional[str]) -> str:
    return (role or "").strip().lower()


def _parse_role_csv(raw: Optional[str]) -> set[str]:
    value = (raw or "").strip()
    if not value:
        return set()
    out: set[str] = set()
    for part in value.split(","):
        item = _normalize_role(part)
        if item:
            out.add(item)
    return out


def _normalize_role_codes(role_codes: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_normalize_role(r) for r in role_codes if _normalize_role(r)}))


def _is_authenticated(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False))


def _is_authenticated_active(user) -> bool:
    return _is_authenticated(user) and getattr(user, "is_active", True) is not False


def _empty_matter_ids_select():
    from app.models.ip_records import MatterStaffAssignment

    return select(MatterStaffAssignment.matter_id).where(text("1=0"))


def _is_admin_internal(user) -> bool:
    if getattr(user, "is_admin", False):
        return True
    roles = get_user_role_names(user)
    return bool(roles.intersection({"admin", "superadmin", "mgmt_director"}))


def _first_attr(obj_or_cls: Any, names: Iterable[str]):
    for n in names:
        if hasattr(obj_or_cls, n):
            return getattr(obj_or_cls, n)
    return None


def _get_user_org_id(user) -> Optional[int]:
    v = _first_attr(user, ["org_id", "organization_id", "tenant_id", "firm_id"])
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _get_user_id(user) -> Optional[int]:
    v = _first_attr(user, ["id", "user_id"])
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def policy_can_read_object(user, obj) -> bool:
    """
      Access Control(Details/items Search).
    - admin: allow
    - org match: required if both sides present
    - assignee match: required by default for non-admin users.
    """
    if _is_admin_internal(user):
        return True

    user_org = _get_user_org_id(user)
    obj_org = _first_attr(obj, ["org_id", "organization_id", "tenant_id", "firm_id"])
    if user_org is not None and obj_org is not None and int(obj_org) != int(user_org):
        return False

    require_assignee = bool(current_app.config.get("POLICY_DEFAULT_REQUIRE_ASSIGNEE_MATCH", True))
    if not require_assignee:
        return True

    user_id = _get_user_id(user)
    if user_id is None:
        return False

    assignee_val = _first_attr(
        obj, ["assignee_id", "responsible_id", "owner_id", "lead_attorney_id"]
    )
    if assignee_val is None:
        # Responsible    orgto (/ fit  )
        return True
    try:
        return int(assignee_val) == int(user_id)
    except Exception:
        return False


def policy_filter_query(query, model_cls, user):
    """
    /Search Search Apply  Filter.
    SQLAlchemy Query   from org/assignee items Add.
    """
    if _is_admin_internal(user):
        return query

    # org filter
    user_org = _get_user_org_id(user)
    org_col = _first_attr(model_cls, ["org_id", "organization_id", "tenant_id", "firm_id"])
    if user_org is not None and org_col is not None:
        query = query.filter(org_col == user_org)

    # assignee filter (default)
    if current_app.config.get("POLICY_DEFAULT_REQUIRE_ASSIGNEE_MATCH", True):
        user_id = _get_user_id(user)
        assignee_col = _first_attr(
            model_cls, ["assignee_id", "responsible_id", "owner_id", "lead_attorney_id"]
        )
        if user_id is not None and assignee_col is not None:
            query = query.filter(assignee_col == user_id)

    return query


logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_LEAD_ATTORNEY = "lead_attorney"
ROLE_PARTNER_ATTORNEY = "partner_attorney"
ROLE_PATENT_STAFF = "patent_staff"
ROLE_MGMT_DIRECTOR = "mgmt_director"
ROLE_MGMT_STAFF = "mgmt_staff"

try:
    from app.models.user import ROLE_MGMT_ALIASES, ROLE_WORK_ALIASES
except Exception:
    ROLE_MGMT_ALIASES = {ROLE_MGMT_DIRECTOR, ROLE_MGMT_STAFF, "manager", "accounting"}
    ROLE_WORK_ALIASES = {
        ROLE_LEAD_ATTORNEY,
        ROLE_PARTNER_ATTORNEY,
        ROLE_PATENT_STAFF,
        "attorney",
        "patent_engineer",
        "paralegal",
        "staff",
    }

_ROLE_READ_ONLY = {"user"}
# "  "  Required  Role(Task  )
# - : mgmt_director
# - : partner_attorney (lead_attorney )
# - , (/Restore/Backup)   admin-only  
_ROLE_BUSINESS_SUPER = {
    ROLE_ADMIN,
    ROLE_LEAD_ATTORNEY,
    ROLE_PARTNER_ATTORNEY,
    ROLE_MGMT_DIRECTOR,
}
_ROLE_CASE_VIEW_GLOBAL = set(_ROLE_BUSINESS_SUPER)
_ROLE_CASE_GLOBAL = set(_ROLE_BUSINESS_SUPER)
_ROLE_CASE_TEAM = {
    ROLE_PARTNER_ATTORNEY,
    ROLE_PATENT_STAFF,
    ROLE_MGMT_DIRECTOR,
    ROLE_MGMT_STAFF,
    "manager",
    "attorney",
    "handler",
    "staff",
    "draftsman",
    "patent_engineer",
    "paralegal",
    "accounting",
}
_ROLE_CASE_TEAM_LEAD = {ROLE_PARTNER_ATTORNEY}
_ROLE_CASE_DELETE = set(_ROLE_BUSINESS_SUPER)
_ROLE_CASE_ASSIGN = {ROLE_ADMIN, ROLE_LEAD_ATTORNEY, ROLE_PARTNER_ATTORNEY}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_ALWAYS_INVOICE_ROLES = {
    ROLE_MGMT_DIRECTOR,
    ROLE_LEAD_ATTORNEY,
}
_DEFAULT_SELF_ASSIGNED_ROLE_CODES = ("manager", "attorney", "handler", "work")
_APP_IDENTIFIER_TYPES = (
    "Application No.",
    "APP_NO",
    "application_no",
    "app_no",
    "PCT Application No.",
    "PCTApplication No.",
    "pct_application_no",
    "EP Application No.",
    "EPApplication No.",
    "ep_application_no",
)
_PRIORITY_IDENTIFIER_TYPES = ("Priority", "priority_no")
_ORIGIN_IDENTIFIER_TYPES = ("Parent application No.", "parent_application_no")
_REFERENCE_IDENTIFIER_TYPES = (
    *_ORIGIN_IDENTIFIER_TYPES,
    "PCT Application No.",
    "PCTApplication No.",
    "pct_application_no",
    "EP Application No.",
    "EPApplication No.",
    "ep_application_no",
)

_ROLE_PRIORITY = [
    ROLE_ADMIN,
    ROLE_MGMT_DIRECTOR,
    ROLE_LEAD_ATTORNEY,
    ROLE_PARTNER_ATTORNEY,
    ROLE_MGMT_STAFF,
    ROLE_PATENT_STAFF,
    "manager",
    "accounting",
    "staff",
    "user",
]

PERM_CASE_VIEW_ASSIGNED = "case.view.assigned"
PERM_CASE_VIEW_TEAM = "case.view.team"
PERM_CASE_VIEW_ALL = "case.view.all"
PERM_CASE_EDIT_ASSIGNED = "case.edit.assigned"
PERM_CASE_EDIT_TEAM = "case.edit.team"
PERM_CASE_EDIT_ALL = "case.edit.all"
PERM_CASE_ASSIGN_TEAM = "case.assign.team"
PERM_CASE_ASSIGN_ALL = "case.assign.all"
PERM_CASE_DELETE = "case.delete"
PERM_INVOICE_MANAGE = "invoice.manage"

_EXPLICIT_CASE_POLICY_PERMISSION_KEYS = {
    PERM_CASE_VIEW_ASSIGNED,
    PERM_CASE_VIEW_TEAM,
    PERM_CASE_VIEW_ALL,
    PERM_CASE_EDIT_ASSIGNED,
    PERM_CASE_EDIT_TEAM,
    PERM_CASE_EDIT_ALL,
    PERM_CASE_ASSIGN_TEAM,
    PERM_CASE_ASSIGN_ALL,
    PERM_CASE_DELETE,
    PERM_INVOICE_MANAGE,
}


def get_management_roles() -> set[str]:
    """
    Get configured management roles from SystemConfig.
    Returns a set of normalized role strings (lowercase).
    Example: {'manager', 'mgmt_director'}
    """
    raw = (
        ConfigService.get_str(
            "STAFF_MANAGEMENT_ROLES",
            "admin,mgmt_director,mgmt_staff,manager",
            strip=True,
            allow_blank=False,
        )
        or ""
    )

    return _parse_role_csv(raw)


def get_invoice_roles() -> set[str]:
    """
    Get configured invoice roles from SystemConfig.
    Returns a set of normalized role strings (lowercase).
    """
    raw = (
        ConfigService.get_str(
            "STAFF_INVOICE_ROLES",
            "admin,mgmt_director,lead_attorney,mgmt_staff,manager,accounting",
            strip=True,
            allow_blank=False,
        )
        or ""
    )

    roles = _parse_role_csv(raw)
    # Policy baseline: these leadership roles must keep invoice/tax-issue access
    # regardless of system_config customizations.
    roles.update(_ALWAYS_INVOICE_ROLES)
    return roles


def get_self_assigned_role_codes() -> tuple[str, ...]:
    """
    Return role codes treated as self-assigned matter roles.

    Default roles are the three roles shown on matter views:
    - manager
    - attorney
    - handler
    """
    raw = (
        ConfigService.get_str(
            "CASE_SELF_ASSIGNED_ROLE_CODES",
            ",".join(_DEFAULT_SELF_ASSIGNED_ROLE_CODES),
            strip=True,
            allow_blank=False,
        )
        or ""
    )
    parsed = tuple(
        sorted({(part or "").strip().lower() for part in raw.split(",") if (part or "").strip()})
    )
    if parsed:
        return parsed
    return tuple(_DEFAULT_SELF_ASSIGNED_ROLE_CODES)


def is_admin(user=None):
    """Check if user has admin role."""
    u = user or current_user
    if not _is_authenticated(u):
        return False
    if bool(getattr(u, "is_admin", False)):
        return True
    return ROLE_ADMIN in get_user_role_names(u)


def is_manager(user=None):
    """
    Check if user is a manager (includes admin and configured management roles).
    This grants operational permissions (invoices, case deletions) but NOT admin page access.
    """
    u = user or current_user
    if not _is_authenticated(u):
        return False

    role_names = get_user_role_names(u)

    # Admin is always a manager
    if ROLE_ADMIN in role_names:
        return True

    mgmt_roles = get_management_roles()
    return bool(role_names.intersection(mgmt_roles))


def is_invoice_manager(user=None):
    u = user or current_user
    if not _is_authenticated(u):
        return False

    role_names = get_user_role_names(u)
    if ROLE_ADMIN in role_names:
        return True

    invoice_roles = set(get_invoice_roles())
    if "manager" in invoice_roles:
        invoice_roles.update(get_management_roles())
    return bool(role_names.intersection(invoice_roles))


def _split_role_csv(raw: Optional[str]) -> set[str]:
    return _parse_role_csv(raw)


def get_user_role_names(user) -> set[str]:
    if not user:
        return set()
    names = None
    try:
        names = getattr(user, "role_names", None)
    except Exception:
        names = None
    if isinstance(names, (set, frozenset, list, tuple)):
        normalized = {_normalize_role(v) for v in names}
        normalized.discard("")
        return normalized

    out = set()
    roles = getattr(user, "roles", None)
    if not isinstance(roles, (list, tuple, set, frozenset)):
        roles = []
    for role_obj in roles or []:
        name = _normalize_role(getattr(role_obj, "name", None))
        if name:
            out.add(name)
    out.update(_split_role_csv(getattr(user, "role", None) or getattr(user, "user_role", None)))
    return out


def get_primary_role_name(user, default: str = "user") -> str:
    names = get_user_role_names(user)
    return pick_primary_role_name(names, default=default)


def pick_primary_role_name(role_names: Iterable[str], default: str = "user") -> str:
    names = {_normalize_role(v) for v in (role_names or [])}
    names.discard("")
    if not names:
        return default
    for name in _ROLE_PRIORITY:
        if name in names:
            return name
    return sorted(names)[0]


def _roles_intersect(user, role_set: set[str]) -> bool:
    return bool(get_user_role_names(user).intersection(role_set))


def _user_has_permission_key(user, perm_key: str) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    checker = getattr(user, "has_permission", None)
    if callable(checker):
        try:
            return bool(checker(perm_key))
        except Exception:
            return False
    return False


def _user_permission_keys(user) -> set[str]:
    out: set[str] = set()
    if not user:
        return out
    roles = getattr(user, "roles", None)
    if not isinstance(roles, (list, tuple, set, frozenset)):
        roles = []
    for role_obj in roles or []:
        for key in getattr(role_obj, "permissions", None) or []:
            name = (str(key or "")).strip()
            if name:
                out.add(name)
    return out


def _uses_explicit_case_policy(user) -> bool:
    keys = _user_permission_keys(user)
    if not keys:
        return False
    if keys.intersection(_EXPLICIT_CASE_POLICY_PERMISSION_KEYS):
        return True
    return any(key.startswith("case.") for key in keys)


def resolve_role_scope(role: Optional[str]) -> dict[str, bool]:
    """
    Normalize role aliases into visibility flags for mgmt/work scopes.

    Returns:
        show_all_mgmt, show_all_work, show_own_mgmt, show_own_work
    """
    role = _normalize_role(role)
    # Business super roles can view both MGMT/WORK scopes regardless of assignment.
    mgmt_all = set(_ROLE_BUSINESS_SUPER)
    work_all = set(_ROLE_BUSINESS_SUPER)

    mgmt_aliases = {(_normalize_role(r) if r else "") for r in ROLE_MGMT_ALIASES}
    work_aliases = {(_normalize_role(r) if r else "") for r in ROLE_WORK_ALIASES}

    mgmt_own = {ROLE_MGMT_STAFF} | (mgmt_aliases - mgmt_all)
    work_own = {ROLE_PATENT_STAFF} | (work_aliases - work_all)

    show_all_mgmt = role in mgmt_all
    show_all_work = role in work_all
    show_own_mgmt = role in mgmt_own
    show_own_work = role in work_own

    if not any((show_all_mgmt, show_all_work, show_own_mgmt, show_own_work)):
        show_own_mgmt = True
        show_own_work = True

    return {
        "show_all_mgmt": show_all_mgmt,
        "show_all_work": show_all_work,
        "show_own_mgmt": show_own_mgmt,
        "show_own_work": show_own_work,
    }


def _user_staff_party_id(user) -> Optional[str]:
    staff_pid = getattr(user, "staff_party_id", None)
    staff_pid = (staff_pid or "").strip()
    return staff_pid or None


def _user_department(user) -> Optional[str]:
    dept = getattr(user, "department", None)
    dept = (dept or "").strip()
    return dept or None


def policy_accessible_matter_ids_select(user):
    """
    Search(/Search)from  Matter  .

     :
    - Role: (role) All 
    - Responsible: matter_staff_assignment.staff_party_id == user.staff_party_id
    - :  dept  (Role  )

    value is `Model.matter_id.in_(...)`     Select.
    """
    if not _is_authenticated(user):
        return _empty_matter_ids_select()

    # Global roles bypass (admin/table// )
    if can_manage_case_globally(user):
        from app.models.ip_records import Matter

        return select(Matter.matter_id)

    staff_pid = _user_staff_party_id(user)
    department = _user_department(user)
    use_explicit_policy = _uses_explicit_case_policy(user)
    self_assigned_role_codes = get_self_assigned_role_codes()

    from app.models.party import PartyStaff
    from app.models.ip_records import MatterStaffAssignment

    selects = []
    allow_direct = bool(staff_pid) and (
        not use_explicit_policy or _user_has_permission_key(user, PERM_CASE_VIEW_ASSIGNED)
    )
    if allow_direct:
        role_expr = func.lower(func.trim(MatterStaffAssignment.staff_role_code))
        selects.append(
            select(MatterStaffAssignment.matter_id)
            .where(MatterStaffAssignment.staff_party_id == staff_pid)
            .where(role_expr.in_(self_assigned_role_codes))
        )

    allow_team = bool(department) and (
        _user_has_permission_key(user, PERM_CASE_VIEW_TEAM)
        if use_explicit_policy
        else _roles_intersect(user, _ROLE_CASE_TEAM)
    )
    if allow_team:
        selects.append(
            select(MatterStaffAssignment.matter_id)
            .join(PartyStaff, PartyStaff.party_id == MatterStaffAssignment.staff_party_id)
            .where(PartyStaff.dept == department)
        )

    if not selects:
        return _empty_matter_ids_select()

    sel = selects[0]
    for extra in selects[1:]:
        sel = sel.union(extra)
    return sel


# ============================================================
# Managed-Matter Visibility Helpers
# ============================================================


_DEFAULT_MANAGER_ROLE_CODES = ("manager", "mgmt")


def managed_matter_ids_select(user, *, role_codes: Iterable[str] = _DEFAULT_MANAGER_ROLE_CODES):
    """
    Return a Select of matter_ids where the user is assigned as a case manager (Manager).

    This is intentionally narrower than policy_accessible_matter_ids_select():
    - We only include matters where the user has a direct assignment with staff_role_code in role_codes.
    - We do NOT include department/team union, to avoid unintentionally widening "managed work" visibility.
    """
    from app.models.ip_records import MatterStaffAssignment

    if not _is_authenticated(user):
        return _empty_matter_ids_select()

    staff_pid = _user_staff_party_id(user)
    if not staff_pid:
        return _empty_matter_ids_select()

    normalized_roles = _normalize_role_codes(role_codes)
    if not normalized_roles:
        return _empty_matter_ids_select()

    role_expr = func.lower(func.trim(MatterStaffAssignment.staff_role_code))
    return (
        select(MatterStaffAssignment.matter_id)
        .where(
            MatterStaffAssignment.staff_party_id == staff_pid,
            role_expr.in_(normalized_roles),
        )
        .distinct()
    )


def is_manager_assigned_to_matter(
    user, matter_id: str, *, role_codes: Iterable[str] = _DEFAULT_MANAGER_ROLE_CODES
) -> bool:
    """
    True if user is assigned to matter_id with staff_role_code in role_codes.

    Use for per-item checks (detail views) where we cannot cheaply reuse subqueries.
    """
    if not _is_authenticated(user):
        return False

    mid = (matter_id or "").strip()
    if not mid:
        return False

    staff_pid = _user_staff_party_id(user)
    if not staff_pid:
        return False

    normalized_roles = _normalize_role_codes(role_codes)
    if not normalized_roles:
        return False

    try:
        from app.models.ip_records import MatterStaffAssignment

        role_expr = func.lower(func.trim(MatterStaffAssignment.staff_role_code))
        exists_expr = (
            db.session.query(MatterStaffAssignment.msa_id)
            .filter(MatterStaffAssignment.matter_id == mid)
            .filter(MatterStaffAssignment.staff_party_id == staff_pid)
            .filter(role_expr.in_(normalized_roles))
            .exists()
        )
        return bool(db.session.query(exists_expr).scalar())
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="permissions.is_manager_assigned_to_matter",
            log_key="permissions.is_manager_assigned_to_matter",
            log_window_seconds=300,
        )
        return False


def _has_direct_assignment(*, staff_party_id: Optional[str], matter_id: str) -> bool:
    """
    "  Matter" :
    CASE_SELF_ASSIGNED_ROLE_CODES(Default: manager,attorney,handler) Role
    User staff_party_id    True.
    """
    if not staff_party_id:
        return False
    role_codes = get_self_assigned_role_codes()
    if not role_codes:
        return False
    try:
        stmt = (
            text("""
                SELECT 1
                FROM matter_staff_assignment
                WHERE matter_id = :mid
                  AND staff_party_id = :sid
                  AND lower(trim(staff_role_code)) IN :role_codes
                LIMIT 1
                """)
            .bindparams(bindparam("role_codes", expanding=True))
            .execution_options(policy_bypass=True)
        )
        row = db.session.execute(
            stmt,
            {
                "mid": str(matter_id),
                "sid": str(staff_party_id),
                "role_codes": list(role_codes),
            },
        ).scalar()
        return bool(row)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="permissions._has_direct_assignment",
            log_key="permissions._has_direct_assignment",
            log_window_seconds=300,
        )
        return False


def _has_team_assignment(*, department: Optional[str], matter_id: str) -> bool:
    if not department:
        return False
    try:
        row = db.session.execute(
            text("""
                SELECT 1
                FROM matter_staff_assignment msa
                JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                WHERE msa.matter_id = :mid
                  AND ps.dept = :dept
                LIMIT 1
                """).execution_options(policy_bypass=True),
            {"mid": str(matter_id), "dept": department},
        ).scalar()
        return bool(row)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="permissions._has_team_assignment",
            log_key="permissions._has_team_assignment",
            log_window_seconds=300,
        )
        return False


def _has_family_related_assignment(
    *,
    staff_party_id: Optional[str],
    department: Optional[str],
    matter_id: str,
) -> tuple[bool, bool]:
    """View-only fallback: inherit read access from another explicitly linked family matter.

    This is intentionally narrow:
    - explicit `matter_family` links only
    - requires direct/team assignment on a sibling matter in the same family component
    - does not widen edit/assign/delete/invoice permissions
    """
    mid = (matter_id or "").strip()
    if not mid:
        return (False, False)

    try:
        family_ids = [
            (fid or "").strip()
            for fid in db.session.execute(
                text("""
                    SELECT DISTINCT family_id
                    FROM matter_family
                    WHERE matter_id = :mid
                    """).execution_options(policy_bypass=True),
                {"mid": mid},
            )
            .scalars()
            .all()
            if (fid or "").strip()
        ]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="permissions._has_family_related_assignment.family_ids",
            log_key="permissions._has_family_related_assignment.family_ids",
            log_window_seconds=300,
        )
        return (False, False)

    if not family_ids:
        return (False, False)

    has_direct = False
    role_codes = get_self_assigned_role_codes()
    if staff_party_id and role_codes:
        try:
            stmt = (
                text("""
                    SELECT 1
                    FROM matter_staff_assignment msa
                    JOIN matter m_assigned ON m_assigned.matter_id = msa.matter_id
                    WHERE msa.matter_id IN (
                        SELECT DISTINCT mf.matter_id
                        FROM matter_family mf
                        JOIN matter m_related ON m_related.matter_id = mf.matter_id
                        WHERE mf.family_id IN :family_ids
                          AND mf.matter_id <> :mid
                          AND COALESCE(m_related.is_deleted, false) = false
                    )
                      AND msa.staff_party_id = :sid
                      AND lower(trim(msa.staff_role_code)) IN :role_codes
                      AND COALESCE(m_assigned.is_deleted, false) = false
                    LIMIT 1
                    """)
                .bindparams(
                    bindparam("family_ids", expanding=True),
                    bindparam("role_codes", expanding=True),
                )
                .execution_options(policy_bypass=True)
            )
            has_direct = bool(
                db.session.execute(
                    stmt,
                    {
                        "family_ids": family_ids,
                        "mid": mid,
                        "sid": str(staff_party_id),
                        "role_codes": list(role_codes),
                    },
                ).scalar()
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="permissions._has_family_related_assignment.direct",
                log_key="permissions._has_family_related_assignment.direct",
                log_window_seconds=300,
            )

    has_team = False
    if department:
        try:
            stmt = (
                text("""
                    SELECT 1
                    FROM matter_staff_assignment msa
                    JOIN matter m_assigned ON m_assigned.matter_id = msa.matter_id
                    JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                    WHERE msa.matter_id IN (
                        SELECT DISTINCT mf.matter_id
                        FROM matter_family mf
                        JOIN matter m_related ON m_related.matter_id = mf.matter_id
                        WHERE mf.family_id IN :family_ids
                          AND mf.matter_id <> :mid
                          AND COALESCE(m_related.is_deleted, false) = false
                    )
                      AND ps.dept = :dept
                      AND COALESCE(m_assigned.is_deleted, false) = false
                    LIMIT 1
                    """)
                .bindparams(bindparam("family_ids", expanding=True))
                .execution_options(policy_bypass=True)
            )
            has_team = bool(
                db.session.execute(
                    stmt,
                    {
                        "family_ids": family_ids,
                        "mid": mid,
                        "dept": department,
                    },
                ).scalar()
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="permissions._has_family_related_assignment.team",
                log_key="permissions._has_family_related_assignment.team",
                log_window_seconds=300,
            )

    return (has_direct, has_team)


def _normalize_identifier(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()


def _split_identifier_values(raw_values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values or []:
        text_val = str(raw or "").strip()
        if not text_val:
            continue
        text_val = (
            text_val.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("，", ",")
            .replace(";", ",")
            .replace("|", ",")
            .replace("\n", ",")
        )
        for tok in text_val.split(","):
            token = (tok or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
    return out


def _load_identifier_related_matter_ids(*, matter_id: str) -> set[str]:
    mid = (matter_id or "").strip()
    if not mid:
        return set()

    try:
        from app.models.ip_records import Matter, MatterCustomField, MatterIdentifier

        identifier_rows = (
            MatterIdentifier.query.filter_by(matter_id=mid)
            .with_entities(MatterIdentifier.id_type, MatterIdentifier.id_value)
            .all()
        )
        custom_rows = (
            MatterCustomField.query.filter_by(matter_id=mid)
            .with_entities(MatterCustomField.data)
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="permissions._load_identifier_related_matter_ids.load",
            log_key="permissions._load_identifier_related_matter_ids.load",
            log_window_seconds=300,
        )
        return set()

    identifiers_by_type: dict[str, list[str]] = {}
    for id_type, id_value in identifier_rows:
        type_key = str(id_type or "").strip().lower()
        value = str(id_value or "").strip()
        if not type_key or not value:
            continue
        identifiers_by_type.setdefault(type_key, []).append(value)

    custom_values: list[dict[str, Any]] = []
    for (data,) in custom_rows:
        if isinstance(data, dict):
            custom_values.append(data)

    def _collect_identifier_values(*id_types: str) -> list[str]:
        out: list[str] = []
        for id_type in id_types:
            out.extend(identifiers_by_type.get(str(id_type or "").strip().lower(), []))
        return out

    def _collect_custom_field_values(*keys: str) -> list[str]:
        out: list[str] = []
        for data in custom_values:
            for key in keys:
                value = data.get(key)
                if isinstance(value, list) and key == "related_applications":
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        number = str(item.get("number") or "").strip()
                        if number:
                            out.append(number)
                    continue
                if value:
                    out.append(str(value))
        return out

    priority_vals = _split_identifier_values(
        _collect_identifier_values(*_PRIORITY_IDENTIFIER_TYPES)
        + _collect_custom_field_values("priority_no")
    )
    origin_vals = _split_identifier_values(
        _collect_identifier_values(*_ORIGIN_IDENTIFIER_TYPES)
        + _collect_custom_field_values("parent_application_no", "related_applications")
    )
    app_vals = _split_identifier_values(
        _collect_identifier_values(*_APP_IDENTIFIER_TYPES)
        + _collect_custom_field_values("application_no", "pct_application_no", "ep_application_no")
    )

    related_ids: set[str] = set()

    def _extend_related_ids(*, id_types: tuple[str, ...], raw_candidates: list[str]) -> None:
        candidate_vals = [str(v or "").strip() for v in raw_candidates if str(v or "").strip()]
        candidate_norms = {
            _normalize_identifier(v) for v in candidate_vals if _normalize_identifier(v)
        }
        if not candidate_vals and not candidate_norms:
            return

        dialect = ""
        try:
            dialect = (db.engine.dialect.name or "").lower()
        except Exception:
            dialect = ""

        try:
            if dialect == "postgresql":
                conditions = []
                if candidate_vals:
                    conditions.append(MatterIdentifier.id_value.in_(candidate_vals))
                if candidate_norms:
                    conditions.append(
                        func.upper(
                            func.regexp_replace(
                                func.coalesce(MatterIdentifier.id_value, ""),
                                "[^A-Za-z0-9]",
                                "",
                                "g",
                            )
                        ).in_(list(candidate_norms))
                    )
                if not conditions:
                    return
                query = (
                    db.session.query(MatterIdentifier.matter_id)
                    .join(Matter, Matter.matter_id == MatterIdentifier.matter_id)
                    .filter(MatterIdentifier.matter_id != mid)
                    .filter(MatterIdentifier.id_type.in_(id_types))
                    .filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
                    .filter(or_(*conditions))
                    .distinct()
                )
                related_ids.update(str(rel_mid) for (rel_mid,) in query.all() if rel_mid)
                return

            rows = (
                db.session.query(MatterIdentifier.matter_id, MatterIdentifier.id_value)
                .join(Matter, Matter.matter_id == MatterIdentifier.matter_id)
                .filter(MatterIdentifier.matter_id != mid)
                .filter(MatterIdentifier.id_type.in_(id_types))
                .filter(or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None)))
                .all()
            )
            for rel_mid, id_value in rows:
                norm = _normalize_identifier(str(id_value or ""))
                if id_value in candidate_vals or (norm and norm in candidate_norms):
                    related_ids.add(str(rel_mid))
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="permissions._load_identifier_related_matter_ids.query",
                log_key="permissions._load_identifier_related_matter_ids.query",
                log_window_seconds=300,
            )

    _extend_related_ids(id_types=_APP_IDENTIFIER_TYPES, raw_candidates=priority_vals + origin_vals)
    _extend_related_ids(
        id_types=_PRIORITY_IDENTIFIER_TYPES,
        raw_candidates=app_vals + priority_vals,
    )
    _extend_related_ids(
        id_types=_REFERENCE_IDENTIFIER_TYPES,
        raw_candidates=app_vals + origin_vals,
    )
    return related_ids


def _has_identifier_related_assignment(
    *,
    staff_party_id: Optional[str],
    department: Optional[str],
    matter_id: str,
) -> tuple[bool, bool]:
    """View-only fallback based on identifier-derived related matters."""
    related_ids = sorted(_load_identifier_related_matter_ids(matter_id=matter_id))
    if not related_ids:
        return (False, False)

    has_direct = False
    role_codes = get_self_assigned_role_codes()
    if staff_party_id and role_codes:
        try:
            stmt = (
                text("""
                    SELECT 1
                    FROM matter_staff_assignment
                    WHERE matter_id IN :matter_ids
                      AND staff_party_id = :sid
                      AND lower(trim(staff_role_code)) IN :role_codes
                    LIMIT 1
                    """)
                .bindparams(
                    bindparam("matter_ids", expanding=True),
                    bindparam("role_codes", expanding=True),
                )
                .execution_options(policy_bypass=True)
            )
            has_direct = bool(
                db.session.execute(
                    stmt,
                    {
                        "matter_ids": related_ids,
                        "sid": str(staff_party_id),
                        "role_codes": list(role_codes),
                    },
                ).scalar()
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="permissions._has_identifier_related_assignment.direct",
                log_key="permissions._has_identifier_related_assignment.direct",
                log_window_seconds=300,
            )

    has_team = False
    if department:
        try:
            stmt = (
                text("""
                    SELECT 1
                    FROM matter_staff_assignment msa
                    JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                    WHERE msa.matter_id IN :matter_ids
                      AND ps.dept = :dept
                    LIMIT 1
                    """)
                .bindparams(bindparam("matter_ids", expanding=True))
                .execution_options(policy_bypass=True)
            )
            has_team = bool(
                db.session.execute(
                    stmt,
                    {
                        "matter_ids": related_ids,
                        "dept": department,
                    },
                ).scalar()
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="permissions._has_identifier_related_assignment.team",
                log_key="permissions._has_identifier_related_assignment.team",
                log_window_seconds=300,
            )

    return (has_direct, has_team)


def _legacy_has_view_access(
    *, role_names: set[str], direct_assigned: bool, team_assigned: bool
) -> bool:
    if role_names.intersection(_ROLE_CASE_VIEW_GLOBAL):
        return True
    if direct_assigned:
        return True
    if role_names.intersection(_ROLE_CASE_TEAM) and team_assigned:
        return True
    return False


def _explicit_has_view_access(user, *, direct_assigned: bool, team_assigned: bool) -> bool:
    if _user_has_permission_key(user, PERM_CASE_VIEW_ALL) or _user_has_permission_key(
        user, PERM_CASE_EDIT_ALL
    ):
        return True
    if direct_assigned and (
        _user_has_permission_key(user, PERM_CASE_VIEW_ASSIGNED)
        or _user_has_permission_key(user, PERM_CASE_EDIT_ASSIGNED)
    ):
        return True
    if team_assigned and (
        _user_has_permission_key(user, PERM_CASE_VIEW_TEAM)
        or _user_has_permission_key(user, PERM_CASE_EDIT_TEAM)
    ):
        return True
    return False


def can_manage_case_globally(user=None) -> bool:
    u = user or current_user
    if not _is_authenticated(u):
        return False
    if _uses_explicit_case_policy(u):
        return (
            _user_has_permission_key(u, PERM_CASE_VIEW_ALL)
            or _user_has_permission_key(u, PERM_CASE_EDIT_ALL)
            or _user_has_permission_key(u, PERM_CASE_ASSIGN_ALL)
        )
    return bool(get_user_role_names(u).intersection(_ROLE_CASE_GLOBAL))


def can_access_matter(user, matter_id: str, action: str = "view") -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_active", True) is False:
        return False
    if not matter_id:
        return False

    role_names = get_user_role_names(user)
    use_explicit_policy = _uses_explicit_case_policy(user)
    action = (action or "view").strip().lower()

    staff_pid = _user_staff_party_id(user)
    department = _user_department(user)

    direct_assigned = None
    team_assigned = None
    family_related_assigned = None
    identifier_related_assigned = None

    def _direct() -> bool:
        nonlocal direct_assigned
        if direct_assigned is None:
            direct_assigned = _has_direct_assignment(staff_party_id=staff_pid, matter_id=matter_id)
        return direct_assigned

    def _team() -> bool:
        nonlocal team_assigned
        if team_assigned is None:
            team_assigned = _has_team_assignment(department=department, matter_id=matter_id)
        return team_assigned

    def _family_related() -> tuple[bool, bool]:
        nonlocal family_related_assigned
        if family_related_assigned is None:
            family_related_assigned = _has_family_related_assignment(
                staff_party_id=staff_pid,
                department=department,
                matter_id=matter_id,
            )
        return family_related_assigned

    def _identifier_related() -> tuple[bool, bool]:
        nonlocal identifier_related_assigned
        if identifier_related_assigned is None:
            identifier_related_assigned = _has_identifier_related_assignment(
                staff_party_id=staff_pid,
                department=department,
                matter_id=matter_id,
            )
        return identifier_related_assigned

    if action == "view":
        if use_explicit_policy:
            if _explicit_has_view_access(
                user,
                direct_assigned=_direct(),
                team_assigned=_team(),
            ):
                return True
            family_direct, family_team = _family_related()
            if _explicit_has_view_access(
                user,
                direct_assigned=family_direct,
                team_assigned=family_team,
            ):
                return True
            identifier_direct, identifier_team = _identifier_related()
            return _explicit_has_view_access(
                user,
                direct_assigned=identifier_direct,
                team_assigned=identifier_team,
            )
        if _legacy_has_view_access(
            role_names=role_names,
            direct_assigned=_direct(),
            team_assigned=_team(),
        ):
            return True
        family_direct, family_team = _family_related()
        if _legacy_has_view_access(
            role_names=role_names,
            direct_assigned=family_direct,
            team_assigned=family_team,
        ):
            return True
        identifier_direct, identifier_team = _identifier_related()
        return _legacy_has_view_access(
            role_names=role_names,
            direct_assigned=identifier_direct,
            team_assigned=identifier_team,
        )

    if action == "edit_case":
        if use_explicit_policy:
            if _user_has_permission_key(user, PERM_CASE_EDIT_ALL):
                return True
            if _user_has_permission_key(user, PERM_CASE_EDIT_ASSIGNED) and _direct():
                return True
            if _user_has_permission_key(user, PERM_CASE_EDIT_TEAM) and _team():
                return True
            return False
        if role_names.intersection(_ROLE_CASE_GLOBAL):
            return True
        editable_roles = role_names - _ROLE_READ_ONLY
        if editable_roles and _direct():
            return True
        if role_names.intersection(_ROLE_CASE_TEAM_LEAD) and _team():
            return True
        return False

    if action == "assign_staff":
        if use_explicit_policy:
            if _user_has_permission_key(user, PERM_CASE_ASSIGN_ALL):
                return True
            if _user_has_permission_key(user, PERM_CASE_ASSIGN_TEAM) and _team():
                return True
            return False
        if role_names.intersection(_ROLE_CASE_GLOBAL):
            return True
        if role_names.intersection(_ROLE_CASE_ASSIGN) and _team():
            return True
        return False

    if action == "delete_case":
        if use_explicit_policy:
            return _user_has_permission_key(user, PERM_CASE_DELETE)
        return bool(role_names.intersection(_ROLE_CASE_DELETE))

    if action in ("invoice", "mgmt_deadline", "finance"):
        if use_explicit_policy:
            if not (
                _user_has_permission_key(user, PERM_INVOICE_MANAGE)
                or _user_has_permission_key(user, PERM_CASE_EDIT_ALL)
                or is_admin(user)
            ):
                return False
            return _explicit_has_view_access(
                user,
                direct_assigned=_direct(),
                team_assigned=_team(),
            )

        # is_invoice_manager()  "manager" management role Extend
        invoice_roles = set(get_invoice_roles())
        if "manager" in invoice_roles:
            invoice_roles.update(get_management_roles())
        invoice_roles.add(ROLE_PARTNER_ATTORNEY)
        if ROLE_ADMIN in role_names:
            return True
        if not role_names.intersection(invoice_roles):
            return False
        return _legacy_has_view_access(
            role_names=role_names,
            direct_assigned=_direct(),
            team_assigned=_team(),
        )

    return False


def require_matter_access(matter_id: str, action: str = "view", user=None) -> None:
    if not can_access_matter(user or current_user, matter_id, action):
        abort(403, "You do not have permission to access this matter.")


def resolve_matter_id_for_case_ref(case_ref: Optional[str]) -> Optional[str]:
    try:
        from app.services.matter.matter_identity_service import MatterIdentityService
    except Exception:
        return None
    return MatterIdentityService.resolve_matter_id_for_case_ref(case_ref)


def can_access_legacy_case(user, case, action: str = "view") -> bool:
    from app.services.case.legacy_case_adapter import LegacyCaseAdapter

    return LegacyCaseAdapter.can_access(user, case, action)


def require_legacy_case_access(case, action: str = "view", user=None) -> None:
    if not can_access_legacy_case(user or current_user, case, action):
        abort(403, "You do not have permission to access this legacy case.")


def resolve_matter_action(req=None) -> str:
    req = req or request
    method = (req.method or "GET").upper()
    if method in _SAFE_METHODS:
        return "view"
    return "edit_case"


def matter_action(action: str):
    """
    Attach matter access metadata to a route.
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Preserve the route metadata for downstream policy checks.
            setattr(wrapper, "_matter_action", action)

            if not current_app.config.get("POLICY_ENGINE_ENABLED", True):
                return fn(*args, **kwargs)

            # Apply the same read-policy path used by service-layer object checks.
            if not current_user.is_authenticated:
                abort(401)
            return fn(*args, **kwargs)

        setattr(wrapper, "_matter_action", action)
        return wrapper

    return decorator


def extract_matter_id(view_args: Optional[dict]) -> Optional[str]:
    if not view_args:
        return None
    for key in ("matter_id", "case_id", "mid"):
        if key in view_args:
            value = view_args.get(key)
            if value is not None:
                return str(value)
    return None


def can_access_uploads(user=None) -> bool:
    u = user or current_user
    if not _is_authenticated_active(u):
        return False
    if is_admin(u) or is_manager(u) or is_invoice_manager(u):
        return True
    try:
        if callable(getattr(u, "is_mgmt_role", None)) and u.is_mgmt_role():
            return True
        if callable(getattr(u, "is_work_role", None)) and u.is_work_role():
            return True
    except Exception as exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Swallowed exception in can_access_uploads: %s", exc, exc_info=True)
    allowed = _parse_role_csv(current_app.config.get("UPLOADS_ALLOWED_ROLES") or "")
    if allowed and get_user_role_names(u).intersection(allowed):
        return True
    return False


def permission_required(perm_key: str):
    """
    Decorator for routes. Checks if current user has the specific permission in their roles.
    Admins are automatically granted access.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            # Admin superuser bypass (matches role_required behavior)
            if is_admin(current_user):
                return view(*args, **kwargs)

            if not current_user.has_permission(perm_key):
                abort(403, "You do not have permission to access this menu.")

            return view(*args, **kwargs)

        return wrapped

    return decorator


def role_required(*allowed_roles):
    """
    Legacy Decorator for routes. Checks if current user has one of the allowed roles.
    Will be progressively replaced by @permission_required.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            user_roles = get_user_role_names(current_user)

            # 1. Admin superuser bypass
            if "admin" in user_roles:
                return view(*args, **kwargs)

            # 2. Expand 'manager' to include all management roles
            extended_roles = {_normalize_role(r) for r in allowed_roles if _normalize_role(r)}
            if "manager" in extended_roles:
                extended_roles.update(get_management_roles())
            if "staff" in extended_roles:
                extended_roles.update({_normalize_role(r) for r in ROLE_WORK_ALIASES})

            if not user_roles.intersection(extended_roles):
                abort(403, "You do not have the required role for this page.")

            return view(*args, **kwargs)

        return wrapped

    return decorator


def check_permission(permission_type: str, user=None) -> bool:
    """
    Generic permission checker.
    Support types: 'admin', 'manage_case', 'manage_invoice', 'delete_file'
    """
    # In tests, Flask-Login's LOGIN_DISABLED bypasses @login_required but doesn't
    # automatically grant privileges. Scope this to app.testing only so production
    # cannot accidentally disable permission checks.
    if has_app_context() and current_app.testing and current_app.config.get("LOGIN_DISABLED"):
        return True

    # For now, map most operational permissions to is_manager()
    if permission_type == "admin":
        return is_admin(user)
    if permission_type in {"manage_case", "delete_file"}:
        return can_manage_case_globally(user)
    if permission_type == "manage_invoice":
        u = user or current_user
        return is_invoice_manager(u) or _user_has_permission_key(u, PERM_INVOICE_MANAGE)

    # Default fail
    return False


def require_permission(permission_type: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            # Keep permission decorators compatible with Flask-Login test bypass.
            if (
                has_app_context()
                and current_app.testing
                and current_app.config.get("LOGIN_DISABLED")
            ):
                return view(*args, **kwargs)
            if not current_user.is_authenticated:
                abort(401)
            if not check_permission(permission_type):
                abort(403, "You do not have the required permission.")
            return view(*args, **kwargs)

        return wrapped

    return decorator
