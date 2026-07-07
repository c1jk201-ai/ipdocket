from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import bindparam

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.docket import DocketItem
from app.models.user import User
from app.utils.policy_sql import policy_text as text
from app.utils.task_distribution_rules import DistributionDecision, resolve_distribution_decision

# Centralized assignment/distribution rules for workflow creation.

TASK_SOURCE_USPTO_NOTICE = "uspto_notice"
TASK_SOURCE_UPLOAD_AUTOMATION = "upload_automation"

ALL_STAFF_ROLE_CODES = (
    "attorney",
    "retainer",
    "handler",
    "manager",
    "mgmt",
    "staff",
    "draftsman",
)

MANAGER_ROLE_CODES = (
    "manager",
    "mgmt",
)

LEGACY_MATTER_STAFF_ROLES = ("ATT", "MGR", "HANDLER")

_ASSIGNMENT_CACHE_KEY = "_task_assignment_cache"

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


@dataclass(frozen=True)
class AssigneeInfo:
    user_id: int
    role_code: str | None = None
    staff_name: str | None = None


def _normalize_assignee_role_code(role_code: str | None) -> str | None:
    role = (role_code or "").strip().lower()
    if role in {"manager", "mgmt"}:
        return "manager"
    if role in {"attorney", "retainer"}:
        return "attorney"
    if role in {"handler", "staff", "draftsman"}:
        return "handler"
    if not role:
        return None
    return role


def _collapse_role_set_assignees(rows: Iterable[AssigneeInfo]) -> list[AssigneeInfo]:
    kept: list[AssigneeInfo] = []
    seen_user_roles: set[tuple[int, str]] = set()
    seen_roles: set[str] = set()
    for row in rows:
        normalized_role = _normalize_assignee_role_code(row.role_code)
        user_role_key = (row.user_id, normalized_role or "")
        if user_role_key in seen_user_roles:
            continue
        if normalized_role and normalized_role in seen_roles:
            continue
        kept.append(row)
        seen_user_roles.add(user_role_key)
        if normalized_role:
            seen_roles.add(normalized_role)
    return kept


def _extract_task_source_from_docket(docket_item: DocketItem | None) -> str | None:
    if not docket_item:
        return None
    raw_source = (getattr(docket_item, "source", None) or "").strip()
    if raw_source:
        return raw_source.lower()

    memo = (getattr(docket_item, "memo", None) or "").strip()
    payload: dict[str, object] = {}
    if memo:
        try:
            parsed = json.loads(memo)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
            src = str(payload.get("source") or "").strip()
            if src:
                return src.lower()

            # Backward compatibility for legacy JSON memo rows without explicit source.
            if isinstance(payload.get("events"), list):
                return TASK_SOURCE_UPLOAD_AUTOMATION
            trigger = str(payload.get("trigger") or "").strip()
            if trigger in {"REGISTRATION_CERTIFICATE", "Notice of allowance", "Final rejection"}:
                return TASK_SOURCE_USPTO_NOTICE

    # New non-JSON fallback: infer source from stable ref/category patterns.
    # Keep this last so explicit memo metadata wins when present.
    category = (getattr(docket_item, "category", None) or "").strip().upper()
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    if name_ref.startswith("USPTO:"):
        return TASK_SOURCE_USPTO_NOTICE
    if name_ref.startswith("USPTO_OA:"):
        return TASK_SOURCE_UPLOAD_AUTOMATION
    if category == "USPTO_OA":
        return TASK_SOURCE_UPLOAD_AUTOMATION
    return None


def is_manager_only_notice(
    name_ref: str | None,
    name_free: str | None,
    *,
    category: str | None = None,
    source: str | None = None,
) -> bool:
    decision = resolve_distribution_decision(
        category=category,
        name_ref=name_ref,
        name_free=name_free,
        source=source,
    )
    if decision.distribute_to != "role_set":
        return False
    if not decision.role_codes:
        return False
    return all((role or "").lower() in MANAGER_ROLE_CODES for role in decision.role_codes)


def should_distribute_to_all(
    *,
    category: str | None,
    name_ref: str | None,
    name_free: str | None,
    source: str | None = None,
) -> bool:
    decision = resolve_distribution_decision(
        category=category,
        name_ref=name_ref,
        name_free=name_free,
        source=source,
    )
    return decision.distribute_to == "all_staff"


def resolve_distribution_decision_for_docket(docket_item: DocketItem) -> DistributionDecision:
    return resolve_distribution_decision(
        category=docket_item.category,
        name_ref=docket_item.name_ref,
        name_free=docket_item.name_free,
        source=_extract_task_source_from_docket(docket_item),
    )


def is_manager_only_notice_task(docket_item: DocketItem) -> bool:
    return is_manager_only_notice(
        docket_item.name_ref,
        docket_item.name_free,
        category=docket_item.category,
        source=_extract_task_source_from_docket(docket_item),
    )


def should_distribute_to_all_staff(docket_item: DocketItem) -> bool:
    return should_distribute_to_all(
        category=docket_item.category,
        name_ref=docket_item.name_ref,
        name_free=docket_item.name_free,
        source=_extract_task_source_from_docket(docket_item),
    )


def _get_assignment_cache() -> dict:
    cache = db.session.info.get(_ASSIGNMENT_CACHE_KEY)
    if cache is None:
        cache = {
            "user_by_staff_party_id": {},
            "assignees_by_matter_role": {},
        }
        db.session.info[_ASSIGNMENT_CACHE_KEY] = cache
    return cache


def resolve_user_id_by_staff_party_id(staff_party_id: str | None) -> int | None:
    if not staff_party_id:
        return None
    cache = _get_assignment_cache()
    cached = cache["user_by_staff_party_id"].get(staff_party_id)
    if cached is not None:
        return cached
    user = (
        User.query.filter_by(staff_party_id=staff_party_id, is_active=True)
        .order_by(User.id.desc())
        .first()
    )
    user_id = user.id if user else None
    cache["user_by_staff_party_id"][staff_party_id] = user_id
    return user_id


def _dedupe_assignees(rows: Iterable[AssigneeInfo]) -> list[AssigneeInfo]:
    seen = set()
    out = []
    for row in rows:
        if row.user_id in seen:
            continue
        seen.add(row.user_id)
        out.append(row)
    return out


def _dedupe_assignees_by_user_role(rows: Iterable[AssigneeInfo]) -> list[AssigneeInfo]:
    seen = set()
    out = []
    for row in rows:
        normalized_role = _normalize_assignee_role_code(row.role_code) or ""
        key = (row.user_id, normalized_role)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _fetch_assignees_from_assignment(
    *, matter_id: str, role_codes: Iterable[str]
) -> list[AssigneeInfo]:
    clean_roles = [str(role).strip().lower() for role in role_codes if role]
    if not clean_roles:
        return []
    try:
        with db.session.begin_nested():
            rows = db.session.execute(
                text(
                    """
                    SELECT u.id, msa.staff_role_code, p.name_display
                    FROM matter_staff_assignment msa
                    JOIN users u ON u.staff_party_id = msa.staff_party_id
                    LEFT JOIN party p ON p.party_id = msa.staff_party_id
                    WHERE msa.matter_id = :mid
                      AND LOWER(TRIM(msa.staff_role_code)) IN :role_codes
                      AND u.is_active = TRUE
                    ORDER BY msa.msa_id ASC, u.id ASC
                    """
                )
                .bindparams(bindparam("role_codes", expanding=True))
                .execution_options(policy_bypass=True),
                {"mid": str(matter_id), "role_codes": clean_roles},
            ).fetchall()
    except Exception as exc:
        _report_swallowed_exception(
            exc,
            context=f"_fetch_assignees_from_assignment(matter_id={matter_id})",
        )
        return []
    return [AssigneeInfo(int(r[0]), str(r[1]).strip() if r[1] else None, r[2]) for r in rows]


def _fetch_assignees_from_flat_index(matter_id: str) -> list[AssigneeInfo]:
    idx = CaseFlatIndex.query.get(str(matter_id))
    if not idx:
        return []
    rows = []
    mapping = (
        (idx.manager_id, "manager"),
        (idx.attorney_id, "attorney"),
        (idx.handler_id, "handler"),
    )
    for user_id, role_code in mapping:
        try:
            uid = int(user_id) if user_id is not None else None
        except (ValueError, TypeError):
            continue
        if not uid:
            continue
        user = User.query.get(uid)
        if not user or not bool(getattr(user, "is_active", False)):
            continue
        rows.append(AssigneeInfo(int(user.id), role_code, None))
    return rows


def _resolve_assignees_for_matter(
    *, matter_id: str, role_codes: Iterable[str] | None = None
) -> list[AssigneeInfo]:
    role_codes = role_codes or ALL_STAFF_ROLE_CODES
    cache = _get_assignment_cache()
    cache_key = (str(matter_id), tuple(sorted([str(r).lower() for r in role_codes])))
    cached = cache["assignees_by_matter_role"].get(cache_key)
    if cached is not None:
        return cached

    rows = _fetch_assignees_from_assignment(matter_id=matter_id, role_codes=role_codes)
    if rows:
        deduped = _dedupe_assignees_by_user_role(rows)
        cache["assignees_by_matter_role"][cache_key] = deduped
        return deduped

    flat_rows = _fetch_assignees_from_flat_index(matter_id)
    target_roles = {str(r).strip().lower() for r in role_codes if str(r).strip()}
    if "mgmt" in target_roles:
        target_roles.add("manager")
    if "retainer" in target_roles:
        target_roles.add("attorney")
    if "staff" in target_roles or "draftsman" in target_roles:
        target_roles.add("handler")
    flat_rows = [row for row in flat_rows if (row.role_code or "").lower() in target_roles]
    deduped = _dedupe_assignees_by_user_role(flat_rows)
    cache["assignees_by_matter_role"][cache_key] = deduped
    return deduped


def resolve_assignees_for_task(
    *,
    matter_id: str,
    name_ref: str | None,
    name_free: str | None,
    category: str | None,
    owner_staff_party_id: str | None = None,
    fallback_user_id: int | None = None,
    fallback_to_all: bool = False,
    source: str | None = None,
    return_decision: bool = False,
) -> list[AssigneeInfo] | tuple[list[AssigneeInfo], DistributionDecision]:
    owner_id = resolve_user_id_by_staff_party_id(owner_staff_party_id)
    decision = resolve_distribution_decision(
        category=category,
        name_ref=name_ref,
        name_free=name_free,
        source=source,
    )

    def _finalize(
        rows: list[AssigneeInfo],
    ) -> list[AssigneeInfo] | tuple[list[AssigneeInfo], DistributionDecision]:
        if return_decision:
            return rows, decision
        return rows

    if decision.rule_id and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Task distribution rule matched: rule=%s distribute_to=%s matter=%s",
            decision.rule_id,
            decision.distribute_to,
            matter_id,
        )

    if decision.distribute_to not in {"owner", "role_set", "all_staff", "none"}:
        logger.error(
            "Unknown distribute_to '%s' (rule=%s, matter=%s); skipping assignment",
            decision.distribute_to,
            decision.rule_id,
            matter_id,
        )
        return _finalize([])

    if decision.distribute_to == "role_set":
        role_codes = decision.role_codes or MANAGER_ROLE_CODES
        managers = _resolve_assignees_for_matter(matter_id=matter_id, role_codes=role_codes)
        managers = _collapse_role_set_assignees(managers)
        if managers:
            return _finalize(managers)
        # role_set rules are explicit audience controls (e.g. manager-only).
        # If targets are missing, do not leak to owner implicitly.
        if fallback_user_id:
            return _finalize([AssigneeInfo(int(fallback_user_id), "fallback", None)])
        return _finalize([])

    if decision.distribute_to == "all_staff":
        assignees = _resolve_assignees_for_matter(matter_id=matter_id)
        if owner_id:
            assignees = [AssigneeInfo(owner_id, "owner", None), *assignees]
        assignees = _dedupe_assignees(assignees)
        if assignees:
            return _finalize(assignees)
        if fallback_user_id:
            return _finalize([AssigneeInfo(int(fallback_user_id), "fallback", None)])
        return _finalize([])

    if decision.distribute_to == "none":
        return _finalize([])

    if decision.distribute_to == "owner":
        if owner_id:
            return _finalize([AssigneeInfo(owner_id, "owner", None)])
        if fallback_user_id:
            return _finalize([AssigneeInfo(int(fallback_user_id), "fallback", None)])
        if fallback_to_all:
            return _finalize(_resolve_assignees_for_matter(matter_id=matter_id))
        return _finalize([])

    # Defensive fallback: should be unreachable due to allowed distribute_to guard above.
    logger.error(
        "Unhandled distribute_to '%s' (rule=%s, matter=%s)",
        decision.distribute_to,
        decision.rule_id,
        matter_id,
    )
    return _finalize([])


def resolve_assignees_for_docket(
    docket_item: DocketItem,
    *,
    fallback_user_id: int | None = None,
    fallback_to_all: bool = False,
    return_decision: bool = False,
) -> list[AssigneeInfo] | tuple[list[AssigneeInfo], DistributionDecision]:
    return resolve_assignees_for_task(
        matter_id=str(docket_item.matter_id),
        name_ref=docket_item.name_ref,
        name_free=docket_item.name_free,
        category=docket_item.category,
        owner_staff_party_id=docket_item.owner_staff_party_id,
        fallback_user_id=fallback_user_id,
        fallback_to_all=fallback_to_all,
        source=_extract_task_source_from_docket(docket_item),
        return_decision=return_decision,
    )
