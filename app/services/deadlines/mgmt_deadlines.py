from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Callable

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, or_
from sqlalchemy.exc import DBAPIError, InvalidRequestError, PendingRollbackError

from app.extensions import db
from app.models.party import PartyStaff
from app.models.ip_records import DocketItem, Matter, MatterCustomField, MatterEvent
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services.annuity.annuity_management import is_annuity_management_disabled_for_matter
from app.services.case.case_kind import is_uspto_managed_case_kind, is_uspto_managed_matter
from app.services.core.config_service import ConfigService
from app.services.docket_manual_state import memo_has_manual_abandon_lock
from app.services.workflow.sync_requests import enqueue_docket_sync_for_item
from app.utils.annuity_deadline_routing import (
    is_annuity_status_red_deadline,
    is_annuity_status_red_label,
)
from app.utils.docket_dates import effective_due_for_legal, normalize_done_date
from app.utils.docket_visibility import compute_visible_from
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.status_red_visibility import is_non_action_status_red_label
from app.utils.status_red_visibility import (
    status_red_visibility_window as resolve_status_red_visibility_window,
)
from app.utils.task_assignment_rules import is_manager_only_notice

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_WS_RE = re.compile(r"\s+")
_COUNTRY_SPLIT_RE = re.compile(r"[,/;|\n]+")

_TRUTHY_TOKENS = {"1", "Y", "YES", "TRUE", "T"}
_NOTICE_SEND_RESPONSE_HINTS = (
    "",
    "",
    "",
    "response",
    "amendment",
    "opinion",
)
_NOTICE_SEND_NOTICE_HINTS = (
    "Notice",
    "",
    "Publication",
    "",
    "",
    "Guidance",
    "",
    "",
    "",
)
_NOTICE_SEND_SLA_REF_PREFIX = "MGMT:NOTICE_SEND_3D:"


_DEFAULT_TEMPLATES = [
    {
        "id": "NOTICE_SEND_3D",
        "trigger": "office_action_received",
        "offset_days": 3,
        "category": "NOTICE",
        "title": "Notice Client(3 )",
        "assignee_field": "manager",
    },
    {
        "id": "FOREIGN_FILING_NOTICE_3M",
        "trigger": "deadline_code",
        "deadline_code": "FOREIGN_FILING_PARIS",
        "offset_months": -3,
        "visible_offset_days": -14,
        "category": "MGMT",
        "title": "ForeignFiling Guidance(3items )",
        "assignee_field": "manager",
        "skip_if_field_set": "foreign_filing_date",
    },
    {
        "id": "PCT_ADVISORY_19M",
        "trigger": "deadline_code",
        "deadline_code": "PCT_ADVISORY_19M",
        "offset_days": 0,
        "category": "MGMT",
        "title": "Domestic deadline first notice",
        "assignee_field": "manager",
        "skip_if_field_set": "national_phase_last_entry_date",
    },
]

# When MGMT_TEMPLATES_JSON exists (seeded early) it may miss newer fields.
# Apply safe defaults for specific templates unless the admin explicitly configured them.
_TEMPLATE_DEFAULT_PATCHES: dict[str, dict[str, int]] = {
    # Ensure the foreign filing notice enters Task 2 weeks before its due date.
    "FOREIGN_FILING_NOTICE_3M": {"visible_offset_days": -14},
}
_TEMPLATE_DEFAULT_ADDITION_IDS = frozenset({"PCT_ADVISORY_19M"})

# Fallback due sources for template trigger=deadline_code
_TEMPLATE_DEADLINE_FALLBACK_FIELDS: dict[str, tuple[str, ...]] = {
    # Ensure 3-month foreign filing notice is still created when engine output is absent
    # but custom field due date is present.
    "FOREIGN_FILING_PARIS": ("foreign_filing_deadline",),
    "PCT_ADVISORY_19M": ("national_phase_19m_deadline",),
}

# Optional fallback to Matter.status_red_related_date when status_red is authoritative.
_TEMPLATE_DEADLINE_STATUS_RED_FALLBACKS: dict[str, str] = {
    "FOREIGN_FILING_PARIS": "ForeignFilingDeadline",
}

# Required fields by trigger type for template validation
_TRIGGER_REQUIRED_FIELDS = {
    "deadline_code": ["deadline_code"],
    "office_action_received": [],
    "status_red": [],
}

_DISABLED_FOLLOWUP_TEMPLATE_IDS = frozenset({"FOREIGN_FILING_EXPIRED_NOTICE_3D"})


def _is_retryable_session_read_error(exc: Exception) -> bool:
    if isinstance(exc, PendingRollbackError):
        return True
    if isinstance(exc, InvalidRequestError):
        try:
            msg = str(exc).lower()
        except Exception:
            msg = ""
        return any(
            marker in msg
            for marker in (
                "session is in 'committed' state",
                "session is in 'prepared' state",
                "no further sql can be emitted within this transaction",
            )
        )
    if isinstance(exc, DBAPIError):
        try:
            pgcode = getattr(exc.orig, "pgcode", None)
        except Exception:
            pgcode = None
        if pgcode == "25P02" or bool(getattr(exc, "connection_invalidated", False)):
            return True
    try:
        msg = str(exc).lower()
    except Exception:
        msg = ""
    return any(
        marker in msg
        for marker in (
            "infailedsqltransaction",
            "current transaction is aborted",
            "server closed the connection unexpectedly",
            "connection reset by peer",
            "connection refused",
            "could not connect to server",
            "terminating connection",
            "can't reconnect until invalid transaction is rolled back",
        )
    )


def _best_effort_deadline_session_rollback() -> bool:
    try:
        sess = getattr(db, "session", None)
    except Exception:
        sess = None
    if sess is None:
        return False
    try:
        actual_sess = sess() if callable(sess) else sess
    except Exception:
        actual_sess = sess
    try:
        if bool(getattr(actual_sess, "_flushing", False)):
            return False
    except Exception:
        return False
    try:
        if actual_sess.new or actual_sess.dirty or actual_sess.deleted:
            return False
    except Exception:
        return False
    try:
        get_nested_tx = getattr(actual_sess, "get_nested_transaction", None)
        if callable(get_nested_tx) and get_nested_tx() is not None:
            return False
    except Exception:
        return False
    try:
        sess.rollback()
        return True
    except Exception:
        return False


def _load_custom_field_rows_out_of_band(matter_id: str) -> list[object]:
    stmt = text(
        """
        SELECT data
        FROM matter_custom_field
        WHERE matter_id = :matter_id
        ORDER BY id ASC
        """
    ).execution_options(policy_bypass=True)
    with db.engine.connect() as conn:
        return [row[0] for row in conn.execute(stmt, {"matter_id": str(matter_id)}).all()]


def _load_matter_staff_assignments_out_of_band(matter_id: str) -> list[tuple[str, str]]:
    stmt = text(
        """
        SELECT staff_role_code, staff_party_id
        FROM matter_staff_assignment
        WHERE matter_id = :matter_id
        ORDER BY COALESCE(seq, 1) ASC, msa_id ASC
        """
    ).execution_options(policy_bypass=True)
    with db.engine.connect() as conn:
        return [
            (
                (row[0] or "").strip().lower(),
                (row[1] or "").strip(),
            )
            for row in conn.execute(stmt, {"matter_id": str(matter_id)}).all()
        ]


_POLICY_CONFIG_KEY = "DEADLINE_POLICY_JSON"
_DEFAULT_DEADLINE_POLICIES = [
    {
        "id": "FOREIGN_FILING_PARIS_MAIN",
        "match": {
            "name_ref_prefixes": ["MGMT:STATUS_RED:ForeignFilingDeadline"],
        },
        "deadline_codes": ["FOREIGN_FILING_PARIS"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "CHILD_MATTER_CREATION",
        "remedyability": "DISCRETIONARY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "FOREIGN_FILING_NOTICE_3M",
        "match": {
            "template_ids": ["FOREIGN_FILING_NOTICE_3M"],
        },
        "deadline_codes": ["FOREIGN_FILING_PARIS"],
        "impact_scope": "REMINDER",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "INTERNAL_AUTO",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "REQUEST_EXAMINATION_MAIN",
        "match": {
            "name_ref_prefixes": ["MGMT:STATUS_RED:Examination requestDeadline"],
        },
        "deadline_codes": ["REQUEST_EXAMINATION"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "DISCRETIONARY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "PCT_NATIONAL_PHASE_MAIN",
        "match": {
            "name_ref_prefixes": ["MGMT:STATUS_RED:PCTDomesticDeadline"],
        },
        "deadline_codes": ["PCT_NATIONAL_PHASE"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "DISCRETIONARY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "PCT_CH2_DEMAND",
        "deadline_codes": ["PCT_CH2_DEMAND"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "PCT_ADVISORY_19M",
        "deadline_codes": ["PCT_ADVISORY_19M"],
        "impact_scope": "REMINDER",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "INTERNAL_AUTO",
        "post_due_policy": "",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "",
        "lockable": True,
    },
    {
        "id": "PCT_ADVISORY_27M",
        "deadline_codes": ["PCT_ADVISORY_27M"],
        "impact_scope": "REMINDER",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "INTERNAL_AUTO",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "CLAIM_DEFERMENT",
        "deadline_codes": ["CLAIM_DEFERMENT"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
    {
        "id": "NOVELTY_GRACE",
        "deadline_codes": ["NOVELTY_GRACE"],
        "impact_scope": "OPPORTUNITY",
        "action_target": "SAME_MATTER_ACTION",
        "remedyability": "NO_REMEDY",
        "deadline_shape": "SINGLE",
        "decision_ownership": "CLIENT_REQUIRED",
        "post_due_policy": "AUTO_EXPIRE",
        "effective_due_basis": "due_date",
        "expire_after_days": 0,
        "close_mark": "EXPIRED",
        "lockable": True,
    },
]

_POLICY_PATCH_IDS = frozenset({"REQUEST_EXAMINATION_MAIN", "PCT_NATIONAL_PHASE_MAIN"})
_POLICY_DEFAULT_PATCHES = [
    p for p in _DEFAULT_DEADLINE_POLICIES if (p.get("id") or "").strip() in _POLICY_PATCH_IDS
]
_POLICY_RUNTIME_FIELD_PATCHES: dict[str, dict[str, object]] = {
    # The 19-month/one-year notice is an internal action deadline. If it is overdue,
    # it should stay visible as work to do rather than disappearing as expired.
    "PCT_ADVISORY_19M": {
        "post_due_policy": "",
        "close_mark": "",
    },
}


def _parse_date(v) -> date | None:
    """Parse various date formats into a date object."""
    if not v:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    s = s.strip("[](){}<>")
    m = _DATE_RE.search(s)
    if m:
        s = m.group(1)
    try:
        return date.fromisoformat(s.split("T")[0])
    except Exception:
        return None


def _date_token(v: object) -> str:
    try:
        return str(v or "").strip().split("T")[0].strip()
    except Exception:
        return ""


def _active_docket_query(query):
    if hasattr(DocketItem, "is_deleted"):
        query = query.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    return query


def _is_notice_send_sla_ref(name_ref: str | None) -> bool:
    return (name_ref or "").strip().upper().startswith(_NOTICE_SEND_SLA_REF_PREFIX)


def _apply_offset(base: date, *, days: int = 0, months: int = 0, years: int = 0) -> date:
    """Apply date offset using relativedelta."""
    return base + relativedelta(days=days, months=months, years=years)


def _priority_exam_progress_due(custom_data: dict) -> date | None:
    data = custom_data if isinstance(custom_data, dict) else {}
    if not _is_truthy(data.get("priority_exam_request")):
        return None
    base = _parse_date(data.get("application_date") or data.get("filing_date"))
    if base is None:
        return None
    return _apply_offset(base, days=7)


def _parse_memo(memo: str | None) -> dict:
    """Parse DocketItem memo JSON safely.

    Returns a dict with parsed JSON data. If parsing fails or memo is empty,
    returns an empty dict.
    """
    if not memo:
        return {}
    try:
        data = json.loads(memo)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _merge_custom_fields(matter_id: str) -> dict:
    """
    Merge custom fields for a matter.
    Uses LAST-WRITE-WINS strategy: later records override earlier ones.
    """
    data: dict = {}
    rows: list[object] | None = None

    def _load_rows() -> list[object]:
        return (
            MatterCustomField.query.filter_by(matter_id=str(matter_id))
            .order_by(MatterCustomField.id.asc())
            .all()
        )

    try:
        rows = _load_rows()
    except Exception as exc:
        final_exc = exc
        if _is_retryable_session_read_error(exc) and _best_effort_deadline_session_rollback():
            try:
                rows = _load_rows()
            except Exception as retry_exc:
                final_exc = retry_exc
        if rows is None and _is_retryable_session_read_error(final_exc):
            try:
                rows = _load_custom_field_rows_out_of_band(matter_id)
            except Exception as fallback_exc:
                report_swallowed_exception(
                    fallback_exc,
                    context=f"mgmt_deadlines.merge_custom_fields.query_fallback(matter_id={matter_id})",
                    log_key="mgmt_deadlines.merge_custom_fields.query_fallback",
                    log_window_seconds=300,
                )
        if rows is None:
            report_swallowed_exception(
                final_exc,
                context=f"mgmt_deadlines.merge_custom_fields.query(matter_id={matter_id})",
                log_key="mgmt_deadlines.merge_custom_fields.query",
                log_window_seconds=300,
            )
            return {}
    if rows is None:
        return {}
    try:
        in_session_rows: list[object] = []
        for source in (db.session.identity_map.values(), db.session.new):
            for obj in source:
                if isinstance(obj, MatterCustomField) and str(obj.matter_id) == str(matter_id):
                    in_session_rows.append(obj)
        if in_session_rows:
            seen = {id(row) for row in rows}
            rows.extend(row for row in in_session_rows if id(row) not in seen)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"mgmt_deadlines.merge_custom_fields.identity_map(matter_id={matter_id})",
            log_key="mgmt_deadlines.merge_custom_fields.identity_map",
            log_window_seconds=300,
        )
    # Process in order (oldest first), so newer values override
    for r in rows:
        try:
            payload = r.data if hasattr(r, "data") else r
            d = dict(payload or {})
        except Exception:
            d = {}
        for k, v in d.items():
            # Always override with newer value (last write wins)
            data[k] = v
    return data


def _resolve_owner_from_matter_staff(
    matter_id: str,
    role_priority: list[str] | None = None,
    category_type: str | None = None,
) -> str | None:
    """Resolve owner staff_party_id from MatterStaffAssignment or MatterCustomField.

    Args:
        matter_id: The matter ID to look up
        role_priority: List of roles to check in priority order
        category_type: 'MGMT' or 'WORK' to determine default role priority

    Returns:
        staff_party_id of the resolved owner, or None if not found
    """
    if role_priority is None:
        if category_type == "MGMT":
            # Management tasks -> manager first
            role_priority = [
                "manager",
                "mgmt",
                "handler",
                "staff",
                "draftsman",
                "attorney",
                "retainer",
            ]
        else:
            # Work tasks -> attorney first
            role_priority = [
                "attorney",
                "retainer",
                "handler",
                "staff",
                "draftsman",
                "manager",
                "mgmt",
            ]

    # 1. Try MatterStaffAssignment first
    valid_roles = {"attorney", "retainer", "handler", "staff", "draftsman", "manager", "mgmt"}
    assignments: list[tuple[str, str]] | None = None

    def _load_assignments() -> list[tuple[str, str]]:
        from app.models.ip_records import MatterStaffAssignment

        # Use ORM to benefit from session autoflush (visibility of uncommitted additions)
        assignments_query = MatterStaffAssignment.query.filter_by(matter_id=str(matter_id))
        if hasattr(MatterStaffAssignment, "seq"):
            assignments_query = assignments_query.order_by(
                func.coalesce(MatterStaffAssignment.seq, 1).asc(),
                MatterStaffAssignment.msa_id.asc(),
            )
        else:
            assignments_query = assignments_query.order_by(MatterStaffAssignment.msa_id.asc())
        return [
            (
                (msa.staff_role_code or "").strip().lower(),
                (msa.staff_party_id or "").strip(),
            )
            for msa in assignments_query.all()
        ]

    try:
        assignments = _load_assignments()
    except Exception as exc:
        final_exc = exc
        if _is_retryable_session_read_error(exc) and _best_effort_deadline_session_rollback():
            try:
                assignments = _load_assignments()
            except Exception as retry_exc:
                final_exc = retry_exc
        if assignments is None and _is_retryable_session_read_error(final_exc):
            try:
                assignments = _load_matter_staff_assignments_out_of_band(matter_id)
            except Exception as fallback_exc:
                report_swallowed_exception(
                    fallback_exc,
                    context=f"mgmt_deadlines.resolve_owner.matter_staff_assignment_fallback(matter_id={matter_id})",
                    log_key="mgmt_deadlines.resolve_owner.matter_staff_assignment_fallback",
                    log_window_seconds=300,
                )
        if assignments is None:
            report_swallowed_exception(
                final_exc,
                context=f"mgmt_deadlines.resolve_owner.matter_staff_assignment(matter_id={matter_id})",
                log_key="mgmt_deadlines.resolve_owner.matter_staff_assignment",
                log_window_seconds=300,
            )
            assignments = []

    by_role: dict[str, str] = {}
    if assignments:
        for role, staff_party_id in assignments:
            if role in valid_roles and role not in by_role and staff_party_id:
                by_role[role] = staff_party_id

        if by_role:
            # Return first match by priority
            for role in role_priority:
                if role in by_role:
                    return by_role[role]

            # Fallback: return the first one found
            return list(by_role.values())[0]

    # 2. Fallback to MatterCustomField
    try:
        custom_data = _merge_custom_fields(str(matter_id))
        if not custom_data:
            return None

        # Map role names to custom field keys
        role_to_field = {
            "attorney": "attorney",
            "retainer": "attorney",
            "manager": "manager",
            "mgmt": "manager",
            "handler": "handler",
            "staff": "handler",
            "draftsman": "handler",
        }

        # Check fields in priority order
        for role in role_priority:
            field = role_to_field.get(role)
            if not field:
                continue

            name_value = (custom_data.get(field) or "").strip()
            if not name_value:
                continue

            # Try to resolve name to staff_party_id via User table
            # Handle multiple values (semicolon or comma separated)
            for sep in (";", ","):
                if sep in name_value:
                    name_value = name_value.split(sep)[0].strip()
                    break

            # Try username match
            user = User.query.filter_by(username=name_value, is_active=True).first()
            if user and user.staff_party_id:
                logger.debug(
                    f"Resolved {name_value} to staff_party_id {user.staff_party_id} via username"
                )
                return user.staff_party_id

            # Try display_name match (safely check if column exists)
            try:
                user = User.query.filter(
                    User.display_name.ilike(name_value), User.is_active.is_(True)
                ).first()
                if user and user.staff_party_id:
                    logger.debug(
                        f"Resolved {name_value} to staff_party_id {user.staff_party_id} via display_name"
                    )
                    return user.staff_party_id
            except Exception as exc:
                # Display name column is optional; log and continue fallback resolution.
                report_swallowed_exception(
                    exc,
                    context="mgmt_deadlines.resolve_assignee.display_name",
                    log_key="mgmt_deadlines.resolve_assignee.display_name",
                    log_window_seconds=300,
                )

            # Try PartyStaff by staff_code
            ps = (
                PartyStaff.query.filter(PartyStaff.staff_code.ilike(name_value))
                .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
                .first()
            )
            if ps and ps.party_id:
                logger.debug(f"Resolved {name_value} to party_id {ps.party_id} via PartyStaff")
                return ps.party_id

            # Try Party table by display or English name.
            from app.models.party import Party

            party = Party.query.filter(
                or_(
                    Party.name_en.ilike(name_value),
                    Party.name_display.ilike(name_value),
                )
            ).first()
            if party and party.party_id:
                logger.debug(f"Resolved {name_value} to party_id {party.party_id} via Party name")
                return party.party_id

    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"mgmt_deadlines.resolve_owner.custom_field_fallback(matter_id={matter_id})",
            log_key="mgmt_deadlines.resolve_owner.custom_field_fallback",
            log_window_seconds=300,
        )

    return None


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().upper()
    return s in _TRUTHY_TOKENS


class AssigneeResolver:
    """
    Caches assignee resolution within a single request/operation.
    Avoids repeated DB lookups for the same assignee value.
    """

    def __init__(self):
        self._cache: dict[str, str | None] = {}

    def resolve(self, assignee_value: str | None) -> str | None:
        """Resolve assignee value to staff_party_id with caching."""
        raw = (assignee_value or "").strip()
        if not raw:
            return None

        if raw in self._cache:
            return self._cache[raw]

        result = self._do_resolve(raw)
        self._cache[raw] = result
        return result

    def _do_resolve(self, raw: str) -> str | None:
        """Actually perform the resolution (no caching)."""
        # Allow multiple values separated by ';' or ',' and pick the first.
        for sep in (";", ","):
            if sep in raw:
                raw = raw.split(sep)[0].strip()
                break

        username = raw
        email = ""
        if "(" in raw and ")" in raw:
            try:
                username = raw.split("(", 1)[0].strip() or raw
                email = raw.split("(", 1)[1].split(")", 1)[0].strip().lower()
            except Exception:
                username = raw
                email = ""

        user = None
        try:
            if email:
                user = User.query.filter(User.email.ilike(email)).first()
            if not user and username:
                user = User.query.filter_by(username=username).first()
            if not user and username and "@" in username:
                user = User.query.filter(User.email.ilike(username.lower())).first()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.assignee_resolver.user_lookup",
                log_key="mgmt_deadlines.assignee_resolver.user_lookup",
                log_window_seconds=300,
            )
            user = None

        try:
            if user and (user.staff_party_id or "").strip():
                party_id = (user.staff_party_id or "").strip()
                ps = None
                try:
                    ps = db.session.get(PartyStaff, party_id)
                except Exception:
                    ps = PartyStaff.query.filter_by(party_id=party_id).first()
                if ps and (ps.active in (None, 1)):
                    return party_id
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.assignee_resolver.staff_party_id",
                log_key="mgmt_deadlines.assignee_resolver.staff_party_id",
                log_window_seconds=300,
            )

        # Fallback: treat value as staff_code and map to party_id.
        try:
            ps = (
                PartyStaff.query.filter(PartyStaff.staff_code.ilike(username))
                .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
                .first()
            )
            if ps and (ps.party_id or "").strip():
                return (ps.party_id or "").strip()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.assignee_resolver.party_staff",
                log_key="mgmt_deadlines.assignee_resolver.party_staff",
                log_window_seconds=300,
            )

        return None


def _resolve_assignee_value(custom_data: dict, field: str | None) -> str | None:
    """Extract raw assignee value from custom_data."""
    if not field:
        return None
    try:
        raw = (custom_data.get(field) or "").strip()
    except Exception:
        raw = ""
    return raw or None


def _validate_template(t: dict) -> tuple[bool, str]:
    """
    Validate a template configuration.
    Returns (is_valid, error_message).
    """
    tpl_id = t.get("id", "<no-id>")

    # Check required base fields
    trigger = (t.get("trigger") or "").strip()
    if not trigger:
        return False, f"Template '{tpl_id}': missing 'trigger' field"

    if trigger not in _TRIGGER_REQUIRED_FIELDS:
        return False, f"Template '{tpl_id}': unknown trigger type '{trigger}'"

    # Check trigger-specific required fields
    for field in _TRIGGER_REQUIRED_FIELDS.get(trigger, []):
        if not (t.get(field) or "").strip():
            return (
                False,
                f"Template '{tpl_id}': missing required field '{field}' for trigger '{trigger}'",
            )

    # Validate offset fields are integers
    for offset_field in (
        "offset_days",
        "offset_months",
        "offset_years",
        "visible_offset_days",
        "visible_offset_months",
        "visible_offset_years",
    ):
        val = t.get(offset_field)
        if val is not None:
            try:
                int(val)
            except (ValueError, TypeError):
                return (
                    False,
                    f"Template '{tpl_id}': '{offset_field}' must be an integer, got '{val}'",
                )

    return True, ""


def _coerce_optional_int(value) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return int(raw)


def _apply_template_defaults(t: dict) -> dict:
    if not isinstance(t, dict):
        return t
    tpl_id = (t.get("id") or "").strip()
    patch = _TEMPLATE_DEFAULT_PATCHES.get(tpl_id)
    if not patch:
        return t
    has_visibility_offset = any(
        _coerce_optional_int(t.get(key)) is not None
        for key in ("visible_offset_days", "visible_offset_months", "visible_offset_years")
    )
    if has_visibility_offset:
        return t
    return {**t, **patch}


def _apply_template_collection_defaults(templates: list[dict]) -> list[dict]:
    if not isinstance(templates, list):
        return list(_DEFAULT_TEMPLATES)
    out = [_apply_template_defaults(t) for t in templates if isinstance(t, dict)]
    existing_ids = {(t.get("id") or "").strip() for t in out if isinstance(t, dict)}
    for default_template in _DEFAULT_TEMPLATES:
        tpl_id = (default_template.get("id") or "").strip()
        if tpl_id in _TEMPLATE_DEFAULT_ADDITION_IDS and tpl_id not in existing_ids:
            out.append(copy.deepcopy(default_template))
            existing_ids.add(tpl_id)
    return out


def _normalize_template(t: dict) -> dict:
    """Normalize template values to expected types."""
    return {
        **t,
        "id": (t.get("id") or "").strip(),
        "trigger": (t.get("trigger") or "").strip(),
        "category": (t.get("category") or "MGMT").strip() or "MGMT",
        "title": (t.get("title") or "").strip(),
        "offset_days": int(t.get("offset_days") or 0),
        "offset_months": int(t.get("offset_months") or 0),
        "offset_years": int(t.get("offset_years") or 0),
        "visible_offset_days": _coerce_optional_int(t.get("visible_offset_days")),
        "visible_offset_months": _coerce_optional_int(t.get("visible_offset_months")),
        "visible_offset_years": _coerce_optional_int(t.get("visible_offset_years")),
    }


def _load_templates() -> list[dict]:
    """Load and validate templates from system config."""
    templates_to_process = ConfigService.get_json("MGMT_TEMPLATES_JSON", None)
    if not templates_to_process:
        templates_to_process = list(_DEFAULT_TEMPLATES)
    elif isinstance(templates_to_process, list):
        templates_to_process = _apply_template_collection_defaults(templates_to_process)
        if not templates_to_process:
            templates_to_process = list(_DEFAULT_TEMPLATES)
    else:
        templates_to_process = list(_DEFAULT_TEMPLATES)

    # Validate and normalize each template
    valid_templates = []
    for t in templates_to_process:
        is_valid, error_msg = _validate_template(t)
        if not is_valid:
            logger.warning(f"Skipping invalid template: {error_msg}")
            continue
        valid_templates.append(_normalize_template(t))

    return valid_templates


def ensure_templates_seeded() -> None:
    """Ensure default templates are seeded in system config."""
    if SystemConfig.query.filter_by(key="MGMT_TEMPLATES_JSON").first():
        return
    try:
        SystemConfig.set_config(
            "MGMT_TEMPLATES_JSON", json.dumps(_DEFAULT_TEMPLATES, ensure_ascii=False)
        )
        logger.info("Seeded default MGMT_TEMPLATES_JSON")
    except Exception as e:
        logger.error(f"Failed to seed MGMT_TEMPLATES_JSON: {e}")
        db.session.rollback()


def _coerce_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _compact_name_ref(value: str | None) -> str:
    return _WS_RE.sub("", (value or "").strip())


def _normalize_match_regexes(regexes: list[str], *, policy_id: str) -> list[str]:
    normalized: list[str] = []
    for raw in regexes:
        rx = str(raw or "").strip()
        if not rx:
            continue
        try:
            re.compile(rx)
        except re.error as exc:
            logger.warning(
                "Ignoring invalid deadline policy regex in %s: %s (%s)",
                policy_id,
                rx,
                exc,
            )
            continue
        normalized.append(rx)
    return normalized


def _any_regex_matches(name: str, regexes: list[str], *, policy_id: str) -> bool:
    for raw in regexes:
        rx = str(raw or "").strip()
        if not rx:
            continue
        try:
            if re.search(rx, name):
                return True
        except re.error as exc:
            logger.warning(
                "Skipping invalid deadline policy regex at match time in %s: %s (%s)",
                policy_id,
                rx,
                exc,
            )
            continue
    return False


def _normalize_followup_template(t: dict) -> dict | None:
    if not isinstance(t, dict):
        return None
    tpl_id = (t.get("id") or "").strip()
    title = (t.get("title") or "").strip()
    if not tpl_id or not title:
        return None
    if tpl_id.upper() in _DISABLED_FOLLOWUP_TEMPLATE_IDS:
        return None
    return {
        **t,
        "id": tpl_id,
        "title": title,
        "category": (t.get("category") or "MGMT").strip() or "MGMT",
        "assignee_field": (t.get("assignee_field") or "").strip() or None,
        "offset_days": int(t.get("offset_days") or 0),
        "offset_months": int(t.get("offset_months") or 0),
        "offset_years": int(t.get("offset_years") or 0),
    }


def _normalize_deadline_policy(p: dict) -> dict | None:
    if not isinstance(p, dict):
        return None
    policy_id = (p.get("id") or "").strip()
    if not policy_id:
        return None
    match = p.get("match") if isinstance(p.get("match"), dict) else {}
    match = dict(match or {})
    match_deadline_codes = _coerce_list(
        match.get("deadline_codes") or p.get("deadline_codes") or p.get("deadline_code")
    )
    match["deadline_codes"] = match_deadline_codes
    match["template_ids"] = _coerce_list(match.get("template_ids"))
    match["name_ref_prefixes"] = _coerce_list(match.get("name_ref_prefixes"))
    match["name_ref_equals"] = _coerce_list(match.get("name_ref_equals"))
    match["name_ref_contains"] = _coerce_list(match.get("name_ref_contains"))
    match["name_ref_regexes"] = _normalize_match_regexes(
        _coerce_list(match.get("name_ref_regexes")),
        policy_id=policy_id,
    )
    has_match = any(
        match.get(key)
        for key in (
            "deadline_codes",
            "template_ids",
            "name_ref_prefixes",
            "name_ref_equals",
            "name_ref_contains",
            "name_ref_regexes",
        )
    )
    if not has_match:
        match = {}

    followups = []
    for tpl in p.get("followup_templates") or []:
        normalized = _normalize_followup_template(tpl)
        if normalized:
            followups.append(normalized)
    post_due_policy = (p.get("post_due_policy") or "").strip().upper() or None
    if post_due_policy == "AUTO_EXPIRE_WITH_FOLLOWUP" and not followups:
        post_due_policy = "AUTO_EXPIRE"

    return {
        **p,
        "id": policy_id,
        "match": match,
        "impact_scope": (p.get("impact_scope") or "").strip().upper() or None,
        "action_target": (p.get("action_target") or "").strip().upper() or None,
        "remedyability": (p.get("remedyability") or "").strip().upper() or None,
        "deadline_shape": (p.get("deadline_shape") or "").strip().upper() or None,
        "decision_ownership": (p.get("decision_ownership") or "").strip().upper() or None,
        "post_due_policy": post_due_policy,
        "effective_due_basis": (p.get("effective_due_basis") or "extended_or_due").strip().lower(),
        "expire_after_days": int(p.get("expire_after_days") or 0),
        "close_mark": (p.get("close_mark") or "").strip().upper() or None,
        "followup_templates": followups,
        "lockable": bool(p.get("lockable", True)),
    }


def _policy_has_deadline_code_or_prefix(
    policy: dict,
    *,
    target_policy_id: str,
    code: str,
    prefix: str,
) -> bool:
    if not isinstance(policy, dict):
        return False
    policy_id = (policy.get("id") or "").strip()
    if policy_id and target_policy_id and policy_id == target_policy_id:
        return True

    policy_codes = set(_coerce_list(policy.get("deadline_codes") or policy.get("deadline_code")))
    match = policy.get("match") if isinstance(policy.get("match"), dict) else {}
    policy_codes.update(_coerce_list((match or {}).get("deadline_codes")))
    if code and code in policy_codes:
        return True

    prefix_compact = _compact_name_ref(prefix)
    if not prefix_compact:
        return False
    policy_prefixes = [
        _compact_name_ref(pfx)
        for pfx in _coerce_list((match or {}).get("name_ref_prefixes"))
        if _compact_name_ref(pfx)
    ]
    return prefix_compact in set(policy_prefixes)


def _apply_policy_defaults(policies: list[dict]) -> list[dict]:
    if not isinstance(policies, list):
        return list(_DEFAULT_DEADLINE_POLICIES)
    out = [p for p in policies if isinstance(p, dict)]
    out = [
        {**p, **_POLICY_RUNTIME_FIELD_PATCHES.get((p.get("id") or "").strip(), {})}
        for p in out
    ]
    for default_policy in _POLICY_DEFAULT_PATCHES:
        target_policy_id = (default_policy.get("id") or "").strip()
        codes = _coerce_list(default_policy.get("deadline_codes"))
        prefixes = _coerce_list((default_policy.get("match") or {}).get("name_ref_prefixes"))
        code = codes[0] if codes else ""
        prefix = prefixes[0] if prefixes else ""
        exists = any(
            _policy_has_deadline_code_or_prefix(
                p,
                target_policy_id=target_policy_id,
                code=code,
                prefix=prefix,
            )
            for p in out
        )
        if not exists:
            out.append(copy.deepcopy(default_policy))
    return out


def _load_deadline_policies() -> list[dict]:
    policies_to_process = ConfigService.get_json(_POLICY_CONFIG_KEY, None)
    if not policies_to_process:
        policies_to_process = list(_DEFAULT_DEADLINE_POLICIES)
    elif isinstance(policies_to_process, list):
        policies_to_process = _apply_policy_defaults(policies_to_process)
        if not policies_to_process:
            policies_to_process = list(_DEFAULT_DEADLINE_POLICIES)
    else:
        policies_to_process = list(_DEFAULT_DEADLINE_POLICIES)

    normalized = []
    for p in policies_to_process:
        np = _normalize_deadline_policy(p)
        if np:
            normalized.append(np)

    if not normalized:
        normalized = [
            p for p in (_normalize_deadline_policy(x) for x in _DEFAULT_DEADLINE_POLICIES) if p
        ]
    return normalized


def ensure_deadline_policies_seeded() -> None:
    if SystemConfig.query.filter_by(key=_POLICY_CONFIG_KEY).first():
        return
    try:
        SystemConfig.set_config(
            _POLICY_CONFIG_KEY,
            json.dumps(_DEFAULT_DEADLINE_POLICIES, ensure_ascii=False),
        )
        logger.info("Seeded %s", _POLICY_CONFIG_KEY)
    except Exception as e:
        logger.error(f"Failed to seed {_POLICY_CONFIG_KEY}: {e}")
        db.session.rollback()


def _policy_matches(
    policy: dict,
    *,
    deadline_code: str | None,
    name_ref: str | None,
    template_id: str | None,
) -> bool:
    match = policy.get("match") or {}
    code = (deadline_code or "").strip()
    name = (name_ref or "").strip()
    name_compact = _compact_name_ref(name)
    tpl = (template_id or "").strip()
    policy_id = (policy.get("id") or "<unknown>").strip() or "<unknown>"

    if match:
        match_codes = _coerce_list(match.get("deadline_codes"))
        if match_codes and code not in match_codes:
            return False
        match_tpls = _coerce_list(match.get("template_ids"))
        if match_tpls and tpl not in match_tpls:
            return False
        match_equals = _coerce_list(match.get("name_ref_equals"))
        if match_equals and name not in match_equals:
            return False
        prefixes = _coerce_list(match.get("name_ref_prefixes"))
        if prefixes:
            compact_prefixes = [
                _compact_name_ref(pfx) for pfx in prefixes if _compact_name_ref(pfx)
            ]
            if compact_prefixes and not any(
                name_compact.startswith(pfx) for pfx in compact_prefixes
            ):
                return False
        contains = _coerce_list(match.get("name_ref_contains"))
        if contains:
            compact_contains = [
                _compact_name_ref(seg) for seg in contains if _compact_name_ref(seg)
            ]
            if compact_contains and not any(seg in name_compact for seg in compact_contains):
                return False
        regexes = _coerce_list(match.get("name_ref_regexes"))
        if regexes and not _any_regex_matches(name, regexes, policy_id=policy_id):
            return False
        return True

    codes = _coerce_list(policy.get("deadline_codes") or policy.get("deadline_code"))
    if codes:
        return code in codes
    return False


def _resolve_policy_for_item(
    *,
    memo_data: dict,
    name_ref: str | None,
    policies: list[dict],
) -> dict | None:
    policy_id = (memo_data.get("policy_id") or memo_data.get("policy") or "").strip()
    if policy_id:
        by_id = {p.get("id"): p for p in policies if p.get("id")}
        if policy_id in by_id:
            return by_id[policy_id]

    deadline_code = (memo_data.get("deadline_code") or "").strip()
    template_id = (memo_data.get("template_id") or "").strip()
    for policy in policies:
        if _policy_matches(
            policy,
            deadline_code=deadline_code,
            name_ref=name_ref,
            template_id=template_id,
        ):
            return policy
    return None


def _resolve_policy_id_for_metadata(
    *,
    deadline_code: str | None,
    name_ref: str | None,
    template_id: str | None,
    policies: list[dict],
) -> str | None:
    memo_data = {
        "deadline_code": (deadline_code or "").strip(),
        "template_id": (template_id or "").strip(),
    }
    policy = _resolve_policy_for_item(
        memo_data=memo_data,
        name_ref=name_ref,
        policies=policies,
    )
    return (policy or {}).get("id")


def _policy_name_ref_matches(policy: dict, name_ref: str | None) -> bool:
    name = (name_ref or "").strip()
    if not name:
        return False
    name_compact = _compact_name_ref(name)
    match = policy.get("match") if isinstance(policy.get("match"), dict) else {}
    if not match:
        return False
    prefixes = _coerce_list(match.get("name_ref_prefixes"))
    equals = _coerce_list(match.get("name_ref_equals"))
    contains = _coerce_list(match.get("name_ref_contains"))
    regexes = _coerce_list(match.get("name_ref_regexes"))
    if not any((prefixes, equals, contains, regexes)):
        return False
    if prefixes:
        compact_prefixes = [_compact_name_ref(pfx) for pfx in prefixes if _compact_name_ref(pfx)]
        if compact_prefixes and not any(name_compact.startswith(pfx) for pfx in compact_prefixes):
            return False
    if equals and name not in equals:
        return False
    if contains:
        compact_contains = [_compact_name_ref(seg) for seg in contains if _compact_name_ref(seg)]
        if compact_contains and not any(seg in name_compact for seg in compact_contains):
            return False
    policy_id = (policy.get("id") or "<unknown>").strip() or "<unknown>"
    if regexes and not _any_regex_matches(name, regexes, policy_id=policy_id):
        return False
    return True


def _item_label(item: DocketItem) -> str:
    for attr in ("name_free", "name_ref", "name", "title"):
        val = getattr(item, attr, None)
        if val:
            return str(val).strip()
    return ""


_FOREIGN_FILING_STATUS_RED_REF_COMPACT = _compact_name_ref("MGMT:STATUS_RED:ForeignFilingDeadline").upper()
_PCT_NATIONAL_PHASE_STATUS_RED_REF_COMPACT = _compact_name_ref(
    "MGMT:STATUS_RED:PCTDomesticDeadline"
).upper()
_REGISTRATION_STATUS_RED_REF_COMPACTS = tuple(
    _compact_name_ref(value).upper()
    for value in (
        "MGMT:STATUS_RED:RegistrationDeadline",
        "MGMT:STATUS_RED:RegistrationDue date",
        "MGMT:STATUS_RED:RegistrationDeadline",
        "MGMT:STATUS_RED:RegistrationDue date",
    )
)


def _is_foreign_filing_status_red_ref(name_ref: str | None) -> bool:
    compact_upper = _compact_name_ref(name_ref).upper()
    if not compact_upper:
        return False
    return compact_upper.startswith(_FOREIGN_FILING_STATUS_RED_REF_COMPACT)


def _is_foreign_filing_mgmt_ref(name_ref: str | None) -> bool:
    raw = (name_ref or "").strip()
    if not raw:
        return False
    raw_upper = raw.upper()
    if raw_upper == "MGMT:FOREIGN_FILING_NOTICE_3M":
        return True
    if raw_upper.startswith("MGMT:FOREIGN_FILING"):
        return True
    return _is_foreign_filing_status_red_ref(raw)


def _is_pct_national_phase_status_red_ref(name_ref: str | None) -> bool:
    compact_upper = _compact_name_ref(name_ref).upper()
    if not compact_upper:
        return False
    return compact_upper.startswith(_PCT_NATIONAL_PHASE_STATUS_RED_REF_COMPACT)


def _is_registration_status_red_ref(name_ref: str | None) -> bool:
    compact_upper = _compact_name_ref(name_ref).upper()
    if not compact_upper:
        return False
    return any(
        compact_upper.startswith(prefix)
        for prefix in _REGISTRATION_STATUS_RED_REF_COMPACTS
        if prefix
    )


def _is_mixed_status_red_ref(name_ref: str | None) -> bool:
    return (
        _is_foreign_filing_status_red_ref(name_ref)
        or _is_pct_national_phase_status_red_ref(name_ref)
        or _is_registration_status_red_ref(name_ref)
    )


_OPEN_SOURCE_STATUS_RED_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "ForeignFilingDeadline": {
        "deadline_codes": ("FOREIGN_FILING_PARIS",),
        "deadline_custom_keys": ("foreign_filing_deadline",),
        "done_custom_keys": ("foreign_filing_date",),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("FOREIGN_FILING_DEADLINE", "ForeignFilingDeadline"),
        "done_event_keys": ("FOREIGN_FILING_DATE", "ForeignFiling date"),
    },
    "Examination requestDeadline": {
        "deadline_codes": ("REQUEST_EXAMINATION",),
        "deadline_custom_keys": ("exam_deadline", "exam_request_deadline"),
        "done_custom_keys": ("exam_request_date",),
        "done_truthy_custom_keys": ("exam_requested",),
        "deadline_event_keys": ("EXAM_REQUEST_DEADLINE", "Examination request Due date"),
        "done_event_keys": ("EXAM_REQUEST_DATE", "EXAM_REQUESTED", "Examination request date"),
    },
    "PCTDomesticDeadline": {
        "deadline_codes": ("PCT_NATIONAL_PHASE",),
        "deadline_custom_keys": ("national_phase_deadline",),
        "done_custom_keys": ("national_phase_last_entry_date",),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("PCT_NATIONAL_PHASE_DEADLINE", "NATIONAL_PHASE_DEADLINE"),
        "done_event_keys": ("NATIONAL_PHASE_ENTRY_DATE", "NATIONAL_PHASE_LAST_ENTRY_DATE"),
    },
    "RegistrationDeadline": {
        "deadline_codes": ("REGISTRATION_DEADLINE",),
        "deadline_custom_keys": ("reg_deadline",),
        "done_custom_keys": ("registration_date", "reg_extension_date", "reg_fee_paid_date"),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("REGISTRATION_DEADLINE", "RegistrationDue date"),
        "done_event_keys": (
            "REGISTRATION_DATE",
            "REGISTRATION_FEE_PAID",
            "Registration date",
            "RegistrationPeriod",
        ),
    },
    "RegistrationDeadline": {
        "deadline_codes": ("PENALTY_REG_DEADLINE",),
        "deadline_custom_keys": ("reg_penalty_deadline",),
        "done_custom_keys": ("registration_date", "reg_extension_date", "reg_fee_paid_date"),
        "done_truthy_custom_keys": (),
        "deadline_event_keys": ("PENALTY_REG_DEADLINE", "RegistrationDue date"),
        "done_event_keys": (
            "REGISTRATION_DATE",
            "REGISTRATION_FEE_PAID",
            "Registration date",
            "RegistrationPeriod",
        ),
    },
}
_OPEN_SOURCE_STATUS_RED_ALIASES = {
    _compact_name_ref(label): label for label in _OPEN_SOURCE_STATUS_RED_SPECS
}
_OPEN_SOURCE_STATUS_RED_ALIASES[_compact_name_ref("RegistrationDue date")] = "RegistrationDeadline"
_OPEN_SOURCE_STATUS_RED_ALIASES[_compact_name_ref("RegistrationDue date")] = "RegistrationDeadline"


def _status_red_label_from_item(item: DocketItem) -> str:
    name_ref = (getattr(item, "name_ref", None) or "").strip()
    if name_ref.upper().startswith("MGMT:STATUS_RED:"):
        label = name_ref.split(":", 2)[-1].strip()
    else:
        label = _item_label(item)
    return _OPEN_SOURCE_STATUS_RED_ALIASES.get(_compact_name_ref(label), label.strip())


def _dates_for_matter_event_keys(matter_id: str, keys: tuple[str, ...]) -> set[date]:
    wanted = tuple(k for k in keys if k)
    if not matter_id or not wanted:
        return set()
    dates: set[date] = set()
    try:
        rows = (
            MatterEvent.query.filter(MatterEvent.matter_id == str(matter_id))
            .filter(MatterEvent.event_key.in_(wanted))
            .all()
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"mgmt_deadlines.matter_event_dates(matter_id={matter_id})",
            log_key="mgmt_deadlines.matter_event_dates",
            log_window_seconds=300,
        )
        return dates

    for row in rows:
        parsed = _parse_date(getattr(row, "event_date", None)) or _parse_date(
            getattr(row, "event_at", None)
        )
        if parsed:
            dates.add(parsed)
    return dates


def _custom_date_for_keys(custom_data: dict, keys: tuple[str, ...]) -> date | None:
    data = custom_data if isinstance(custom_data, dict) else {}
    for key in keys:
        parsed = _parse_date(data.get(key))
        if parsed:
            return parsed
    return None


def _custom_truthy_for_keys(custom_data: dict, keys: tuple[str, ...]) -> bool:
    data = custom_data if isinstance(custom_data, dict) else {}
    for key in keys:
        if _is_truthy(data.get(key)):
            return True
    return False


def _matter_has_terminal_signal(*, matter_id: str, custom_data: dict) -> bool:
    if _custom_date_for_keys(custom_data, ("complete_date", "abandon_date")):
        return True
    if _dates_for_matter_event_keys(
        matter_id,
        ("Done/Closed", "complete_date", "Abandoned/Withdrawn", "abandon_date"),
    ):
        return True

    matter = db.session.get(Matter, str(matter_id))
    if matter is None:
        return False
    try:
        from app.services.case.terminal_status import is_terminal_case_status
    except Exception:
        return False
    return any(
        is_terminal_case_status(value)
        for value in (
            getattr(matter, "status_blue", None),
            getattr(matter, "inhouse_status", None),
            getattr(matter, "status_red", None),
        )
    )


def _current_status_red_due_matches(*, matter_id: str, label: str, due: date) -> bool:
    matter = db.session.get(Matter, str(matter_id))
    if matter is None:
        return False
    current_label = _OPEN_SOURCE_STATUS_RED_ALIASES.get(
        _compact_name_ref(getattr(matter, "status_red", None)),
        (getattr(matter, "status_red", None) or "").strip(),
    )
    if current_label != label:
        return False
    return _parse_date(getattr(matter, "status_red_related_date", None)) == due


def _engine_deadline_matches(
    *, matter_id: str, deadline_code: str, due: date, custom_data: dict
) -> bool:
    matter = db.session.get(Matter, str(matter_id))
    if matter is None:
        return False
    try:
        deadlines_by_code = _compute_engine_deadlines(
            matter_id=str(matter_id),
            our_ref=getattr(matter, "our_ref", None),
            custom_data=custom_data,
            right_group=getattr(matter, "right_group", None),
            matter_type=getattr(matter, "matter_type", None),
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=f"mgmt_deadlines.engine_deadline_matches(matter_id={matter_id})",
            log_key="mgmt_deadlines.engine_deadline_matches",
            log_window_seconds=300,
        )
        return False
    return due in (deadlines_by_code.get(deadline_code) or [])


def _core_status_red_source_still_open(
    *, item: DocketItem, custom_data: dict | None = None
) -> bool:
    if memo_has_manual_abandon_lock(getattr(item, "memo", None)):
        return False

    label = _status_red_label_from_item(item)
    spec = _OPEN_SOURCE_STATUS_RED_SPECS.get(label)
    if not spec:
        return False

    matter_id = str(getattr(item, "matter_id", "") or "").strip()
    due = _parse_date(getattr(item, "due_date", None)) or _parse_date(
        getattr(item, "extended_due_date", None)
    )
    if not matter_id or due is None or due < date.today():
        return False

    data = custom_data if custom_data is not None else _merge_custom_fields(matter_id)
    if _matter_has_terminal_signal(matter_id=matter_id, custom_data=data):
        return False

    if label == "ForeignFilingDeadline":
        matter = db.session.get(Matter, str(matter_id))
        if _is_foreign_filing_priority_excluded(matter=matter, custom_data=data):
            return False
        if _foreign_filing_priority_done_signal_date(matter=matter, custom_data=data):
            return False

    if _custom_truthy_for_keys(data, spec["done_truthy_custom_keys"]):
        return False
    if _custom_date_for_keys(data, spec["done_custom_keys"]):
        return False
    if _dates_for_matter_event_keys(matter_id, spec["done_event_keys"]):
        return False

    if _custom_date_for_keys(data, spec["deadline_custom_keys"]) == due:
        return True
    if due in _dates_for_matter_event_keys(matter_id, spec["deadline_event_keys"]):
        return True
    if _current_status_red_due_matches(matter_id=matter_id, label=label, due=due):
        return True

    deadline_code = (_parse_memo(getattr(item, "memo", None)).get("deadline_code") or "").strip()
    if not deadline_code or deadline_code not in spec["deadline_codes"]:
        return False
    if deadline_code == "PCT_NATIONAL_PHASE":
        return True
    return _engine_deadline_matches(
        matter_id=matter_id,
        deadline_code=deadline_code,
        due=due,
        custom_data=data,
    )


def _is_pct_matter(*, matter_type: str | None, our_ref: str | None) -> bool:
    type_compact = _WS_RE.sub("", (matter_type or "").strip())
    type_upper = type_compact.upper()
    if "PCT" in type_upper or "Filing" in type_compact:
        return True
    ref_upper = (our_ref or "").strip().upper()
    return "PCT" in ref_upper


_PCT_ADVISORY_STATUS_RED_LABELS = frozenset(
    {
        "PCTPreliminary examinationDeadline",
        "Domestic Deadline 1  Notice",
        "DomesticDeadline19itemsDeadline",
    }
)
_PCT_ADVISORY_STATUS_RED_COMPACTS = frozenset(
    _WS_RE.sub("", label) for label in _PCT_ADVISORY_STATUS_RED_LABELS
)


def _is_pct_advisory_status_red_label(value: str | None) -> bool:
    compact = _WS_RE.sub("", (value or "").strip())
    if not compact:
        return False
    return compact in _PCT_ADVISORY_STATUS_RED_COMPACTS


def _normalize_case_division(value: str | None) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    if upper in {"DOM", "INC", "OUT"}:
        return upper
    if upper in {"DOMESTIC"}:
        return "DOM"
    if upper in {"INCOMING", "INBOUND"}:
        return "INC"
    if upper in {"OUTGOING", "OUTBOUND", "FOREIGN"}:
        return "OUT"
    if raw in {"Domestic", ""}:
        return "DOM"
    if raw in {"", "Matter", ""}:
        return "INC"
    if raw in {"Foreign", "Foreign", "", ""}:
        return "OUT"
    return ""


def _infer_matter_profile(matter: Matter | None) -> tuple[str, str]:
    if matter is None:
        return "", ""

    division = _normalize_case_division(getattr(matter, "right_group", None))
    matter_type = (getattr(matter, "matter_type", None) or "").strip().upper()
    if matter_type in {"PATENT", "UTILITY", "DESIGN", "TRADEMARK", "PCT"}:
        return division, matter_type

    our_ref = (getattr(matter, "our_ref", None) or "").strip().upper()
    if len(our_ref) >= 4 and our_ref[:2].isdigit():
        code = our_ref[2:4]
        if code.startswith("P"):
            matter_type = "PATENT"
        elif code.startswith("U"):
            matter_type = "UTILITY"
        elif code.startswith("D"):
            matter_type = "DESIGN"
        elif code.startswith("T"):
            matter_type = "TRADEMARK"
    return division, matter_type


def _compact_route_token(value: object | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return re.sub(r"[\s._\-/()]+", "", raw).upper()


def _has_pct_national_phase_text_signal(value: object | None) -> bool:
    token = _compact_route_token(value)
    if not token:
        return False
    if "203" in token and "NATIONAL" in token:
        return True
    if "DOMESTIC" in token:
        return True
    if "NATIONALPHASE" in token:
        return True
    return False


_FOREIGN_FILING_PROTOCOL_ROUTE_TOKENS = frozenset(
    {
        "PCT",
        "PCTNP",
        "MADRID",
        "MADRIDSYSTEM",
        "HAGUE",
        "HAGUESYSTEM",
        "EPVALIDATION",
        "EUROPEANVALIDATION",
    }
)
_FOREIGN_FILING_PROTOCOL_ROUTE_MARKERS = (
    "PCT",
    "MADRID",
    "HAGUE",
    "EPVALIDATION",
    "EUROPEANVALIDATION",
)


def _has_foreign_filing_protocol_route_signal(value: object | None) -> bool:
    token = _compact_route_token(value)
    if not token:
        return False
    if token in _FOREIGN_FILING_PROTOCOL_ROUTE_TOKENS:
        return True
    return any(marker in token for marker in _FOREIGN_FILING_PROTOCOL_ROUTE_MARKERS)


def _is_incoming_pct_national_phase_matter(
    *, matter: Matter | None, custom_data: dict | None
) -> bool:
    """Return True for incoming US national-stage matters from a prior PCT.

    These are already 203 /national-phase filings, so Paris-style
    foreign filing priority management does not apply.
    """
    division, matter_type = _infer_matter_profile(matter)
    if division != "INC" or matter_type not in {"PATENT", "UTILITY"}:
        return False

    data = custom_data if isinstance(custom_data, dict) else {}
    route_token = _compact_route_token(
        data.get("app_route") or data.get("application_route") or data.get("filing_route")
    )
    if route_token in {"PCT", "PCTNP"}:
        return True
    if _has_pct_national_phase_text_signal(
        data.get("app_route") or data.get("application_route") or data.get("filing_route")
    ):
        return True

    for key in (
        "filing_type",
        "filing_kind",
        "app_type",
        "application_type",
        "application_kind",
        "doc_type",
        "document_type",
    ):
        if _has_pct_national_phase_text_signal(data.get(key)):
            return True

    return bool(
        str(data.get("pct_application_no") or "").strip()
        or str(data.get("pct_application_date") or "").strip()
        or str(data.get("international_filing_date") or "").strip()
    )


def _foreign_filing_priority_exclusion_reason(
    *, matter: Matter | None, custom_data: dict | None
) -> str:
    """Return a close reason when Paris-style foreign filing management is inapplicable."""
    data = custom_data if isinstance(custom_data, dict) else {}
    if _is_incoming_pct_national_phase_matter(matter=matter, custom_data=data):
        return "excluded_pct_national_phase"

    division, matter_type = _infer_matter_profile(matter)
    raw_matter_type = getattr(matter, "matter_type", None) if matter is not None else None
    our_ref = getattr(matter, "our_ref", None) if matter is not None else None
    if _is_pct_matter(matter_type=raw_matter_type or matter_type, our_ref=our_ref):
        return "excluded_pct_international_application"

    if _has_foreign_filing_protocol_route_signal(raw_matter_type):
        return "excluded_protocol_route"

    for key in (
        "app_route",
        "application_route",
        "filing_route",
        "filing_type",
        "filing_kind",
        "app_type",
        "application_type",
        "application_kind",
        "doc_type",
        "document_type",
    ):
        value = data.get(key)
        if _has_foreign_filing_protocol_route_signal(value):
            return "excluded_protocol_route"

    return ""


def _is_foreign_filing_priority_excluded(
    *, matter: Matter | None, custom_data: dict | None
) -> bool:
    return bool(_foreign_filing_priority_exclusion_reason(matter=matter, custom_data=custom_data))


def _foreign_filing_priority_done_signal_date(
    *, matter: Matter | None, custom_data: dict | None
) -> date | None:
    """Resolve the date that completes Paris-style foreign filing management."""
    data = custom_data if isinstance(custom_data, dict) else {}
    direct_done = _parse_date(data.get("foreign_filing_date") or data.get("ForeignFiling date"))
    if direct_done:
        return direct_done

    division, _matter_type = _infer_matter_profile(matter)
    if division in {"INC", "OUT"}:
        return _parse_date(data.get("application_date") or data.get("filing_date"))

    return None


_TERM_EXPIRY_LABEL_NORMALS = frozenset(
    {
        "Termexpired",
        "TermExpiry",
        "TermExpiration",
    }
)


def _normalize_term_expiry_label(label: object | None) -> str:
    try:
        text = str(label or "").strip()
    except Exception:
        return ""
    if not text:
        return ""
    return _WS_RE.sub("", text)


def _is_term_expiry_like_label(label: object | None) -> bool:
    return _normalize_term_expiry_label(label) in _TERM_EXPIRY_LABEL_NORMALS


def _is_renewal_managed_term_expiry_matter(matter: Matter | None) -> bool:
    if matter is None:
        return False
    if is_annuity_management_disabled_for_matter(str(getattr(matter, "matter_id", "") or "")):
        return False
    division, matter_type = _infer_matter_profile(matter)
    return division == "DOM" and matter_type in {"PATENT", "UTILITY", "DESIGN", "TRADEMARK"}


def _is_renewal_managed_term_expiry_status_red(
    *, matter: Matter | None, red_label: str | None
) -> bool:
    if not _is_term_expiry_like_label(red_label):
        return False
    return _is_renewal_managed_term_expiry_matter(matter)


def _close_renewal_managed_term_expiry_like_status_red_dockets(*, matter: Matter | None) -> int:
    if not _is_renewal_managed_term_expiry_matter(matter):
        return 0

    matter_id = str(getattr(matter, "matter_id", "") or "").strip()
    if not matter_id:
        return 0

    items = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == matter_id,
            DocketItem.name_ref.like("MGMT:STATUS_RED:%"),
        )
    ).all()
    if not items:
        return 0

    today = date.today()
    done_str = today.isoformat()
    canonical_label = "Term expired"
    canonical_name_ref = f"MGMT:STATUS_RED:{canonical_label}"
    count = 0
    for item in items:
        if (getattr(item, "done_date", None) or "").strip():
            continue
        memo_data = _parse_memo(getattr(item, "memo", None))

        name_ref = (getattr(item, "name_ref", None) or "").strip()
        ref_label = name_ref.split(":", 2)[-1] if name_ref else ""
        name_free = getattr(item, "name_free", None)
        if not (_is_term_expiry_like_label(ref_label) or _is_term_expiry_like_label(name_free)):
            continue
        if memo_data.get("locked") and not memo_data.get("auto"):
            continue

        if name_ref != canonical_name_ref:
            item.name_ref = canonical_name_ref
        if (str(name_free or "").strip()) != canonical_label:
            item.name_free = canonical_label

        item.memo = _merge_memo(
            item.memo,
            {
                "close_reason": "moved_to_renewal",
                "closed_at": today.isoformat(),
            },
        )
        item.done_date = done_str
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.close_term_expiry_like.enqueue_sync",
                log_key="mgmt_deadlines.close_term_expiry_like.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1

    return count


_COUNTRY_CODE_ALIASES: dict[str, str] = {
    "US": "US",
    "USA": "US",
    "UNITEDSTATES": "US",
    "UNITEDSTATESOFAMERICA": "US",
    "SG": "SG",
    "SINGAPORE": "SG",
}


def _normalize_country_code(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"[\s._\-]", "", raw).upper()
    if not compact:
        return ""
    if compact in _COUNTRY_CODE_ALIASES:
        return _COUNTRY_CODE_ALIASES[compact]
    if len(compact) == 2 and compact.isalpha():
        return compact
    return ""


def _extract_country_codes(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            for code in _extract_country_codes(item):
                if code and code not in seen:
                    seen.add(code)
                    out.append(code)
        return out

    raw = str(value or "").strip()
    if not raw:
        return []
    chunks = [raw]
    if _COUNTRY_SPLIT_RE.search(raw):
        chunks = [p.strip() for p in _COUNTRY_SPLIT_RE.split(raw) if p and p.strip()]

    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        token = chunk.strip("[](){}<>\"'")
        code = _normalize_country_code(token)
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _resolve_pct_jurisdiction_codes(
    *,
    custom_data: dict,
    right_group: str | None = None,
    matter_type: str | None = None,
) -> tuple[str, str | None]:
    """
    Resolve filing/designated country for PCT national phase deadline computation.

    Rules:
    - USPTO-managed PCT cases default to US national stage.
    - Foreign-email-managed OUT cases default to the non-US path when explicit country is missing.
    - Stored ETC/PCT reference rows also default to the safer non-US path when
      national-phase country is missing, because they can later branch into foreign jurisdictions.
    - When `national_phase_countries` has multiple entries and includes US, prefer US for due calculation.
    """
    data = custom_data if isinstance(custom_data, dict) else {}
    division = _normalize_case_division(right_group)
    normalized_type = str(matter_type or "").strip().upper()
    is_uspto_managed = (
        is_uspto_managed_case_kind(division, normalized_type)
        if normalized_type
        else division in {"DOM", "INC"}
    )

    app_country = _normalize_country_code(
        data.get("application_country") or data.get("country") or data.get("filing_country")
    )
    designated_codes = (
        _extract_country_codes(data.get("national_phase_countries"))
        or _extract_country_codes(data.get("designated_country"))
        or _extract_country_codes(data.get("national_phase_country"))
    )

    designated_country: str | None = None
    if designated_codes:
        designated_country = ("US" if "US" in designated_codes else designated_codes[0]) or None
    elif app_country:
        designated_country = app_country

    filing_country = app_country
    if not filing_country:
        pct_storage_without_country = normalized_type == "PCT" and division not in {"DOM", "INC"}
        if pct_storage_without_country or division == "OUT":
            filing_country = ""
        elif is_uspto_managed:
            filing_country = "US"
        else:
            filing_country = "US"

    return filing_country, designated_country


def _status_red_visibility_window(
    *,
    red_label: str | None,
    due_date: date | None,
    is_uspto_managed_case: bool = False,
) -> tuple[date | None, bool]:
    return resolve_status_red_visibility_window(
        red_label=red_label,
        due_date=due_date,
        is_uspto_managed_case=is_uspto_managed_case,
    )


def _status_red_done_signal_date(*, red_label: str | None, custom_data: dict) -> date | None:
    """Resolve domain-specific done signals for selected status-red labels."""
    compact = _compact_name_ref(red_label)
    if not compact:
        return None

    # Registration stages should close once registration date exists.
    if ("Notice of allowance" in compact) or ("Registration" in compact):
        return _parse_date(custom_data.get("registration_date") or custom_data.get("Registration date"))

    # Payment-extension approval style docs can close on extension completion.
    if ("RegistrationPeriod" in compact) or ("RegistrationPaymentPeriod" in compact):
        return _parse_date(
            custom_data.get("reg_extension_date") or custom_data.get("RegistrationPeriod")
        )

    if compact == _compact_name_ref("ExaminationOpen"):
        return _parse_date(
            custom_data.get("expedited_request_date") or custom_data.get("expedited_decision_date")
        )

    return None


def _infer_auto_memo_for_item(*, item: DocketItem, policies: list[dict]) -> dict | None:
    name_ref = (item.name_ref or "").strip()
    if not name_ref or not name_ref.startswith("MGMT:STATUS_RED:"):
        label = _item_label(item)
        if label and any(key in label for key in ("ForeignFilingDeadline", "ForeignFilingDeadline")):
            policy_id = _resolve_policy_id_for_metadata(
                deadline_code="FOREIGN_FILING_PARIS",
                name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
                template_id=None,
                policies=policies,
            )
            updates = {
                "auto": True,
                "trigger": "legacy_name_match",
                "deadline_code": "FOREIGN_FILING_PARIS",
            }
            if policy_id:
                updates["policy_id"] = policy_id
            return updates
        return None

    policy = None
    for candidate in policies:
        if _policy_name_ref_matches(candidate, name_ref):
            policy = candidate
            break
    if not policy:
        return None

    updates = {"auto": True, "trigger": "name_ref_infer"}
    policy_id = policy.get("id")
    if policy_id:
        updates["policy_id"] = policy_id
    codes = _coerce_list(policy.get("deadline_codes") or policy.get("deadline_code"))
    if codes:
        updates["deadline_code"] = codes[0]
    return updates


def _merge_memo(existing_memo: str | None, updates: dict) -> str:
    data = _parse_memo(existing_memo)
    if not data and existing_memo:
        data = {"legacy_memo": existing_memo}
    for k, v in updates.items():
        if v is not None:
            data[k] = v
    return json.dumps(data, ensure_ascii=False)


def _effective_due_for_policy(item: DocketItem, policy: dict) -> date | None:
    basis = (policy.get("effective_due_basis") or "extended_or_due").strip().lower()
    if basis == "extended_due_date":
        return _parse_date(item.extended_due_date)
    if basis == "due_date":
        return _parse_date(item.due_date)
    # Default: use the later of due vs extended (treat extended as legal only when it extends)
    return effective_due_for_legal(item.due_date, item.extended_due_date)


def _compute_engine_deadlines(
    *,
    matter_id: str | None = None,
    our_ref: str | None,
    custom_data: dict,
    right_group: str | None = None,
    matter_type: str | None = None,
) -> dict[str, list[date]]:
    """Compute deadlines using the DeadlineEngine if available.

    Args:
        matter_id: If provided, fetch events from office_action/matter_event tables
        our_ref: Case reference for right_type inference
        custom_data: Custom field data for date extraction
        matter_type: Stored case type, used when our_ref does not carry enough type signal

    Returns:
        Dict mapping DeadlineCode to list of computed due dates (sorted, deduped)
    """
    DeadlineEngine = None
    caseinfo_from_minimal = None
    try:
        from deadline_engine import DeadlineEngine, caseinfo_from_minimal
    except ImportError:
        # Best-effort fallback for installations that provide deadline_engine separately.
        try:
            from app.utils.vendor_paths import ensure_deadline_engine_path

            ensure_deadline_engine_path()

            from deadline_engine import DeadlineEngine, caseinfo_from_minimal
        except Exception:
            logger.debug("deadline_engine module not available")

    is_pct_case = _is_pct_matter(matter_type=matter_type, our_ref=our_ref)
    filing_date = _parse_date(custom_data.get("application_date") or custom_data.get("filing_date"))
    priority_date = _parse_date(custom_data.get("priority_date"))
    novelty_dt = _parse_date(
        custom_data.get("novelty_grace_date") or custom_data.get("novelty_disclosure_date")
    )
    pct_dt = _parse_date(
        custom_data.get("pct_application_date") or custom_data.get("international_filing_date")
    )
    if is_pct_case and pct_dt is None:
        # Legacy PCT rows often stored the international filing date as application_date.
        pct_dt = filing_date
    reg_dt = _parse_date(custom_data.get("registration_date"))
    filing_country, designated_country = _resolve_pct_jurisdiction_codes(
        custom_data=custom_data,
        right_group=right_group,
        matter_type="PCT" if is_pct_case else None,
    )
    engine_our_ref = our_ref
    if is_pct_case and "PCT" not in (engine_our_ref or "").upper():
        engine_our_ref = f"{(engine_our_ref or '').strip()}PCT" or "PCT"

    def _fallback_deadlines() -> dict[str, list[date]]:
        fallback: dict[str, list[date]] = {}
        if is_pct_case:
            basis = pct_dt or priority_date or filing_date
            if basis:
                fallback["PCT_NATIONAL_PHASE"] = [basis + relativedelta(months=30)]
        return fallback

    if DeadlineEngine is None or caseinfo_from_minimal is None:
        return _fallback_deadlines()

    # Build events from office_action/matter_event if matter_id provided
    events = []
    if matter_id:
        try:
            from app.services.automation.event_pipeline import build_events_for_matter

            events = build_events_for_matter(matter_id)
            if events:
                logger.debug(f"Built {len(events)} events for matter {matter_id}")
        except Exception as e:
            logger.warning(f"Failed to build events for {matter_id}: {e}")
            events = []

    try:
        ci = caseinfo_from_minimal(
            our_ref=engine_our_ref,
            filing_country=filing_country or "",
            designated_country=designated_country,
            filing_date=filing_date,
            priority_date=priority_date,
            novelty_disclosure_date=novelty_dt,
            international_filing_date=pct_dt,
            registration_date=reg_dt,
            meta={
                # Claim deferment only when explicitly enabled
                "claim_deferment_selected": _is_truthy(
                    custom_data.get("claim_deferment_selected")
                    or custom_data.get("claim_deferment")
                    or custom_data.get("claim_deferment_requested")
                ),
            },
        )
        engine = DeadlineEngine()
        ds = engine.compute_all(ci, events=events, strict=False)
    except Exception as e:
        logger.warning(f"DeadlineEngine computation failed: {e}")
        return _fallback_deadlines()

    out: dict[str, list[date]] = {}
    for d in ds or []:
        try:
            # DeadlineEngine uses Enum codes (DeadlineCode). Use `.value` so templates can match.
            code = str(getattr(d.code, "value", d.code))
            due = d.due
        except Exception:
            continue
        if code and isinstance(due, date):
            out.setdefault(code, []).append(due)
    for code, dates in list(out.items()):
        deduped = sorted({d for d in dates if isinstance(d, date)})
        out[code] = deduped
    if is_pct_case:
        # PCT international applications use the dedicated national-phase deadline path.
        # Paris-style foreign filing reminders are for domestic priority management.
        out.pop("FOREIGN_FILING_PARIS", None)
        if not out.get("PCT_NATIONAL_PHASE"):
            out.update(_fallback_deadlines())
    return out


def _pick_preferred_deadline(
    dates: list[date] | date | None, *, today: date | None = None
) -> date | None:
    """Pick the nearest upcoming deadline; fallback to latest past."""
    if not dates:
        return None
    # Robustness: some legacy/monkeypatched engine paths return a single `date`.
    if isinstance(dates, date):
        dates = [dates]
    today = today or date.today()
    future = [d for d in dates if d >= today]
    if future:
        return min(future)
    return max(dates)


def _resolve_template_base_due(
    *,
    template: dict,
    deadlines_by_code: dict[str, list[date]],
    custom_data: dict,
    matter: Matter | None = None,
) -> tuple[date | None, str | None]:
    """Resolve base due date for a deadline_code template with robust fallback."""
    code = (template.get("deadline_code") or "").strip()
    if not code:
        return None, None

    # Manual override should take precedence when present.
    for key in _TEMPLATE_DEADLINE_FALLBACK_FIELDS.get(code, ()):
        dt = _parse_date(custom_data.get(key))
        if dt:
            return dt, f"custom:{key}"

    base_due = _pick_preferred_deadline(deadlines_by_code.get(code) or [])
    if base_due:
        return base_due, "engine"

    red_label = _TEMPLATE_DEADLINE_STATUS_RED_FALLBACKS.get(code)
    if red_label and matter is not None:
        current_red = (getattr(matter, "status_red", None) or "").strip()
        if current_red == red_label:
            red_due = _parse_date(getattr(matter, "status_red_related_date", None))
            if red_due:
                return red_due, "matter_status_red_related_date"

    return None, None


def _upsert_docket_item(
    *,
    matter_id: str,
    name_ref: str,
    category: str,
    title: str,
    due: date,
    owner: str | None,
    memo: str | None,
    internal_due: date | None = None,
    visible_from: date | None = None,
    done: date | None = None,
    clear_owner: bool = False,
    clear_internal_due: bool = False,
    clear_visible_from: bool = False,
) -> DocketItem:
    """
    Upsert a DocketItem by (matter_id, name_ref).

    Args:
        clear_owner: If True and owner is None, clear the owner field
        clear_internal_due: If True and internal_due is None, clear extended_due_date

    Returns:
        The created or updated DocketItem
    """
    due_str = due.isoformat()
    internal_due_str = internal_due.isoformat() if isinstance(internal_due, date) else None
    visible_from_str = visible_from.isoformat() if isinstance(visible_from, date) else None
    done_str = done.isoformat() if isinstance(done, date) else None

    # Prefer an open row with the same due date, then latest open row, then latest done row.
    # This keeps upsert deterministic when legacy duplicates exist.
    candidates = (
        _active_docket_query(
            DocketItem.query.filter_by(matter_id=str(matter_id), name_ref=name_ref)
        )
        .order_by(DocketItem.docket_id.desc())
        .all()
    )
    open_candidates = [row for row in candidates if not (row.done_date or "").strip()]
    existing = None
    if open_candidates:
        existing = next(
            (row for row in open_candidates if _date_token(row.due_date) == due_str),
            open_candidates[0],
        )
    elif candidates:
        existing = candidates[0]

    # [FIX] Fallback owner resolution from MatterStaffAssignment if not provided
    if not owner and not clear_owner:
        # Use incoming category/name_ref to help resolve owner
        # If name_ref starts with MGMT:, this is a management task → resolve manager first
        is_mgmt_task = name_ref.upper().startswith("MGMT:") if name_ref else False
        category_type = (
            "MGMT"
            # NOTE:
            # - DocketItem.category="NOTICE" is used by some OA-like tasks that must default to WORK
            #   ownership (attorney/handler) when owner isn't explicitly provided.
            # - Treat only explicit MGMT/SLA/ADMIN (or MGMT:* refs) as management tasks here.
            if is_mgmt_task or (category or "").upper() in ("MGMT", "SLA", "ADMIN")
            else "WORK"
        )
        owner = _resolve_owner_from_matter_staff(matter_id, category_type=category_type)

    # [NEW] Determine actual category to save
    # Priority:
    #   1. Mixed status-red tasks (ForeignFilingDeadline/PCTDomesticDeadline/RegistrationDeadline column)
    #      are saved as MGMT_WORK
    #   2. Explicit mixed category request is preserved
    #   3. If name_ref starts with "MGMT:", default to "MGMT"
    #   4. Otherwise, use role-based category from owner's role
    from app.utils.task_classification import determine_category_by_staff_role

    category_upper = (category or "").strip().upper()
    if _is_mixed_status_red_ref(name_ref):
        save_category = "MGMT_WORK"
    elif category_upper in {"MGMT_WORK", "WORK_MGMT"}:
        save_category = "MGMT_WORK"
    elif name_ref and name_ref.upper().startswith("MGMT:"):
        save_category = "MGMT"
    else:
        save_category = determine_category_by_staff_role(matter_id, staff_party_id=owner)

    target = None
    if existing:
        # Check for manual lock - skip auto-updates for locked items
        existing_memo = _parse_memo(existing.memo)
        if existing_memo.get("locked") and not (existing.done_date or "").strip():
            logger.debug(f"Skipping locked item: {name_ref}")
            target = existing
        elif (
            (existing.done_date or "").strip()
            and done_str is None
            and memo_has_manual_abandon_lock(existing_memo)
        ):
            logger.debug(
                "Preserving manually abandoned docket item: %s (%s)",
                name_ref,
                getattr(existing, "docket_id", None),
            )
            target = existing
        else:
            changed = False

            # Reopen previously closed row when upserted without explicit done date.
            if (existing.done_date or "").strip() and done_str is None:
                existing.done_date = None
                changed = True

            # Apply role-based category
            if (existing.category or "").strip() != save_category:
                existing.category = save_category
                changed = True

            if _date_token(existing.due_date) != due_str:
                existing.due_date = due_str
                changed = True

            # Handle internal_due: can clear if clear_internal_due is True
            if internal_due_str is not None:
                if _date_token(existing.extended_due_date) != internal_due_str:
                    existing.extended_due_date = internal_due_str
                    changed = True
            elif clear_internal_due and existing.extended_due_date:
                existing.extended_due_date = None
                changed = True

            if visible_from_str is not None:
                if _date_token(existing.visible_from_date) != visible_from_str:
                    existing.visible_from_date = visible_from_str
                    changed = True
            elif clear_visible_from and existing.visible_from_date:
                existing.visible_from_date = None
                changed = True

            # Handle owner: can clear if clear_owner is True
            if owner:
                if (existing.owner_staff_party_id or "").strip() != owner:
                    existing.owner_staff_party_id = owner
                    if (existing.category or "").strip() != save_category:
                        existing.category = save_category
                    changed = True
            elif clear_owner and existing.owner_staff_party_id:
                existing.owner_staff_party_id = None
                changed = True

            if title and (existing.name_free or "") != title:
                existing.name_free = title
                changed = True

            if memo is not None and (existing.memo or "") != memo:
                existing.memo = memo
                changed = True

            if done_str is not None and _date_token(existing.done_date) != done_str:
                existing.done_date = done_str
                changed = True

            if changed:
                db.session.add(existing)
                enqueue_docket_sync_for_item(docket_item=existing)
            target = existing

    if not target:
        # Create new
        di = DocketItem(
            docket_id=uuid.uuid4().hex,
            matter_id=str(matter_id),
            category=save_category,
            name_ref=name_ref,
            name_free=title,
            due_date=due_str,
            extended_due_date=internal_due_str,
            visible_from_date=visible_from_str,
            done_date=done_str,
            owner_staff_party_id=owner,
            memo=memo,
        )
        db.session.add(di)
        enqueue_docket_sync_for_item(docket_item=di)
        target = di

    _cancel_duplicate_name_refs(
        matter_id=str(matter_id),
        name_ref=name_ref,
        keep_docket_id=target.docket_id,
    )

    return target


def _cancel_duplicate_name_refs(
    *,
    matter_id: str,
    name_ref: str,
    keep_docket_id: str,
) -> int:
    if not matter_id or not name_ref or not keep_docket_id:
        return 0

    duplicates = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == str(matter_id),
            DocketItem.name_ref == name_ref,
            DocketItem.docket_id != keep_docket_id,
        )
    ).all()
    if not duplicates:
        return 0

    today = date.today().isoformat()
    count = 0
    for item in duplicates:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo(item.memo)
        if memo_data.get("locked"):
            continue
        item.done_date = f"AUTO_CANCELLED:{today}"
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.cancel_duplicate.enqueue_sync",
                log_key="mgmt_deadlines.cancel_duplicate.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1

    return count


def _mark_done_by_name_ref(
    *,
    matter_id: str,
    name_ref: str,
    done_dt: date | None,
    close_reason: str | None = None,
    policy_id: str | None = None,
) -> int:
    if not matter_id or not name_ref:
        return 0
    items = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == str(matter_id),
            DocketItem.name_ref == name_ref,
        )
    ).all()
    if not items:
        return 0

    closed_at = done_dt or date.today()
    done_str = closed_at.isoformat()
    count = 0
    for item in items:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo(item.memo)
        if memo_data.get("locked"):
            continue
        if close_reason or policy_id:
            item.memo = _merge_memo(
                item.memo,
                {
                    "close_reason": close_reason,
                    "closed_at": closed_at.isoformat(),
                    "policy_id": policy_id,
                },
            )
        item.done_date = done_str
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.mark_done.enqueue_sync",
                log_key="mgmt_deadlines.mark_done.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1
    return count


def _mark_done_token_by_name_ref(
    *,
    matter_id: str,
    name_ref: str,
    done_value: str | None,
    close_reason: str | None = None,
    policy_id: str | None = None,
) -> int:
    if not matter_id or not name_ref:
        return 0
    normalized = normalize_done_date(done_value)
    if not normalized:
        return 0

    items = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == str(matter_id),
            DocketItem.name_ref == name_ref,
        )
    ).all()
    if not items:
        return 0

    closed_at = _parse_date(normalized) or date.today()
    count = 0
    for item in items:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo(item.memo)
        if memo_data.get("locked"):
            continue
        if close_reason or policy_id:
            item.memo = _merge_memo(
                item.memo,
                {
                    "close_reason": close_reason,
                    "closed_at": closed_at.isoformat(),
                    "policy_id": policy_id,
                },
            )
        item.done_date = normalized
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.mark_done_token.enqueue_sync",
                log_key="mgmt_deadlines.mark_done_token.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1
    return count


def _cancel_foreign_filing_priority_deadlines(
    *, matter_id: str, close_reason: str = "excluded_foreign_filing_priority"
) -> int:
    if not matter_id:
        return 0

    today_str = date.today().isoformat()
    items = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == str(matter_id),
            or_(
                DocketItem.name_ref == "MGMT:FOREIGN_FILING_NOTICE_3M",
                DocketItem.name_ref.ilike("MGMT:FOREIGN_FILING%"),
                DocketItem.name_ref.ilike("MGMT:STATUS_RED:%ForeignFiling%"),
                DocketItem.name_free.ilike("%ForeignFiling%"),
            ),
        )
    ).all()
    if not items:
        return 0

    count = 0
    for item in items:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo(item.memo)
        if memo_data.get("locked"):
            continue
        if not memo_data.get("auto"):
            continue

        name_ref = (getattr(item, "name_ref", None) or "").strip()
        label = _item_label(item)
        label_compact = _compact_name_ref(label)
        if not (
            _is_foreign_filing_mgmt_ref(name_ref)
            or "ForeignFilingDeadline" in label_compact
            or "ForeignFilingDeadline" in label_compact
            or "ForeignFilingGuidance" in label_compact
        ):
            continue

        item.done_date = f"AUTO_CANCELLED:{today_str}"
        item.memo = _merge_memo(
            item.memo,
            {
                "close_reason": close_reason,
                "closed_at": today_str,
            },
        )
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.cancel_foreign_filing_priority.enqueue_sync",
                log_key="mgmt_deadlines.cancel_foreign_filing_priority.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1

    return count


def _complete_foreign_filing_priority_deadlines(
    *, matter_id: str, done_dt: date | None, close_reason: str = "done"
) -> int:
    if not matter_id or done_dt is None:
        return 0

    done_str = done_dt.isoformat()
    items = _active_docket_query(
        DocketItem.query.filter(
            DocketItem.matter_id == str(matter_id),
            or_(
                DocketItem.name_ref == "MGMT:FOREIGN_FILING_NOTICE_3M",
                DocketItem.name_ref.ilike("MGMT:FOREIGN_FILING%"),
                DocketItem.name_ref.ilike("MGMT:STATUS_RED:%ForeignFiling%"),
                DocketItem.name_free.ilike("%ForeignFiling%"),
            ),
        )
    ).all()
    if not items:
        return 0

    count = 0
    for item in items:
        if (item.done_date or "").strip():
            continue
        memo_data = _parse_memo(item.memo)
        if memo_data.get("locked"):
            continue

        name_ref = (getattr(item, "name_ref", None) or "").strip()
        label = _item_label(item)
        label_compact = _compact_name_ref(label)
        is_known_foreign_ref = _is_foreign_filing_mgmt_ref(name_ref)
        is_foreign_label = (
            "ForeignFilingDeadline" in label_compact
            or "ForeignFilingDeadline" in label_compact
            or "ForeignFilingGuidance" in label_compact
        )
        if not (is_known_foreign_ref or is_foreign_label):
            continue
        if is_foreign_label and not is_known_foreign_ref and not memo_data.get("auto"):
            continue

        item.done_date = done_str
        item.memo = _merge_memo(
            item.memo,
            {
                "close_reason": close_reason,
                "closed_at": done_str,
            },
        )
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.complete_foreign_filing_priority.enqueue_sync",
                log_key="mgmt_deadlines.complete_foreign_filing_priority.enqueue_sync",
                log_window_seconds=300,
            )
        count += 1

    return count


def _cleanup_stale_auto_deadlines(*, matter_id: str, valid_name_refs: set[str]) -> int:
    """Cancel auto-generated deadlines that are no longer valid.

    Marks stale MGMT-prefixed docket items as AUTO_CANCELLED if they are:
    - Not in the valid_name_refs set
    - Not already done
    - Not manually locked

    Args:
        matter_id: The matter ID to clean up
        valid_name_refs: Set of name_refs that were just created/updated (still valid)

    Returns:
        Number of items cancelled
    """
    count = 0
    try:
        custom_data = _merge_custom_fields(str(matter_id))
        existing = _active_docket_query(
            DocketItem.query.filter(
                DocketItem.matter_id == str(matter_id),
                DocketItem.name_ref.like("MGMT:%"),
                DocketItem.category == "MGMT",
            )
        ).all()

        for item in existing:
            # Skip if already done
            if (item.done_date or "").strip():
                continue

            # Skip if in valid set
            if item.name_ref in valid_name_refs:
                continue

            # Skip if manually locked
            memo_data = _parse_memo(item.memo)
            if memo_data.get("locked"):
                logger.debug(f"Skipping locked stale item: {item.name_ref}")
                continue
            # Safety: only clean items that were auto-generated by this service.
            if not memo_data.get("auto"):
                continue
            # NOTICE_SEND_3D is synchronized from office_action lifecycle. It can
            # coexist with unrelated status-red/core deadlines and must not be
            # cancelled by the generic MGMT stale sweep.
            if _is_notice_send_sla_ref(item.name_ref):
                continue
            if (item.name_ref or "").startswith("MGMT:STATUS_RED:"):
                if _core_status_red_source_still_open(item=item, custom_data=custom_data):
                    continue
                item.done_date = date.today().isoformat()
                db.session.add(item)
                count += 1
                logger.debug(f"Auto-completed stale status-red deadline: {item.name_ref}")
                enqueue_docket_sync_for_item(docket_item=item)
                continue

            # Mark as auto-cancelled with timestamp
            item.done_date = f"AUTO_CANCELLED:{date.today().isoformat()}"
            db.session.add(item)
            count += 1
            logger.debug(f"Auto-cancelled stale deadline: {item.name_ref}")

            # Delete from Google Calendar
            enqueue_docket_sync_for_item(docket_item=item)

    except Exception as e:
        logger.warning(f"Failed to cleanup stale deadlines for {matter_id}: {e}")

    return count


def _cleanup_stale_status_red_deadlines(
    *, matter_id: str, valid_name_refs: set[str], custom_data: dict | None = None
) -> int:
    """
    Close stale auto-generated MGMT:STATUS_RED rows even when the deadline engine
    produced no current deadlines.

    This covers notice-driven status transitions such as Office action -> RegistrationDeadline,
    where the current red deadline may be updated outside the core engine path.
    """
    count = 0
    try:
        source_custom_data = (
            custom_data if custom_data is not None else _merge_custom_fields(str(matter_id))
        )
        existing = _active_docket_query(
            DocketItem.query.filter(
                DocketItem.matter_id == str(matter_id),
                DocketItem.name_ref.like("MGMT:STATUS_RED:%"),
            )
        ).all()

        for item in existing:
            if (item.done_date or "").strip():
                continue

            if item.name_ref in valid_name_refs:
                continue

            memo_data = _parse_memo(item.memo)
            if memo_data.get("locked"):
                continue
            if not memo_data.get("auto"):
                continue
            if _core_status_red_source_still_open(item=item, custom_data=source_custom_data):
                continue

            item.done_date = f"AUTO_CANCELLED:{date.today().isoformat()}"
            db.session.add(item)
            count += 1
            logger.debug(f"Auto-completed stale status-red deadline: {item.name_ref}")
            enqueue_docket_sync_for_item(docket_item=item)
    except Exception as e:
        logger.warning(f"Failed to cleanup stale status-red deadlines for {matter_id}: {e}")

    return count


def _cleanup_annuity_status_red_deadlines(*, matter_id: str) -> int:
    """
    Close legacy auto-generated annuity status-red dockets.

    Annuity schedules are managed by `annuity_item` + renewal module. Keeping
    duplicate `MGMT:STATUS_RED:<n>RenewalDeadline` rows as open docket items causes
    ownership/calendar/workflow divergence.
    """
    count = 0
    try:
        existing = _active_docket_query(
            DocketItem.query.filter(
                DocketItem.matter_id == str(matter_id),
                DocketItem.name_ref.like("MGMT:STATUS_RED:%"),
            )
        ).all()

        for item in existing:
            if (item.done_date or "").strip():
                continue

            memo_data = _parse_memo(item.memo)
            if not memo_data.get("auto"):
                continue

            if not is_annuity_status_red_deadline(
                name_ref=getattr(item, "name_ref", None),
                title=_item_label(item),
            ):
                continue

            item.done_date = date.today().isoformat()
            db.session.add(item)
            count += 1
            enqueue_docket_sync_for_item(docket_item=item)
    except Exception as e:
        logger.warning(f"Failed to cleanup annuity status-red deadlines for {matter_id}: {e}")

    return count


def _create_post_due_followups(
    *,
    docket_item: DocketItem,
    policy: dict,
    closed_at: date,
    effective_due: date | None = None,
    resolver: AssigneeResolver,
    custom_data: dict,
) -> int:
    templates = policy.get("followup_templates") or []
    if not templates:
        return 0

    base_due = effective_due or closed_at
    count = 0
    for tpl in templates:
        tpl_id = (tpl.get("id") or "").strip()
        title = (tpl.get("title") or "").strip()
        if not tpl_id or not title:
            continue

        due = _apply_offset(
            base_due,
            days=tpl.get("offset_days", 0),
            months=tpl.get("offset_months", 0),
            years=tpl.get("offset_years", 0),
        )

        owner = resolver.resolve(
            _resolve_assignee_value(custom_data, (tpl.get("assignee_field") or "").strip() or None)
        )

        name_ref = f"MGMT:FOLLOWUP:{tpl_id}:{docket_item.docket_id}"
        memo = None
        try:
            memo = json.dumps(
                {
                    "auto": True,
                    "trigger": "post_due_followup",
                    "template_id": tpl_id,
                    "policy_id": policy.get("id"),
                    "source_docket_id": docket_item.docket_id,
                    "closed_at": closed_at.isoformat(),
                    "effective_due": effective_due.isoformat() if effective_due else None,
                },
                ensure_ascii=False,
            )
        except Exception:
            memo = None

        _upsert_docket_item(
            matter_id=str(docket_item.matter_id),
            name_ref=name_ref,
            category=(tpl.get("category") or "MGMT"),
            title=title,
            due=due,
            owner=owner,
            memo=memo,
        )
        count += 1
    return count


def auto_close_post_due_deadlines(
    *,
    matter_id: str | None = None,
    today: date | None = None,
    commit: bool = False,
) -> dict:
    """
    Auto-close post-due deadlines based on policy definitions.
    """
    ensure_deadline_policies_seeded()
    policies = _load_deadline_policies()

    today = today or date.today()
    q = _active_docket_query(
        DocketItem.query.filter(
            or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == "")
        )
    )
    if matter_id:
        q = q.filter(DocketItem.matter_id == str(matter_id))
    q = q.filter((DocketItem.due_date.isnot(None)) | (DocketItem.extended_due_date.isnot(None)))
    items = q.all()

    resolver = AssigneeResolver()
    custom_cache: dict[str, dict] = {}

    def _custom_data(mid: str) -> dict:
        if mid not in custom_cache:
            custom_cache[mid] = _merge_custom_fields(mid)
        return custom_cache[mid]

    evaluated = 0
    closed = 0
    followups = 0

    for item in items:
        memo_data = _parse_memo(item.memo)
        memo_updates = None
        if memo_data.get("auto") is True:
            # Legacy auto-generated rows may have memo={"auto": true} only.
            # Recover policy/deadline hints from name_ref so post-due auto-close can still apply.
            has_policy_hint = bool(
                (memo_data.get("policy_id") or "").strip()
                or (memo_data.get("deadline_code") or "").strip()
                or (memo_data.get("template_id") or "").strip()
            )
            if not has_policy_hint:
                memo_updates = _infer_auto_memo_for_item(item=item, policies=policies)
                if memo_updates:
                    memo_data.update(memo_updates)
        elif "auto" in memo_data:
            continue
        else:
            memo_updates = _infer_auto_memo_for_item(item=item, policies=policies)
            if not memo_updates:
                continue
            memo_data.update(memo_updates)

        policy = _resolve_policy_for_item(
            memo_data=memo_data,
            name_ref=item.name_ref,
            policies=policies,
        )
        # Additional safeguard for legacy rows: try one more inference pass when
        # auto flag exists but policy resolution still fails.
        if not policy and memo_data.get("auto") is True:
            inferred = _infer_auto_memo_for_item(item=item, policies=policies)
            if inferred:
                memo_updates = {**(memo_updates or {}), **inferred}
                memo_data.update(inferred)
                policy = _resolve_policy_for_item(
                    memo_data=memo_data,
                    name_ref=item.name_ref,
                    policies=policies,
                )
        if not policy:
            continue
        if memo_data.get("locked") and policy.get("lockable", True):
            continue

        if memo_updates:
            item.memo = _merge_memo(item.memo, memo_updates)
            db.session.add(item)

        evaluated += 1
        post_policy = (policy.get("post_due_policy") or "").strip().upper()
        if post_policy not in ("AUTO_EXPIRE", "AUTO_EXPIRE_WITH_FOLLOWUP"):
            continue

        due = _effective_due_for_policy(item, policy)
        if not due:
            continue

        expire_after = int(policy.get("expire_after_days") or 0)
        if today <= (due + timedelta(days=expire_after)):
            continue

        close_mark = (policy.get("close_mark") or "EXPIRED").strip().upper()
        if close_mark == "DONE":
            done_value = today.isoformat()
            close_reason = "done"
        elif close_mark == "CANCELLED":
            done_value = f"AUTO_CANCELLED:{today.isoformat()}"
            close_reason = "cancelled"
        else:
            done_value = f"AUTO_EXPIRED:{today.isoformat()}"
            close_reason = "expired"

        item.done_date = done_value
        item.memo = _merge_memo(
            item.memo,
            {
                "policy_id": policy.get("id"),
                "post_due_policy": post_policy,
                "close_mark": close_mark,
                "close_reason": close_reason,
                "closed_at": today.isoformat(),
                "effective_due": due.isoformat(),
            },
        )
        db.session.add(item)
        try:
            enqueue_docket_sync_for_item(docket_item=item)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_deadlines.auto_close.enqueue_sync",
                log_key="mgmt_deadlines.auto_close.enqueue_sync",
                log_window_seconds=300,
            )
        closed += 1

        if post_policy == "AUTO_EXPIRE_WITH_FOLLOWUP":
            followups += _create_post_due_followups(
                docket_item=item,
                policy=policy,
                closed_at=today,
                effective_due=due,
                resolver=resolver,
                custom_data=_custom_data(str(item.matter_id)),
            )

    if commit:
        db.session.commit()

    return {
        "evaluated": evaluated,
        "closed": closed,
        "followups": followups,
    }


def ensure_mgmt_deadlines_for_matter(matter_id: str, *, commit: bool = False) -> None:
    """
    Ensure all management deadlines exist for a matter.

    Args:
        matter_id: The matter to process
        commit: If True, commit the transaction. If False, caller is responsible for commit.
    """
    ensure_templates_seeded()
    ensure_deadline_policies_seeded()

    matter = Matter.query.get(str(matter_id))
    if not matter:
        return

    custom_data = _merge_custom_fields(str(matter_id))
    deadlines_by_code = _compute_engine_deadlines(
        matter_id=str(matter_id),
        our_ref=(matter.our_ref or "").strip() or None,
        custom_data=custom_data,
        right_group=getattr(matter, "right_group", None),
        matter_type=getattr(matter, "matter_type", None),
    )
    foreign_filing_priority_exclusion_reason = _foreign_filing_priority_exclusion_reason(
        matter=matter,
        custom_data=custom_data,
    )
    foreign_filing_priority_excluded = bool(foreign_filing_priority_exclusion_reason)
    foreign_filing_priority_done_dt = (
        None
        if foreign_filing_priority_excluded
        else _foreign_filing_priority_done_signal_date(matter=matter, custom_data=custom_data)
    )
    if foreign_filing_priority_excluded:
        deadlines_by_code.pop("FOREIGN_FILING_PARIS", None)
        _cancel_foreign_filing_priority_deadlines(
            matter_id=str(matter_id),
            close_reason=foreign_filing_priority_exclusion_reason,
        )
    elif foreign_filing_priority_done_dt:
        deadlines_by_code.pop("FOREIGN_FILING_PARIS", None)
        _complete_foreign_filing_priority_deadlines(
            matter_id=str(matter_id),
            done_dt=foreign_filing_priority_done_dt,
            close_reason="done",
        )

    # Create resolver with caching for this operation
    resolver = AssigneeResolver()

    # Track valid name_refs for cleanup of stale auto-generated items
    valid_name_refs: set[str] = set()
    manual_core_status_red_refs: set[str] = set()

    # Legacy data may still carry term-expiry dockets under drifted labels/name_refs.
    # Close these first so renewal-managed matters stop showing them as open work.
    _close_renewal_managed_term_expiry_like_status_red_dockets(matter=matter)

    policies = _load_deadline_policies()
    templates = _load_templates()
    for t in templates:
        trigger = t.get("trigger", "")
        if trigger != "deadline_code":
            continue

        code = (t.get("deadline_code") or "").strip()
        if not code:
            continue
        if code == "FOREIGN_FILING_PARIS" and (
            foreign_filing_priority_excluded or foreign_filing_priority_done_dt
        ):
            continue

        base_due, base_due_source = _resolve_template_base_due(
            template=t,
            deadlines_by_code=deadlines_by_code,
            custom_data=custom_data,
            matter=matter,
        )
        if not base_due:
            continue

        skip_field = (t.get("skip_if_field_set") or "").strip()
        if skip_field:
            skip_date = _parse_date(custom_data.get(skip_field))
            if skip_date:
                _mark_done_by_name_ref(
                    matter_id=str(matter_id),
                    name_ref=f"MGMT:{t.get('id', '') or code}",
                    done_dt=skip_date,
                    close_reason="done",
                )
                continue

        due = _apply_offset(
            base_due,
            days=t.get("offset_days", 0),
            months=t.get("offset_months", 0),
            years=t.get("offset_years", 0),
        )
        has_visibility_offset = any(
            t.get(key) is not None
            for key in ("visible_offset_days", "visible_offset_months", "visible_offset_years")
        )
        visible_from = (
            compute_visible_from(
                due,
                days=int(t.get("visible_offset_days") or 0),
                months=int(t.get("visible_offset_months") or 0),
                years=int(t.get("visible_offset_years") or 0),
            )
            if has_visibility_offset
            else None
        )

        title = t.get("title", "") or f"{code} Deadline"
        category = t.get("category", "MGMT")
        tpl_id = t.get("id", "") or code
        name_ref = f"MGMT:{tpl_id}"

        # Use cached resolver
        owner = resolver.resolve(
            _resolve_assignee_value(custom_data, (t.get("assignee_field") or "").strip() or None)
        )

        policy_id = _resolve_policy_id_for_metadata(
            deadline_code=code,
            name_ref=name_ref,
            template_id=tpl_id,
            policies=policies,
        )
        memo = None
        try:
            memo = json.dumps(
                {
                    "auto": True,
                    "template_id": tpl_id,
                    "trigger": trigger,
                    "deadline_code": code,
                    "base_due": base_due.isoformat(),
                    "base_due_source": base_due_source or "unknown",
                    "policy_id": policy_id,
                },
                ensure_ascii=False,
            )
        except Exception:
            memo = None

        _upsert_docket_item(
            matter_id=str(matter_id),
            name_ref=name_ref,
            category=category,
            title=title,
            due=due,
            visible_from=visible_from,
            owner=owner,
            memo=memo,
            clear_visible_from=not has_visibility_offset,
        )
        valid_name_refs.add(name_ref)

    # Core post-filing deadlines: keep both foreign filing + exam request visible/managed together.
    # (Filing  Examination request    'ForeignFilingDeadline'  'Examination requestDeadline'  )
    matter_type_raw = (getattr(matter, "matter_type", None) or "").strip()
    matter_type = matter_type_raw.upper()
    our_ref = (getattr(matter, "our_ref", None) or "").strip().upper()
    if not matter_type and len(our_ref) >= 4 and our_ref[:2].isdigit():
        code = our_ref[2:4]
        if code.startswith("P"):
            matter_type = "PATENT"
        elif code.startswith("U"):
            matter_type = "UTILITY"
        elif code.startswith("D"):
            matter_type = "DESIGN"
        elif code.startswith("T"):
            matter_type = "TRADEMARK"
    is_pct_case = _is_pct_matter(matter_type=matter_type_raw or matter_type, our_ref=our_ref)
    is_uspto_managed_case = is_uspto_managed_matter(matter)

    exam_spec = {
        "label": "Examination requestDeadline",
        "engine_code": "REQUEST_EXAMINATION",
        "manual_due_keys": ["exam_deadline", "exam_request_deadline"],
        "done_keys": ["exam_request_date"],
    }
    if is_uspto_managed_case:
        exam_spec["visible_offset_months"] = -2

    core_specs: list[dict] = []
    if not foreign_filing_priority_excluded and not foreign_filing_priority_done_dt:
        core_specs.append(
            {
                "label": "ForeignFilingDeadline",
                "engine_code": "FOREIGN_FILING_PARIS",
                "manual_due_keys": ["foreign_filing_deadline"],
                "done_keys": ["foreign_filing_date"],
                "visible_offset_months": -1,
                "category": "MGMT_WORK",
            }
        )
    core_specs.append(exam_spec)
    if is_pct_case:
        core_specs.append(
            {
                "label": "PCTDomesticDeadline",
                "engine_code": "PCT_NATIONAL_PHASE",
                "manual_due_keys": ["national_phase_deadline"],
                "done_keys": ["national_phase_last_entry_date"],
                "visible_offset_days": -120,
                "category": "MGMT_WORK",
            }
        )

    for spec in core_specs:
        label = (spec.get("label") or "").strip()
        if not label:
            continue
        if is_annuity_status_red_label(label):
            continue

        # Skip if already completed
        done_dt = None
        for k in spec.get("done_keys") or []:
            done_dt = _parse_date(custom_data.get(k))
            if done_dt:
                break
        if done_dt:
            policy_id = _resolve_policy_id_for_metadata(
                deadline_code=spec.get("engine_code"),
                name_ref=f"MGMT:STATUS_RED:{label}",
                template_id=None,
                policies=policies,
            )
            _mark_done_by_name_ref(
                matter_id=str(matter_id),
                name_ref=f"MGMT:STATUS_RED:{label}",
                done_dt=done_dt,
                close_reason="done",
                policy_id=policy_id,
            )
            continue

        due: date | None = None
        used_manual = False
        for k in spec.get("manual_due_keys") or []:
            due = _parse_date(custom_data.get(k))
            if due:
                used_manual = True
                break
        if not due:
            due = _pick_preferred_deadline(
                deadlines_by_code.get((spec.get("engine_code") or "").strip()) or []
            )
        if not due:
            continue

        policy_id = _resolve_policy_id_for_metadata(
            deadline_code=spec.get("engine_code"),
            name_ref=f"MGMT:STATUS_RED:{label}",
            template_id=None,
            policies=policies,
        )
        memo = None
        try:
            memo = json.dumps(
                {
                    "auto": True,
                    "trigger": "core_deadline",
                    "deadline_code": spec.get("engine_code"),
                    "source": "manual" if used_manual else "engine",
                    "policy_id": policy_id,
                },
                ensure_ascii=False,
            )
        except Exception:
            memo = None

        name_ref = f"MGMT:STATUS_RED:{label}"
        has_visibility_offset = any(
            spec.get(key) is not None
            for key in ("visible_offset_days", "visible_offset_months", "visible_offset_years")
        )
        visible_from = (
            compute_visible_from(
                due,
                days=int(spec.get("visible_offset_days") or 0),
                months=int(spec.get("visible_offset_months") or 0),
                years=int(spec.get("visible_offset_years") or 0),
            )
            if has_visibility_offset
            else None
        )
        _upsert_docket_item(
            matter_id=str(matter_id),
            name_ref=name_ref,
            category=(spec.get("category") or "DEADLINE"),
            title=label,
            due=due,
            visible_from=visible_from,
            owner=None,
            memo=memo,
            clear_visible_from=not has_visibility_offset,
        )
        valid_name_refs.add(name_ref)
        if used_manual:
            manual_core_status_red_refs.add(name_ref)

    priority_exam_name_ref = "MGMT:STATUS_RED:ExaminationOpen"
    priority_exam_done_dt = _parse_date(
        custom_data.get("expedited_request_date") or custom_data.get("expedited_decision_date")
    )
    priority_exam_due = _priority_exam_progress_due(custom_data)
    if priority_exam_done_dt:
        _mark_done_by_name_ref(
            matter_id=str(matter_id),
            name_ref=priority_exam_name_ref,
            done_dt=priority_exam_done_dt,
            close_reason="done",
        )
    elif priority_exam_due:
        priority_exam_memo = None
        try:
            priority_exam_memo = json.dumps(
                {
                    "auto": True,
                    "trigger": "priority_exam_progress",
                    "source": "application_date_plus_7d",
                    "priority_exam_request": True,
                },
                ensure_ascii=False,
            )
        except Exception:
            priority_exam_memo = None
        _upsert_docket_item(
            matter_id=str(matter_id),
            name_ref=priority_exam_name_ref,
            category="DEADLINE",
            title="ExaminationOpen",
            due=priority_exam_due,
            owner=None,
            memo=priority_exam_memo,
        )
        valid_name_refs.add(priority_exam_name_ref)

    # Also surface current auto-status red deadline as a docket_item.
    # SKIP: FilingDeadline, Filing deadline - These are already managed via Workflow in general.py
    SKIP_STATUS_RED = {"FilingDeadline", "Filing deadline"}
    try:
        red = (matter.status_red or "").strip()
    except Exception:
        red = ""
    red_due = _parse_date(getattr(matter, "status_red_related_date", None))
    if (
        red
        and red_due
        and red not in SKIP_STATUS_RED
        and not is_annuity_status_red_label(red)
        and not is_non_action_status_red_label(red)
    ):
        # Let _upsert_docket_item resolve owner based on task type.
        owner = None
        memo = None
        status_red_name_ref = f"MGMT:STATUS_RED:{red}"
        if is_pct_case and _is_pct_advisory_status_red_label(red):
            _mark_done_by_name_ref(
                matter_id=str(matter_id),
                name_ref=status_red_name_ref,
                done_dt=date.today(),
                close_reason="superseded_pct_advisory_status_red",
            )
        elif _is_renewal_managed_term_expiry_status_red(matter=matter, red_label=red):
            _mark_done_by_name_ref(
                matter_id=str(matter_id),
                name_ref=status_red_name_ref,
                done_dt=date.today(),
                close_reason="moved_to_renewal",
            )
        elif foreign_filing_priority_excluded and _is_foreign_filing_status_red_ref(
            status_red_name_ref
        ):
            _cancel_foreign_filing_priority_deadlines(
                matter_id=str(matter_id),
                close_reason=foreign_filing_priority_exclusion_reason,
            )
        elif foreign_filing_priority_done_dt and _is_foreign_filing_status_red_ref(
            status_red_name_ref
        ):
            _complete_foreign_filing_priority_deadlines(
                matter_id=str(matter_id),
                done_dt=foreign_filing_priority_done_dt,
                close_reason="done",
            )
        elif status_red_name_ref in manual_core_status_red_refs:
            # Manual/custom core deadlines are the source of truth. A stale cached
            # Matter.status_red_related_date must not overwrite the just-resolved
            # docket date for the same MGMT:STATUS_RED item.
            valid_name_refs.add(status_red_name_ref)
        else:
            status_red_done_dt = _status_red_done_signal_date(
                red_label=red,
                custom_data=custom_data,
            )
            if status_red_done_dt:
                policy_id = _resolve_policy_id_for_metadata(
                    deadline_code=None,
                    name_ref=status_red_name_ref,
                    template_id=None,
                    policies=policies,
                )
                _mark_done_by_name_ref(
                    matter_id=str(matter_id),
                    name_ref=status_red_name_ref,
                    done_dt=status_red_done_dt,
                    close_reason="done",
                    policy_id=policy_id,
                )
                valid_name_refs.add(status_red_name_ref)
            else:
                status_red_category = (
                    "MGMT_WORK" if _is_mixed_status_red_ref(status_red_name_ref) else "DEADLINE"
                )
                status_red_visible_from, has_status_red_visibility = _status_red_visibility_window(
                    red_label=red,
                    due_date=red_due,
                    is_uspto_managed_case=is_uspto_managed_case,
                )
                try:
                    memo = json.dumps(
                        {
                            "auto": True,
                            "trigger": "status_red",
                            "status_red": red,
                            "status_red_related_date": red_due.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                except Exception:
                    memo = None
                _upsert_docket_item(
                    matter_id=str(matter_id),
                    name_ref=status_red_name_ref,
                    category=status_red_category,
                    title=red,
                    due=red_due,
                    visible_from=status_red_visible_from,
                    owner=owner,
                    memo=memo,
                    clear_visible_from=not has_status_red_visibility,
                )
                valid_name_refs.add(status_red_name_ref)

    # Hard guardrail: annuity status-red rows should never remain as open docket items.
    _cleanup_annuity_status_red_deadlines(matter_id=str(matter_id))
    _cleanup_stale_status_red_deadlines(
        matter_id=str(matter_id),
        valid_name_refs=valid_name_refs,
        custom_data=custom_data,
    )

    # Keep legacy filing deadlines from being auto-cancelled as stale.
    # They may still exist for older matters even though creation is now handled by workflow.
    filing_deadline_due = _parse_date(custom_data.get("filing_deadline"))
    if filing_deadline_due:
        valid_name_refs.add("MGMT:FILING_DEADLINE")

    # Keep skipped filing status-red aliases from being auto-cancelled by stale cleanup.
    if red in SKIP_STATUS_RED and red_due:
        valid_name_refs.add(f"MGMT:STATUS_RED:{red}")

    # Cleanup stale auto-generated deadlines that are no longer valid.
    # Use the resolved valid_name_refs set rather than DeadlineEngine output alone.
    # Some edits intentionally clear one deadline while keeping other explicit deadlines,
    # and those cases still need stale MGMT rows removed.
    if valid_name_refs:
        _cleanup_stale_auto_deadlines(
            matter_id=str(matter_id),
            valid_name_refs=valid_name_refs,
        )
    elif (
        (red in SKIP_STATUS_RED and red_due)
        or foreign_filing_priority_excluded
        or foreign_filing_priority_done_dt
    ):
        pass
    else:
        logger.warning(
            "Skipping stale MGMT deadline cleanup for %s: no valid deadline refs were resolved.",
            str(matter_id),
        )

    # Auto-close post-due opportunity/reminder deadlines for this matter.
    try:
        auto_close_post_due_deadlines(matter_id=str(matter_id), commit=False)
    except Exception as e:
        logger.warning(f"Auto-close post-due deadlines failed for {matter_id}: {e}")

    if commit:
        db.session.commit()


def create_notice_send_sla(
    *,
    matter_id: str,
    oa_id: str,
    received_date: str | None,
    doc_name: str | None,
    commit: bool = False,
) -> None:
    """
    Create SLA deadline for sending notice to client.

    Args:
        commit: If True, commit the transaction. If False, caller is responsible for commit.
    """
    ensure_templates_seeded()

    custom_data = _merge_custom_fields(str(matter_id))
    resolver = AssigneeResolver()
    owner = resolver.resolve(_resolve_assignee_value(custom_data, "manager"))

    base = _parse_date(received_date) or date.today()
    due = _apply_offset(base, days=3)

    title_base = "Notice Client(3 )"
    doc = (doc_name or "").strip()
    title = f"{title_base} · {doc}" if doc else title_base

    memo = None
    try:
        memo = json.dumps(
            {
                "auto": True,
                "template_id": "NOTICE_SEND_3D",
                "trigger": "office_action_received",
                "oa_id": oa_id,
                "received_date": base.isoformat(),
            },
            ensure_ascii=False,
        )
    except Exception:
        memo = None

    _upsert_docket_item(
        matter_id=str(matter_id),
        name_ref=f"MGMT:NOTICE_SEND_3D:{oa_id}",
        category="NOTICE",
        title=title,
        due=due,
        owner=owner,
        memo=memo,
    )

    if commit:
        db.session.commit()


def _is_notice_send_target_doc(doc_name: str | None) -> bool:
    compact = _WS_RE.sub("", str(doc_name or "")).strip().lower()
    if not compact:
        return False
    if any(hint in compact for hint in _NOTICE_SEND_RESPONSE_HINTS):
        return False
    return any(hint in compact for hint in _NOTICE_SEND_NOTICE_HINTS)


def sync_notice_send_sla(
    *,
    matter_id: str,
    oa_id: str,
    received_date: str | None,
    doc_name: str | None,
    done_date: str | None,
    commit: bool = False,
) -> None:
    """
    Keep NOTICE_SEND_3D in sync with the lifecycle of a USPTO office_action.

    This must be called from every office_action create/update/delete path so
    uploads, manual history edits, and future ingestion paths converge on the
    same behavior.
    """
    mid = (matter_id or "").strip()
    oid = (oa_id or "").strip()
    if not mid or not oid:
        return

    name_ref = f"MGMT:NOTICE_SEND_3D:{oid}"
    matter = Matter.query.filter_by(matter_id=mid).first()
    if matter is None or not is_uspto_managed_matter(matter):
        _mark_done_token_by_name_ref(
            matter_id=mid,
            name_ref=name_ref,
            done_value=f"AUTO_CANCELLED:{date.today().isoformat()}",
            close_reason="not_uspto_managed",
        )
        if commit:
            db.session.commit()
        return

    if not _is_notice_send_target_doc(doc_name):
        _mark_done_token_by_name_ref(
            matter_id=mid,
            name_ref=name_ref,
            done_value=f"AUTO_CANCELLED:{date.today().isoformat()}",
            close_reason="doc_type_not_notice",
        )
        if commit:
            db.session.commit()
        return

    done_token = normalize_done_date(done_date)
    if done_token:
        close_reason = "office_action_done"
        upper = done_token.upper()
        if upper.startswith("AUTO_CANCELLED:"):
            close_reason = "office_action_cancelled"
        elif upper.startswith("AUTO_EXPIRED:"):
            close_reason = "office_action_expired"
        _mark_done_token_by_name_ref(
            matter_id=mid,
            name_ref=name_ref,
            done_value=done_token,
            close_reason=close_reason,
        )
        if commit:
            db.session.commit()
        return

    create_notice_send_sla(
        matter_id=mid,
        oa_id=oid,
        received_date=received_date,
        doc_name=doc_name,
        commit=commit,
    )


def create_office_action_due_deadline(
    *,
    matter_id: str,
    oa_id: str,
    doc_name: str | None,
    due_date: str | None,
    extended_due_date: str | None,
    done_date: str | None,
    commit: bool = False,
) -> None:
    """
    Create deadline for office action response (Att + Hdl + Mgmt).

    Manager-only notice rules can reduce this to a single MGMT task.
    """
    ensure_templates_seeded()

    due_dt = _parse_date(due_date)
    ext_dt = _parse_date(extended_due_date)
    done_dt = _parse_date(done_date)

    if not (due_dt or ext_dt):
        return

    effective_due = ext_dt or due_dt
    if not effective_due:
        return

    # Resolve owners as staff_party_id (NOT users.id).
    #
    # - CaseFlatIndex.{attorney_id,handler_id,manager_id} stores User.id (as text) and may be missing.
    # - DocketItem.owner_staff_party_id must store staff_party_id (party_id) for downstream mapping and
    #   notifications (email) to work correctly.
    mid = (matter_id or "").strip()
    att_pid = _resolve_owner_from_matter_staff(mid, category_type="WORK")
    hdl_pid = _resolve_owner_from_matter_staff(
        mid,
        role_priority=["handler", "staff", "draftsman"],
        category_type="WORK",
    )
    mgr_pid = _resolve_owner_from_matter_staff(mid, category_type="MGMT")

    title_base = "Notice  Deadline"
    doc = (doc_name or "").strip()
    title_work = f"{title_base} · {doc}" if doc else title_base
    title_mgmt = f"Notice  · {doc}" if doc else "Notice "
    manager_only = is_manager_only_notice(name_ref=None, name_free=title_work)
    manager_title = title_work if manager_only else title_mgmt

    memo = None
    try:
        memo = json.dumps(
            {
                "auto": True,
                "trigger": "office_action_due",
                "oa_id": oa_id,
                "due_date": (due_date or "") or None,
                "extended_due_date": (extended_due_date or "") or None,
            },
            ensure_ascii=False,
        )
    except Exception:
        memo = None

    if not manager_only:
        # 1. Attorney Task (Main)
        _upsert_docket_item(
            matter_id=str(matter_id),
            name_ref=f"NOTICE:OA:{oa_id}",
            category="NOTICE",
            title=title_work,
            due=(due_dt or effective_due),
            owner=att_pid,
            memo=memo,
            internal_due=(ext_dt if (due_dt and ext_dt) else None),
            done=done_dt,
            clear_internal_due=(extended_due_date is None),
        )

        # 2. Handler Task (Secondary Work)
        if hdl_pid and hdl_pid != att_pid:
            _upsert_docket_item(
                matter_id=str(matter_id),
                name_ref=f"NOTICE:OA:{oa_id}:HDL",
                category="NOTICE",
                title=f"{title_work} (Process)",
                due=(due_dt or effective_due),
                owner=hdl_pid,
                memo=memo,
                internal_due=(ext_dt if (due_dt and ext_dt) else None),
                done=done_dt,
                clear_internal_due=(extended_due_date is None),
            )

    # 3. Mgmt Task (Manager)
    _upsert_docket_item(
        matter_id=str(matter_id),
        name_ref=f"MGMT:NOTICE:OA:{oa_id}",
        category="MGMT",
        title=manager_title,
        due=(due_dt or effective_due),
        owner=mgr_pid,
        memo=memo,
        internal_due=(ext_dt if (due_dt and ext_dt) else None),
        done=done_dt,
        clear_internal_due=(extended_due_date is None),
    )

    if commit:
        db.session.commit()
