"""
Unified MGMT/WORK classification utility.

Single Source of Truth for task type classification across the entire application.

Classification Priority (CASE-ROLE-BASED):
1. MatterStaffAssignment.staff_role_code for the specific case
2. Default: WORK

NOTE: Classification is based on the user's role IN THE SPECIFIC CASE,
NOT their global User.role. A partner_attorney can be a manager for a specific case.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


def _report_swallowed_exception(exc: Exception, *, context: str) -> None:
    try:
        from app.utils.error_logging import report_swallowed_exception

        report_swallowed_exception(exc, context=context)
    except Exception as report_exc:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Failed to report swallowed exception in %s: %s",
                context,
                report_exc,
                exc_info=True,
            )
        logger.warning("Swallowed exception in %s: %s", context, exc)


# Staff role codes that indicate MGMT tasks
MGMT_STAFF_ROLES = frozenset(
    {
        "manager",
        "mgmt",
    }
)

# Staff role codes that indicate WORK tasks
WORK_STAFF_ROLES = frozenset(
    {
        "attorney",
        "retainer",
        "staff",
        "draftsman",
        "handler",
    }
)

# Category constants (kept for backward compatibility in queries)
WORK_CATEGORIES = frozenset(
    {
        "FILING",
        "EXAM",
        "REG",
        "USPTO_OA",
        "USPTO_NOTICE",
        "DOCKET",
        "WORK",
        "MGMT_WORK",
        "WORK_MGMT",
        "LEGAL",
        "DEADLINE",
        "V2_LIMIT",
        "filing",
        "exam",
        "reg",
        "uspto_oa",
        "uspto_notice",
        "docket",
        "work",
        "mgmt_work",
        "work_mgmt",
        "legal",
        "deadline",
        "v2_limit",
    }
)

MGMT_CATEGORIES = frozenset(
    {
        "MGMT",
        "MGMT_WORK",
        "WORK_MGMT",
        "MANAGEMENT",
        "NOTICE",
        "SLA",
        "ADMIN",
        "mgmt",
        "mgmt_work",
        "work_mgmt",
        "management",
        "notice",
        "sla",
        "admin",
    }
)

# Kept for backward compatibility
MGMT_ROLES = frozenset({"mgmt_director", "mgmt_staff", "manager"})
WORK_ROLES = frozenset(
    {
        "lead_attorney",
        "partner_attorney",
        "patent_staff",
        "attorney",
        "handler",
        "staff",
        "draftsman",
        "admin",
    }
)


def _has_mgmt_hint(
    *,
    category: str | None,
    name_ref: str | None,
    name_free: str | None,
) -> bool:
    cat = (category or "").strip().upper()
    if cat and cat in {c.upper() for c in MGMT_CATEGORIES}:
        return True
    if (name_ref or "").strip().upper().startswith("MGMT:"):
        return True
    if (name_free or "").strip().upper().startswith("MGMT:"):
        return True
    return False


def classify_task_type(
    *,
    assignee_id: int | None = None,
    assignee: "User | None" = None,
    owner_staff_party_id: str | None = None,
    staff_role: str | None = None,
    owner_role: str | None = None,
    category: str | None = None,
    name_ref: str | None = None,
    name_free: str | None = None,
    matter_id: str | None = None,
) -> str:
    """
    Unified MGMT/WORK classification function.

    Classification is based on the user's role IN THE SPECIFIC CASE:
    1. Use staff_role if provided (from MatterStaffAssignment.staff_role_code)
    2. Look up staff_role_code from MatterStaffAssignment if matter_id and owner_staff_party_id provided
    3. Look up staff_role_code from MatterStaffAssignment if matter_id and assignee_id provided
    4. If staff_role lookup failed, apply strong MGMT hints (category/ref prefixes)
    5. Default: WORK

    Manager role in case → "mgmt"
    Attorney/Handler role in case → "work"

    Args:
        assignee_id: User.id of the assignee
        assignee: User object directly if available
        owner_staff_party_id: staff_party_id of the owner
        staff_role: Role from MatterStaffAssignment.staff_role_code (manager/attorney/handler)
        owner_role: (ignored - kept for backward compatibility)
        category: Optional category hint (e.g. MGMT)
        name_ref: Optional name_ref hint (e.g. MGMT:...)
        name_free: Optional name_free hint (e.g. MGMT:...)
        matter_id: Matter ID for looking up role in MatterStaffAssignment

    Returns:
        "mgmt" or "work"
    """

    # 1. Use staff_role if directly provided
    if staff_role:
        sr = staff_role.lower().strip()
        if sr in MGMT_STAFF_ROLES:
            return "mgmt"
        if sr in WORK_STAFF_ROLES:
            return "work"

    # 2. Look up from MatterStaffAssignment by staff_party_id
    if matter_id and owner_staff_party_id:
        role = get_staff_role_for_matter(matter_id, owner_staff_party_id)
        if role:
            r = role.lower().strip()
            if r in MGMT_STAFF_ROLES:
                return "mgmt"
            if r in WORK_STAFF_ROLES:
                return "work"

    # 3. Look up from MatterStaffAssignment by assignee_id
    if matter_id and assignee_id:
        role = get_staff_role_for_matter_by_user_id(matter_id, assignee_id)
        if role:
            r = role.lower().strip()
            if r in MGMT_STAFF_ROLES:
                return "mgmt"
            if r in WORK_STAFF_ROLES:
                return "work"

    # 4. If we have assignee but no matter_id, try to get staff_party_id and look up
    if assignee_id and not matter_id:
        try:
            from app.models.user import User

            user = User.query.get(assignee_id)
            if user and user.staff_party_id:
                # Can't determine role without matter_id
                pass
        except Exception as exc:
            _report_swallowed_exception(
                exc,
                context=f"classify_task_type(user_lookup assignee_id={assignee_id})",
            )

    # 5. Strong MGMT hints (e.g. MGMT:* ref or MGMT category).
    if _has_mgmt_hint(category=category, name_ref=name_ref, name_free=name_free):
        return "mgmt"

    # 6. Default to work
    return "work"


def determine_category_by_staff_role(
    matter_id: str | None,
    assignee_id: int | None = None,
    staff_party_id: str | None = None,
    staff_role: str | None = None,
    category: str | None = None,
    name_ref: str | None = None,
    name_free: str | None = None,
) -> str:
    """
    Determine category (MGMT/WORK) based on the user's role in the specific case.

    Args:
        matter_id: Matter ID (required for lookup)
        assignee_id: User.id of the assignee
        staff_party_id: staff_party_id of the assignee
        staff_role: Known staff_role_code if available
        category: Optional category hint (e.g. MGMT)
        name_ref: Optional name_ref hint (e.g. MGMT:...)
        name_free: Optional name_free hint (e.g. MGMT:...)

    Returns:
        "MGMT" or "WORK"
    """
    # 1. Use staff_role if directly provided
    if staff_role:
        sr = staff_role.lower().strip()
        if sr in MGMT_STAFF_ROLES:
            return "MGMT"
        if sr in WORK_STAFF_ROLES:
            return "WORK"

    if not matter_id:
        return (
            "MGMT"
            if _has_mgmt_hint(category=category, name_ref=name_ref, name_free=name_free)
            else "WORK"
        )

    # 2. Look up by staff_party_id
    if staff_party_id:
        role = get_staff_role_for_matter(matter_id, staff_party_id)
        if role:
            r = role.lower().strip()
            if r in MGMT_STAFF_ROLES:
                return "MGMT"
            return "WORK"

    # 3. Look up by assignee_id
    if assignee_id:
        role = get_staff_role_for_matter_by_user_id(matter_id, assignee_id)
        if role:
            r = role.lower().strip()
            if r in MGMT_STAFF_ROLES:
                return "MGMT"
            return "WORK"

    if _has_mgmt_hint(category=category, name_ref=name_ref, name_free=name_free):
        return "MGMT"

    return "WORK"


def get_staff_role_for_matter(matter_id: str, staff_party_id: str) -> str | None:
    """
    Look up the staff_role_code for a specific matter and staff_party_id.

    Returns:
        "manager", "attorney", "handler", etc. or None
    """
    if not matter_id or not staff_party_id:
        return None

    try:
        from app.extensions import db
        from app.utils.policy_sql import policy_text as text

        result = db.session.execute(
            text(
                """
                SELECT staff_role_code FROM matter_staff_assignment
                WHERE matter_id = :mid AND staff_party_id = :spid
                LIMIT 1
            """
            ).execution_options(policy_bypass=True),
            {"mid": matter_id, "spid": str(staff_party_id)},
        ).fetchone()

        if result:
            return result[0]
    except Exception as exc:
        try:
            from app.utils.error_logging import report_swallowed_exception

            report_swallowed_exception(
                exc,
                context=(
                    "get_staff_role_for_matter"
                    f"(matter_id={matter_id}, staff_party_id={staff_party_id})"
                ),
            )
        except Exception:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Failed to report swallowed exception in get_staff_role_for_matter",
                    exc_info=True,
                )
            logger.warning(
                "Swallowed exception in get_staff_role_for_matter: %s",
                exc,
            )

    return None


def get_staff_role_for_matter_by_user_id(matter_id: str, user_id: int) -> str | None:
    """
    Look up the staff_role_code for a specific matter by user_id.
    First gets user's staff_party_id, then looks up in MatterStaffAssignment.

    Returns:
        "manager", "attorney", "handler", etc. or None
    """
    if not matter_id or not user_id:
        return None

    try:
        from app.models.user import User

        user = User.query.get(user_id)
        if user and user.staff_party_id:
            return get_staff_role_for_matter(matter_id, user.staff_party_id)
    except Exception as exc:
        _report_swallowed_exception(
            exc,
            context=f"get_staff_role_for_matter_by_user_id(matter_id={matter_id}, user_id={user_id})",
        )

    return None


def get_user_role(user_id: int) -> str | None:
    """Get User.role by user_id (kept for backward compatibility)."""
    if not user_id:
        return None

    try:
        from app.models.user import User

        user = User.query.get(user_id)
        if user:
            return user.role
    except Exception as exc:
        _report_swallowed_exception(
            exc,
            context=f"get_user_role(user_id={user_id})",
        )

    return None
