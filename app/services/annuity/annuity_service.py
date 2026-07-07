"""
Annuity Auto-generation Service

Automatically generates AnnuityItem records for registered matters based on
the registration date and right type using the deadline_engine annuity module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta

from app.extensions import db
from app.models.matter_facts import MatterFacts
from app.models.ip_records import AnnuityItem, Matter, MatterCustomField
from app.models.system_config import SystemConfig
from app.services.case.case_kind import is_uspto_managed_matter, resolve_profile_case_kind
from app.services.case.case_policy_service import get_policy_section
from app.services.core.config_service import ConfigService
from app.services.workflow.sync_requests import enqueue_annuity_sync_for_matter
from app.utils.docket_dates import parse_date as _parse_date_value
from app.utils.error_logging import report_swallowed_exception
from app.utils.registration_date import (
    REG_FEE_PAID_CUSTOM_KEYS,
    REG_FEE_PAID_EVENT_KEYS,
    REGISTRATION_CUSTOM_KEYS,
    REGISTRATION_EVENT_KEYS,
    data_has_any_key,
    find_first_date,
)

logger = logging.getLogger(__name__)


# Default number of years to generate annuities for
DEFAULT_ANNUITY_YEARS = 20

# Right types that require annuities
ANNUITY_RIGHT_TYPES = {"PATENT", "UTILITY", "DESIGN", "TRADEMARK"}
_TRADEMARK_RENEWAL_TERM_YEARS = 10
_TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT = 10
_TRADEMARK_SPLIT_PAYMENT_YEARS = 5
_TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS_DEFAULT = 90
_TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS_DEFAULT = 180
_TRADEMARK_RENEWAL_OPEN_DAYS_DEFAULT = 365
_TRADEMARK_RENEWAL_GRACE_MONTHS = 6
_TERM_EXPIRY_EVENT_KEYS: tuple[str, ...] = (
    " Period ",
    "TERM_EXPIRY_DATE",
    "term_expiry_date",
)
_TERM_EXPIRY_CUSTOM_KEYS: tuple[str, ...] = (
    "term_expiry_date",
    "TERM_EXPIRY_DATE",
    " Period ",
)

_REG_EVENT_KEYS_SQL = ", ".join([f"'{k}'" for k in REGISTRATION_EVENT_KEYS])
_REG_CUSTOM_KEYS_SQL = " OR ".join(
    [f"mcf.data::text LIKE '%{k}%'" for k in REGISTRATION_CUSTOM_KEYS]
)


def _get_int_config(key: str, default: int) -> int:
    """Fetch int config with safe fallback."""
    policy = get_policy_section("annuity") or get_policy_section("annuity_policy")
    if isinstance(policy, dict):
        raw = policy.get(key)
        if raw is None:
            raw = policy.get(key.lower())
        if raw is not None:
            try:
                return int(raw)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="annuity_service.get_int_config",
                    log_key=f"annuity_service.get_int_config.{key}",
                    log_window_seconds=300,
                )
    value = ConfigService.get_int(key, default)
    return default if value is None else value


def _get_bool_config(key: str, default: bool) -> bool:
    """Fetch boolean config with safe fallback."""
    policy = get_policy_section("annuity") or get_policy_section("annuity_policy")
    if isinstance(policy, dict):
        raw = policy.get(key)
        if raw is None:
            raw = policy.get(key.lower())
        if raw is not None:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(raw)
            try:
                return str(raw).strip().lower() in ("1", "true", "yes", "on")
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="annuity_service.get_bool_config",
                    log_key=f"annuity_service.get_bool_config.{key}",
                    log_window_seconds=300,
                )
    return ConfigService.get_bool(key, default)


def _get_datetime_config(key: str) -> datetime | None:
    """Fetch datetime config with safe fallback."""
    s = (ConfigService.get_str(key, None, strip=True, allow_blank=False) or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", ""))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_service.get_datetime_config",
            log_key=f"annuity_service.get_datetime_config.{key}",
            log_window_seconds=300,
        )
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def _parse_date(v) -> date | None:
    # Keep parsing consistent across app (docket/workflow/annuity).
    return _parse_date_value(v)


def _has_paid(paid_date) -> bool:
    """Type-safe check if paid_date is set (handles str/date/None)."""
    return _parse_date(paid_date) is not None


def revive_soft_deleted_annuity_item(item: AnnuityItem | None) -> bool:
    """Restore a soft-deleted annuity row in-place so cycle uniqueness remains usable."""
    if item is None or not bool(getattr(item, "is_deleted", False)):
        return False

    changed = False
    if bool(getattr(item, "is_deleted", False)):
        item.is_deleted = False
        changed = True

    for attr in ("deleted_at", "deleted_by", "delete_reason", "deleted_op_id"):
        if getattr(item, attr, None) is not None:
            setattr(item, attr, None)
            changed = True

    if changed:
        db.session.add(item)
    return changed


def soft_delete_annuity_item(
    item: AnnuityItem | None,
    *,
    reason: str,
    deleted_by: int | None = None,
    deleted_op_id: int | None = None,
    include_paid: bool = True,
) -> bool:
    """Soft-delete a user-visible annuity row without freeing its matter/cycle slot."""
    if item is None or bool(getattr(item, "is_deleted", False)):
        return False

    if not include_paid:
        try:
            is_paid = bool(getattr(item, "is_paid", False))
        except Exception:
            is_paid = _has_paid(getattr(item, "paid_date", None))
        if is_paid:
            return False

    changed = False
    now = datetime.utcnow()
    if not bool(getattr(item, "is_deleted", False)):
        item.is_deleted = True
        changed = True
    item.deleted_at = now
    changed = True
    if getattr(item, "delete_reason", None) != reason:
        item.delete_reason = reason
        changed = True
    if getattr(item, "deleted_by", None) != deleted_by:
        item.deleted_by = deleted_by
        changed = True
    if getattr(item, "deleted_op_id", None) != deleted_op_id:
        item.deleted_op_id = deleted_op_id
        changed = True

    if changed:
        db.session.add(item)
    return changed


def soft_delete_unpaid_annuity_item(item: AnnuityItem | None, *, reason: str) -> bool:
    """Soft-delete an unpaid annuity row when auto-generation rules no longer require it."""
    return soft_delete_annuity_item(
        item,
        reason=reason,
        deleted_by=None,
        deleted_op_id=None,
        include_paid=False,
    )


_USER_DELETE_REASONS = frozenset(
    {
        "case_annuity_delete",
        "renewal_fee_delete",
        "renewal_fee_bulk_delete",
    }
)


def _is_user_deleted_annuity_item(item: AnnuityItem | None) -> bool:
    if item is None or not bool(getattr(item, "is_deleted", False)):
        return False
    reason = (getattr(item, "delete_reason", None) or "").strip()
    return reason in _USER_DELETE_REASONS


def _is_auto_generated_annuity_item(item: AnnuityItem | None) -> bool:
    if item is None:
        return False
    memo = (getattr(item, "memo", None) or "").strip()
    return memo.startswith("[AutoCreate]") or '"auto": true' in memo.lower()


def _load_deadline_engine():
    try:
        from deadline_engine.annuities import compute_annual_fee_deadlines

        try:
            from deadline_engine.types import DeadlineCode
        except Exception:
            DeadlineCode = None
        return compute_annual_fee_deadlines, DeadlineCode
    except Exception:
        try:
            from app.utils.vendor_paths import ensure_deadline_engine_path

            ensure_deadline_engine_path()

            from deadline_engine.annuities import compute_annual_fee_deadlines

            try:
                from deadline_engine.types import DeadlineCode
            except Exception:
                DeadlineCode = None
            return compute_annual_fee_deadlines, DeadlineCode
        except Exception:
            logger.warning(
                "deadline_engine module not available; using registration-anniversary "
                "annuity deadline fallback"
            )
            return _compute_annual_fee_deadlines_fallback, None


@dataclass(frozen=True)
class _FallbackAnnuityDeadline:
    code: str
    name: str
    due: date


def _compute_annual_fee_deadlines_fallback(
    registration_date: date,
    year: int,
) -> list[_FallbackAnnuityDeadline]:
    """
    Minimal non-trademark annuity calculator used when the optional deadline_engine
    package is absent.

    The app's domestic policy treats cycles 1-3 as paid at registration and starts
    active tracking at cycle 4. For cycle N, the statutory deadline is the
    (N - 1)th anniversary of registration; the surcharge/grace date is 6 months
    later.
    """
    try:
        cycle_no = int(year)
    except Exception:
        return []
    if cycle_no <= 0:
        return []

    due = registration_date + relativedelta(years=cycle_no - 1)
    grace = due + relativedelta(months=6)
    return [
        _FallbackAnnuityDeadline(
            code="ANNUITY_DUE",
            name=f"{cycle_no}RenewalDeadline",
            due=due,
        ),
        _FallbackAnnuityDeadline(
            code="ANNUITY_GRACE",
            name=f"{cycle_no}RenewalGrace",
            due=grace,
        ),
    ]


def _compute_renewal_schedule(due_dt: date | None) -> tuple[str | None, str | None]:
    if not due_dt:
        return None, None
    notice_days = _get_int_config("DEADLINE_ANNUITY_RENEWAL_NOTICE_DAYS", 60)
    open_days = _get_int_config("DEADLINE_ANNUITY_RENEWAL_OPEN_DAYS", 30)
    notice = due_dt - timedelta(days=notice_days) if notice_days > 0 else None
    opened = due_dt - timedelta(days=open_days) if open_days > 0 else None
    return (opened.isoformat() if opened else None, notice.isoformat() if notice else None)


def _compute_trademark_renewal_schedule(due_dt: date | None) -> tuple[str | None, str | None]:
    if not due_dt:
        return None, None
    notice_days = _get_int_config("DEADLINE_TRADEMARK_RENEWAL_NOTICE_DAYS", 60)
    open_days = _get_int_config(
        "DEADLINE_TRADEMARK_RENEWAL_OPEN_DAYS",
        _TRADEMARK_RENEWAL_OPEN_DAYS_DEFAULT,
    )
    notice = due_dt - timedelta(days=notice_days) if notice_days > 0 else None
    opened = due_dt - timedelta(days=open_days) if open_days > 0 else None
    return (opened.isoformat() if opened else None, notice.isoformat() if notice else None)


def _compute_trademark_split_payment_schedule(due_dt: date | None) -> tuple[str | None, str | None]:
    if not due_dt:
        return None, None
    notice_days = _get_int_config(
        "DEADLINE_TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS",
        _TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS_DEFAULT,
    )
    open_days = _get_int_config(
        "DEADLINE_TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS",
        _TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS_DEFAULT,
    )
    notice = due_dt - timedelta(days=notice_days) if notice_days > 0 else None
    opened = due_dt - timedelta(days=open_days) if open_days > 0 else None
    return (opened.isoformat() if opened else None, notice.isoformat() if notice else None)


def _get_registration_date(matter_id: str, *, refresh: bool = False) -> date | None:
    """Get registration date via normalized facts (fallback computes & stores)."""
    try:
        from app.services.matter.matter_facts_service import get_registration_date

        return get_registration_date(matter_id, refresh=refresh)
    except Exception:
        # fallback: keep legacy behavior if facts table not ready
        from sqlalchemy import func

        from app.models.ip_records import MatterEvent

        mid = (matter_id or "").strip()
        if not mid:
            return None
        try:
            rows = (
                db.session.query(MatterEvent.event_at)
                .filter(MatterEvent.matter_id == mid)
                .filter(MatterEvent.event_key.in_(REGISTRATION_EVENT_KEYS))
                .filter(MatterEvent.event_at.isnot(None))
                .filter(func.trim(MatterEvent.event_at) != "")
                .order_by(MatterEvent.mevent_id.desc())
                .limit(20)
                .all()
            )
            for (event_at,) in rows or []:
                if event_at:
                    dt = _parse_date(event_at)
                    if dt:
                        _upsert_matter_facts(mid, reg_date=dt, reg_source="matter_event")
                        return dt
        except Exception as e:
            logger.debug("Failed to fetch registration from matter_event: %s", e)

        try:
            rows = MatterCustomField.query.filter_by(matter_id=mid).all()
            for r in rows:
                dt = find_first_date(
                    r.data or {}, REGISTRATION_CUSTOM_KEYS, key_substring="Registration date"
                )
                if dt:
                    _upsert_matter_facts(
                        mid,
                        reg_date=dt,
                        reg_source=f"custom_field:{getattr(r, 'namespace', '')}",
                    )
                    return dt
        except Exception as e:
            logger.debug("Failed to fetch registration from custom fields: %s", e)
        return None


def _get_term_expiry_date(matter_id: str) -> date | None:
    """Fetch the current term-expiry date, preferring the furthest future signal."""
    from sqlalchemy import func

    from app.models.ip_records import MatterEvent

    mid = (matter_id or "").strip()
    if not mid:
        return None

    dates: list[date] = []

    try:
        rows = (
            db.session.query(MatterEvent.event_at)
            .filter(MatterEvent.matter_id == mid)
            .filter(MatterEvent.event_key.in_(_TERM_EXPIRY_EVENT_KEYS))
            .filter(MatterEvent.event_at.isnot(None))
            .filter(func.trim(MatterEvent.event_at) != "")
            .order_by(MatterEvent.mevent_id.desc())
            .limit(50)
            .all()
        )
        for (event_at,) in rows or []:
            dt = _parse_date(event_at)
            if dt:
                dates.append(dt)
    except Exception:
        logger.debug("Failed to fetch term_expiry_date from matter_event", exc_info=True)

    try:
        rows = MatterCustomField.query.filter_by(matter_id=mid).all()
        for r in rows:
            dt = find_first_date(
                r.data or {},
                _TERM_EXPIRY_CUSTOM_KEYS,
                key_substring="Period",
            )
            if dt:
                dates.append(dt)
    except Exception:
        logger.debug("Failed to fetch term_expiry_date from custom fields", exc_info=True)

    try:
        matter = Matter.query.get(mid)
        if matter and (getattr(matter, "status_red", None) or "").strip() == "Term expired":
            dt = _parse_date(getattr(matter, "status_red_related_date", None))
            if dt:
                dates.append(dt)
    except Exception:
        logger.debug(
            "Failed to fetch term_expiry_date from matter.status_red_related_date", exc_info=True
        )

    return max(dates) if dates else None


def _get_reg_fee_paid_date(matter_id: str) -> date | None:
    """
    Best-effort: fetch "registration fee paid" date.

    NOTE: This is *not* a true registration date. It is only used as a conservative
    fallback for annuity auto-generation.
    """
    from sqlalchemy import func

    from app.models.ip_records import MatterEvent

    mid = (matter_id or "").strip()
    if not mid:
        return None

    try:
        rows = (
            db.session.query(MatterEvent.event_at)
            .filter(MatterEvent.matter_id == mid)
            .filter(MatterEvent.event_key.in_(REG_FEE_PAID_EVENT_KEYS))
            .filter(MatterEvent.event_at.isnot(None))
            .filter(func.trim(MatterEvent.event_at) != "")
            .order_by(MatterEvent.mevent_id.desc())
            .limit(20)
            .all()
        )
        for (event_at,) in rows or []:
            if event_at:
                dt = _parse_date(event_at)
                if dt:
                    return dt
    except Exception:
        logger.debug("Failed to fetch reg_fee_paid_date from matter_event", exc_info=True)

    try:
        rows = MatterCustomField.query.filter_by(matter_id=mid).all()
        for r in rows:
            dt = find_first_date(
                r.data or {}, REG_FEE_PAID_CUSTOM_KEYS, key_substring="RegistrationPayment"
            )
            if dt:
                return dt
    except Exception:
        logger.debug("Failed to fetch reg_fee_paid_date from custom fields", exc_info=True)
    return None


def _allow_reg_fee_paid_fallback_for_matter(
    matter: Matter | None,
    right_type: str | None,
) -> bool:
    """Return whether reg_fee_paid_date may be used as an annuity base date."""
    if _get_bool_config("ANNUITY_ALLOW_REG_FEE_PAID_FALLBACK", False):
        return True
    if not matter or right_type != "TRADEMARK":
        return False
    try:
        return bool(is_uspto_managed_matter(matter))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_service.reg_fee_paid_fallback.uspto_check",
            log_key="annuity_service.reg_fee_paid_fallback.uspto_check",
            log_window_seconds=300,
        )
        return False


def _get_custom_field_value(
    matter_id: str,
    field_keys: tuple[str, ...],
) -> str | None:
    mid = (matter_id or "").strip()
    if not mid or not field_keys:
        return None
    try:
        rows = MatterCustomField.query.filter_by(matter_id=mid).all()
    except Exception:
        logger.debug("Failed to fetch custom field rows for %s", mid, exc_info=True)
        return None

    for row in rows or []:
        payload = row.data or {}
        if not isinstance(payload, dict):
            continue
        for key in field_keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def _upsert_matter_facts(
    matter_id: str,
    *,
    reg_date: date | None = None,
    reg_source: str | None = None,
    right_type_norm: str | None = None,
) -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False
    try:
        changed = False
        mf = MatterFacts.query.get(mid)
        if not mf:
            mf = MatterFacts(matter_id=mid)
            db.session.add(mf)
            changed = True
        if reg_date and mf.registration_date != reg_date:
            mf.registration_date = reg_date
            changed = True
        if reg_source:
            mf.registration_date_source = (reg_source or "").strip() or None
            changed = True
        if right_type_norm and mf.right_type_norm != right_type_norm:
            mf.right_type_norm = right_type_norm
            changed = True
        db.session.add(mf)
        return changed
    except Exception:
        # facts table might not exist yet (pre-migration). ignore.
        logger.debug("Skipping matter_facts upsert (table may not exist yet)", exc_info=True)
        return False


def _backfill_matter_facts_for_annuities(*, limit: int = 0) -> int:
    """
    Fill missing matter_facts rows (registration_date / right_type_norm) for matters
    that *appear* registered (event/custom-field scan), but are not yet normalized.
    """
    from sqlalchemy import Text, cast, false, func, or_, select

    from app.models.ip_records import MatterEvent

    if limit <= 0:
        limit = _get_int_config("ANNUITY_BACKFILL_LIMIT", 0)
    lookback_days = _get_int_config("ANNUITY_BACKFILL_LOOKBACK_DAYS", 0)
    if lookback_days < 0:
        lookback_days = 0
    cutoff = datetime.utcnow() - timedelta(days=lookback_days) if lookback_days > 0 else None
    include_events = lookback_days <= 0
    if not include_events:
        logger.debug(
            "Skipping matter_event backfill scan: ANNUITY_BACKFILL_LOOKBACK_DAYS=%s",
            lookback_days,
        )

    dialect = getattr(db.engine.dialect, "name", "")
    mids: list[str] = []

    if dialect == "postgresql":
        try:
            event_sel = None
            if include_events:
                event_sel = (
                    select(MatterEvent.matter_id.label("matter_id"))
                    .where(
                        MatterEvent.event_key.in_(REGISTRATION_EVENT_KEYS + REG_FEE_PAID_EVENT_KEYS)
                    )
                    .where(MatterEvent.event_at.isnot(None))
                    .where(func.trim(MatterEvent.event_at) != "")
                    .distinct()
                )

            data_text = cast(MatterCustomField.data, Text)
            custom_conditions = [
                data_text.like(f"%{k}%")
                for k in (REGISTRATION_CUSTOM_KEYS + REG_FEE_PAID_CUSTOM_KEYS)
            ]
            custom_sel = (
                select(MatterCustomField.matter_id.label("matter_id"))
                .where(MatterCustomField.data.isnot(None))
                .where(or_(*custom_conditions) if custom_conditions else false())
                .distinct()
            )
            if cutoff:
                custom_sel = custom_sel.where(MatterCustomField.updated_at >= cutoff)

            union_sel = event_sel.union(custom_sel) if event_sel is not None else custom_sel
            candidates = union_sel.cte("candidates")
            stmt = (
                select(candidates.c.matter_id)
                .select_from(
                    candidates.outerjoin(
                        MatterFacts, MatterFacts.matter_id == candidates.c.matter_id
                    )
                )
                .where(
                    or_(
                        MatterFacts.matter_id.is_(None),
                        MatterFacts.registration_date.is_(None),
                        MatterFacts.right_type_norm.is_(None),
                    )
                )
            )
            if limit > 0:
                stmt = stmt.limit(limit)

            rows = db.session.execute(stmt).all()
            mids = [str(r[0]) for r in rows if r and r[0]]
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_service.sync_matter_facts.query",
                log_key="annuity_service.sync_matter_facts.query",
                log_window_seconds=300,
            )
            return 0
    else:
        try:
            seen = set()

            def _add_mid(val):
                if not val:
                    return False
                s = str(val)
                if s in seen:
                    return False
                seen.add(s)
                mids.append(s)
                return True

            if include_events:
                q = (
                    db.session.query(MatterEvent.matter_id)
                    .outerjoin(MatterFacts, MatterFacts.matter_id == MatterEvent.matter_id)
                    .filter(
                        MatterEvent.event_key.in_(REGISTRATION_EVENT_KEYS + REG_FEE_PAID_EVENT_KEYS)
                    )
                    .filter(MatterEvent.event_at.isnot(None))
                    .filter(func.trim(MatterEvent.event_at) != "")
                    .filter(
                        or_(
                            MatterFacts.matter_id.is_(None),
                            MatterFacts.registration_date.is_(None),
                            MatterFacts.right_type_norm.is_(None),
                        )
                    )
                    .distinct()
                )
                if limit > 0:
                    q = q.limit(limit)
                for (mid_val,) in q.all():
                    if _add_mid(mid_val) and limit > 0 and len(mids) >= limit:
                        break
            if limit <= 0 or len(mids) < limit:
                q = (
                    db.session.query(MatterCustomField.matter_id, MatterCustomField.data)
                    .outerjoin(MatterFacts, MatterFacts.matter_id == MatterCustomField.matter_id)
                    .filter(
                        or_(
                            MatterFacts.matter_id.is_(None),
                            MatterFacts.registration_date.is_(None),
                            MatterFacts.right_type_norm.is_(None),
                        )
                    )
                )
                if cutoff:
                    q = q.filter(MatterCustomField.updated_at >= cutoff)
                if limit > 0:
                    q = q.limit(max(limit - len(mids), 0) or limit)
                for mid_val, data in q.all():
                    if limit > 0 and len(mids) >= limit:
                        break
                    if not data:
                        continue
                    if data_has_any_key(
                        data,
                        REGISTRATION_CUSTOM_KEYS + REG_FEE_PAID_CUSTOM_KEYS,
                        key_substring="Registration date",
                    ):
                        if _add_mid(mid_val) and limit > 0 and len(mids) >= limit:
                            break
        except Exception:
            return 0

    updated = 0
    for mid in mids:
        try:
            matter = Matter.query.get(str(mid))
            if not matter:
                continue
            rt = _infer_right_type(matter)
            rd = _get_registration_date(str(mid))
            reg_source = None
            if not rd and _allow_reg_fee_paid_fallback_for_matter(matter, rt):
                rd = _get_reg_fee_paid_date(str(mid))
                if rd:
                    reg_source = "reg_fee_paid_date_fallback"
            if rt or rd:
                _upsert_matter_facts(
                    str(mid),
                    reg_date=rd,
                    reg_source=reg_source,
                    right_type_norm=rt,
                )
                updated += 1
            if updated % 200 == 0:
                db.session.commit()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_service.sync_matter_facts.loop",
                log_key="annuity_service.sync_matter_facts.loop",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="annuity_service.sync_matter_facts.rollback",
                    log_key="annuity_service.sync_matter_facts.rollback",
                    log_window_seconds=300,
                )
            continue
    try:
        db.session.commit()
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_service.sync_matter_facts.commit",
            log_key="annuity_service.sync_matter_facts.commit",
            log_window_seconds=300,
        )
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="annuity_service.sync_matter_facts.rollback",
                log_key="annuity_service.sync_matter_facts.rollback",
                log_window_seconds=300,
            )
    return updated


def _infer_right_type(matter: Matter) -> str | None:
    """Infer right type from matter_type or our_ref."""

    def _normalize_right_type(raw: str | None) -> str | None:
        if not raw:
            return None
        s = str(raw).strip()
        if not s:
            return None
        s_upper = s.upper()
        s_compact = re.sub(r"[^A-Z0-9-]", "", s_upper)
        if "Patent" in s or "PATENT" in s_compact or s_compact in {"PAT", "PT"}:
            return "PATENT"
        if "UTILITY" in s_compact or s_compact in {"UM", "UTILITYMODEL"}:
            return "UTILITY"
        if "Design" in s or "DESIGN" in s_compact or s_compact in {"DES"}:
            return "DESIGN"
        if "Trademark" in s or "TRADEMARK" in s_compact or s_compact in {"TM", "MARK"}:
            return "TRADEMARK"
        return None

    _division, profile_type = resolve_profile_case_kind(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
    )

    for raw in (profile_type, matter.matter_type, matter.right_group):
        normalized = _normalize_right_type(raw)
        if normalized:
            return normalized

    # Infer from our_ref pattern (e.g., 24PD0001US -> PATENT, 24UD... -> UTILITY)
    our_ref = (matter.our_ref or "").strip().upper()
    if len(our_ref) >= 4 and our_ref[:2].isdigit():
        code = our_ref[2:4]
        if code.startswith("P"):
            return "PATENT"
        elif code.startswith("U"):
            return "UTILITY"
        elif code.startswith("D"):
            return "DESIGN"
        elif code.startswith("T"):
            return "TRADEMARK"

    return None


def _normalize_trademark_registration_payment_term(value: object | None) -> int | None:
    if value is None:
        return None

    text = str(value).strip().upper()
    if not text:
        return None

    if "10" in text:
        return 10
    if re.search(r"(^|[^0-9])5($|[^0-9])", text):
        return 5

    try:
        parsed = int(text)
    except Exception:
        return None
    return parsed if parsed in (5, 10) else None


def _get_trademark_registration_payment_term(matter: Matter) -> int:
    mid = (getattr(matter, "matter_id", None) or "").strip()
    if mid:
        explicit = _normalize_trademark_registration_payment_term(
            _get_custom_field_value(mid, ("tm_registration_payment_term",))
        )
        if explicit:
            return explicit

        try:
            existing_items = AnnuityItem.query.filter(
                AnnuityItem.matter_id == mid,
                AnnuityItem.cycle_no.isnot(None),
            ).all()
        except Exception:
            existing_items = []
        for row in existing_items or []:
            cycle_no = getattr(row, "cycle_no", None)
            if cycle_no and int(cycle_no) % 10 == _TRADEMARK_SPLIT_PAYMENT_YEARS:
                if not bool(getattr(row, "is_deleted", False)):
                    return _TRADEMARK_SPLIT_PAYMENT_YEARS

    configured = _normalize_trademark_registration_payment_term(
        _get_int_config(
            "DEADLINE_TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT",
            _TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT,
        )
    )
    return configured or _TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT


def _get_default_owner(matter: Matter) -> str | None:
    """Get default owner for annuity (manager > attorney > null)."""
    from app.models.case_flat_index import CaseFlatIndex
    from app.models.user import User

    idx = CaseFlatIndex.query.get(matter.matter_id)
    if idx:
        # Prefer manager, then attorney
        for user_id in (idx.manager_id, idx.attorney_id):
            if user_id:
                user = User.query.get(user_id)
                if user and user.staff_party_id:
                    return user.staff_party_id
    return None


def _get_term_years(right_type: str) -> int:
    """Get the term years for annuity generation based on right type."""
    if right_type == "PATENT":
        return _get_int_config("DEADLINE_DEFAULT_PATENT_TERM", 20)
    elif right_type == "UTILITY":
        return _get_int_config("DEADLINE_DEFAULT_UTILITY_TERM", 10)
    elif right_type == "DESIGN":
        return _get_int_config("DEADLINE_DEFAULT_DESIGN_TERM", 20)
    elif right_type == "TRADEMARK":
        return _get_int_config("DEADLINE_DEFAULT_TRADEMARK_TERM", _TRADEMARK_RENEWAL_TERM_YEARS)
    return DEFAULT_ANNUITY_YEARS


def _annuity_year_from_registration(reg_date: date, *, today: date | None = None) -> int:
    """
    Return the annuity year number counted from the registration date anniversary (1-based).

    Example: reg_date=2022-11-04, today=2026-01-22 -> year 4.
    """
    today = today or date.today()
    if today < reg_date:
        return 1
    years = today.year - reg_date.year
    if (today.month, today.day) < (reg_date.month, reg_date.day):
        years -= 1
    return years + 1


def _trademark_cycle_no_from_dates(reg_date: date, term_expiry_date: date) -> int:
    years = term_expiry_date.year - reg_date.year
    if (term_expiry_date.month, term_expiry_date.day) < (reg_date.month, reg_date.day):
        years -= 1
    return max(years, 1)


def _resolve_trademark_term_expiry_date(matter_id: str, reg_date: date) -> date:
    term_expiry_date = _get_term_expiry_date(matter_id)
    if not term_expiry_date:
        term_expiry_date = reg_date + relativedelta(years=_get_term_years("TRADEMARK"))
    return term_expiry_date


def _ensure_trademark_split_payment_item(
    *,
    matter: Matter,
    reg_date: date,
    term_expiry_date: date,
    payment_term_years: int,
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return 0, False

    term_cycle_no = _trademark_cycle_no_from_dates(reg_date, term_expiry_date)
    if term_cycle_no <= _TRADEMARK_SPLIT_PAYMENT_YEARS:
        split_due_date = term_expiry_date
    else:
        split_due_date = term_expiry_date - relativedelta(years=_TRADEMARK_SPLIT_PAYMENT_YEARS)
    cycle_no = _trademark_cycle_no_from_dates(reg_date, split_due_date)
    row = (
        AnnuityItem.query.filter_by(matter_id=mid, cycle_no=cycle_no)
        .order_by(AnnuityItem.annuity_id.desc())
        .first()
    )

    should_have_split_payment = (
        payment_term_years == _TRADEMARK_SPLIT_PAYMENT_YEARS
        or term_cycle_no <= _TRADEMARK_SPLIT_PAYMENT_YEARS
    )

    if start_year is not None and cycle_no < int(start_year):
        return 0, False
    if end_year is not None and cycle_no > int(end_year):
        return 0, False

    if not should_have_split_payment:
        if row and soft_delete_unpaid_annuity_item(
            row,
            reason="auto_reconcile:trademark_registration_payment_term",
        ):
            return 1, True
        return 0, False

    due_str = split_due_date.isoformat()
    renewal_open_str, renewal_notice_str = _compute_trademark_split_payment_schedule(split_due_date)
    default_owner = _get_default_owner(matter)

    if row:
        if _is_user_deleted_annuity_item(row):
            return 0, False
        changed = revive_soft_deleted_annuity_item(row)
        is_paid = False
        try:
            is_paid = bool(getattr(row, "is_paid", False))
        except Exception:
            is_paid = _has_paid(getattr(row, "paid_date", None))
        if not is_paid:
            if (row.due_date or "").split("T")[0] != due_str:
                row.due_date = due_str
                changed = True
            if (row.extended_due_date or "").split("T")[0]:
                row.extended_due_date = None
                changed = True
            if (row.renewal_open_date or "").split("T")[0] != (renewal_open_str or ""):
                row.renewal_open_date = renewal_open_str
                changed = True
            if (row.renewal_notice_due or "").split("T")[0] != (renewal_notice_str or ""):
                row.renewal_notice_due = renewal_notice_str
                changed = True
        if default_owner and (row.owner_staff_party_id or "").strip() != default_owner:
            row.owner_staff_party_id = default_owner
            changed = True
        if changed:
            db.session.add(row)
            return 1, True
        return 0, False

    item = AnnuityItem(
        matter_id=mid,
        cycle_no=cycle_no,
        annuity_status="pending",
        due_date=due_str,
        extended_due_date=None,
        renewal_open_date=renewal_open_str,
        renewal_notice_due=renewal_notice_str,
        owner_staff_party_id=default_owner,
        memo="[AutoCreate] "
        + json.dumps(
            {
                "auto": True,
                "generated_at": date.today().isoformat(),
                "registration_date": reg_date.isoformat(),
                "term_expiry_date": term_expiry_date.isoformat(),
                "split_due_date": split_due_date.isoformat(),
                "payment_term_years": payment_term_years,
                "right_type": "TRADEMARK",
                "renewal_type": "TRADEMARK_SPLIT_PAYMENT",
            },
            ensure_ascii=False,
        ),
    )
    db.session.add(item)
    return 1, True


def _ensure_trademark_renewal_item(
    *,
    matter: Matter,
    reg_date: date,
    term_expiry_date: date,
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return 0, False

    cycle_no = _trademark_cycle_no_from_dates(reg_date, term_expiry_date)
    if cycle_no <= _TRADEMARK_SPLIT_PAYMENT_YEARS:
        return 0, False
    if start_year is not None and cycle_no < int(start_year):
        return 0, False
    if end_year is not None and cycle_no > int(end_year):
        return 0, False

    due_str = term_expiry_date.isoformat()
    extended_due = term_expiry_date + relativedelta(months=_TRADEMARK_RENEWAL_GRACE_MONTHS)
    extended_str = extended_due.isoformat()
    renewal_open_str, renewal_notice_str = _compute_trademark_renewal_schedule(term_expiry_date)
    default_owner = _get_default_owner(matter)

    row = (
        AnnuityItem.query.filter_by(matter_id=mid, cycle_no=cycle_no)
        .order_by(AnnuityItem.annuity_id.desc())
        .first()
    )
    if row:
        if _is_user_deleted_annuity_item(row):
            return 0, False
        changed = revive_soft_deleted_annuity_item(row)
        is_paid = False
        try:
            is_paid = bool(getattr(row, "is_paid", False))
        except Exception:
            is_paid = _has_paid(getattr(row, "paid_date", None))
        if not is_paid:
            if (row.due_date or "").split("T")[0] != due_str:
                row.due_date = due_str
                changed = True
            if (row.extended_due_date or "").split("T")[0] != extended_str:
                row.extended_due_date = extended_str
                changed = True
            if (row.renewal_open_date or "").split("T")[0] != (renewal_open_str or ""):
                row.renewal_open_date = renewal_open_str
                changed = True
            if (row.renewal_notice_due or "").split("T")[0] != (renewal_notice_str or ""):
                row.renewal_notice_due = renewal_notice_str
                changed = True
        if default_owner and (row.owner_staff_party_id or "").strip() != default_owner:
            row.owner_staff_party_id = default_owner
            changed = True
        if changed:
            db.session.add(row)
            return 1, True
        return 0, False

    item = AnnuityItem(
        matter_id=mid,
        cycle_no=cycle_no,
        annuity_status="pending",
        due_date=due_str,
        extended_due_date=extended_str,
        renewal_open_date=renewal_open_str,
        renewal_notice_due=renewal_notice_str,
        owner_staff_party_id=default_owner,
        memo="[AutoCreate] "
        + json.dumps(
            {
                "auto": True,
                "generated_at": date.today().isoformat(),
                "registration_date": reg_date.isoformat(),
                "term_expiry_date": term_expiry_date.isoformat(),
                "right_type": "TRADEMARK",
                "renewal_type": "TRADEMARK",
            },
            ensure_ascii=False,
        ),
    )
    db.session.add(item)
    return 1, True


def _resolve_uspto_trademark_term_expiry_date(matter_id: str, reg_date: date) -> date:
    term_expiry_date = _get_term_expiry_date(matter_id)
    if term_expiry_date:
        cycle_no = _trademark_cycle_no_from_dates(reg_date, term_expiry_date)
        if cycle_no >= _TRADEMARK_RENEWAL_TERM_YEARS and cycle_no % 10 == 0:
            return term_expiry_date
    return reg_date + relativedelta(years=_TRADEMARK_RENEWAL_TERM_YEARS)


def _compute_uspto_trademark_schedule(
    *,
    window_open_dt: date,
    due_dt: date,
) -> tuple[str | None, str | None, str | None]:
    notice_days = _get_int_config("DEADLINE_TRADEMARK_RENEWAL_NOTICE_DAYS", 60)
    notice = due_dt - timedelta(days=notice_days) if notice_days > 0 else None
    extended = due_dt + relativedelta(months=_TRADEMARK_RENEWAL_GRACE_MONTHS)
    return (
        window_open_dt.isoformat(),
        notice.isoformat() if notice else None,
        extended.isoformat(),
    )


def _ensure_uspto_trademark_item(
    *,
    matter: Matter,
    reg_date: date,
    cycle_no: int,
    due_date: date,
    window_open_date: date,
    renewal_type: str,
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return 0, False
    if not _within_requested_cycle_range(
        cycle_no,
        start_year=start_year,
        end_year=end_year,
    ):
        return 0, False

    row = (
        AnnuityItem.query.filter_by(matter_id=mid, cycle_no=cycle_no)
        .order_by(AnnuityItem.annuity_id.desc())
        .first()
    )
    if row and _is_user_deleted_annuity_item(row):
        return 0, False

    renewal_open_str, renewal_notice_str, extended_str = _compute_uspto_trademark_schedule(
        window_open_dt=window_open_date,
        due_dt=due_date,
    )
    due_str = due_date.isoformat()
    default_owner = _get_default_owner(matter)

    if row:
        changed = revive_soft_deleted_annuity_item(row)
        try:
            is_paid = bool(getattr(row, "is_paid", False))
        except Exception:
            is_paid = _has_paid(getattr(row, "paid_date", None))
        if not is_paid:
            if (row.due_date or "").split("T")[0] != due_str:
                row.due_date = due_str
                changed = True
            if (row.extended_due_date or "").split("T")[0] != extended_str:
                row.extended_due_date = extended_str
                changed = True
            if (row.renewal_open_date or "").split("T")[0] != (renewal_open_str or ""):
                row.renewal_open_date = renewal_open_str
                changed = True
            if (row.renewal_notice_due or "").split("T")[0] != (renewal_notice_str or ""):
                row.renewal_notice_due = renewal_notice_str
                changed = True
        if default_owner and (row.owner_staff_party_id or "").strip() != default_owner:
            row.owner_staff_party_id = default_owner
            changed = True
        if changed:
            db.session.add(row)
            return 1, True
        return 0, False

    item = AnnuityItem(
        matter_id=mid,
        cycle_no=cycle_no,
        annuity_status="pending",
        due_date=due_str,
        extended_due_date=extended_str,
        renewal_open_date=renewal_open_str,
        renewal_notice_due=renewal_notice_str,
        owner_staff_party_id=default_owner,
        memo="[AutoCreate] "
        + json.dumps(
            {
                "auto": True,
                "generated_at": date.today().isoformat(),
                "registration_date": reg_date.isoformat(),
                "window_open_date": window_open_date.isoformat(),
                "right_type": "TRADEMARK",
                "jurisdiction": "USPTO",
                "renewal_type": renewal_type,
            },
            ensure_ascii=False,
        ),
    )
    db.session.add(item)
    return 1, True


def _ensure_uspto_trademark_annuity_items(
    *,
    matter: Matter,
    reg_date: date,
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return 0, False

    section8_cycle_no = 6
    section8_due = reg_date + relativedelta(years=6)
    section8_open = reg_date + relativedelta(years=5)

    renewal_due = _resolve_uspto_trademark_term_expiry_date(mid, reg_date)
    renewal_cycle_no = _trademark_cycle_no_from_dates(reg_date, renewal_due)
    if renewal_cycle_no < _TRADEMARK_RENEWAL_TERM_YEARS:
        renewal_cycle_no = _TRADEMARK_RENEWAL_TERM_YEARS
        renewal_due = reg_date + relativedelta(years=_TRADEMARK_RENEWAL_TERM_YEARS)
    renewal_open = renewal_due - relativedelta(years=1)

    expected_cycle_nos: set[int] = set()
    for cycle_no in (section8_cycle_no, renewal_cycle_no):
        if _within_requested_cycle_range(
            cycle_no,
            start_year=start_year,
            end_year=end_year,
        ):
            expected_cycle_nos.add(cycle_no)

    count = 0
    changed = False

    section8_count, section8_changed = _ensure_uspto_trademark_item(
        matter=matter,
        reg_date=reg_date,
        cycle_no=section8_cycle_no,
        due_date=section8_due,
        window_open_date=section8_open,
        renewal_type="USPTO_SECTION_8",
        start_year=start_year,
        end_year=end_year,
    )
    count += section8_count
    changed = section8_changed or changed

    renewal_count, renewal_changed = _ensure_uspto_trademark_item(
        matter=matter,
        reg_date=reg_date,
        cycle_no=renewal_cycle_no,
        due_date=renewal_due,
        window_open_date=renewal_open,
        renewal_type="USPTO_SECTION_8_9",
        start_year=start_year,
        end_year=end_year,
    )
    count += renewal_count
    changed = renewal_changed or changed

    stale_count, stale_changed = _soft_delete_stale_trademark_annuity_items(
        matter_id=mid,
        expected_cycle_nos=expected_cycle_nos,
        start_year=start_year,
        end_year=end_year,
    )
    count += stale_count
    changed = stale_changed or changed

    return count, changed


def _within_requested_cycle_range(
    cycle_no: int,
    *,
    start_year: int | None,
    end_year: int | None,
) -> bool:
    if start_year is not None and cycle_no < int(start_year):
        return False
    if end_year is not None and cycle_no > int(end_year):
        return False
    return True


def _soft_delete_stale_trademark_annuity_items(
    *,
    matter_id: str,
    expected_cycle_nos: set[int],
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    if not matter_id or not expected_cycle_nos:
        return 0, False

    rows = (
        AnnuityItem.query.filter(AnnuityItem.matter_id == matter_id)
        .filter(AnnuityItem.cycle_no.isnot(None))
        .all()
    )

    count = 0
    changed = False
    for row in rows:
        if bool(getattr(row, "is_deleted", False)):
            continue
        try:
            cycle_no = int(getattr(row, "cycle_no", None) or 0)
        except Exception:
            continue
        if cycle_no <= 0 or cycle_no in expected_cycle_nos:
            continue
        if not _within_requested_cycle_range(
            cycle_no,
            start_year=start_year,
            end_year=end_year,
        ):
            continue
        if not _is_auto_generated_annuity_item(row):
            continue
        if soft_delete_unpaid_annuity_item(
            row,
            reason="auto_reconcile:trademark_term_expiry_date",
        ):
            count += 1
            changed = True

    return count, changed


def _ensure_trademark_annuity_items(
    *,
    matter: Matter,
    reg_date: date,
    start_year: int | None,
    end_year: int | None,
) -> tuple[int, bool]:
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return 0, False

    term_expiry_date = _resolve_trademark_term_expiry_date(mid, reg_date)
    payment_term_years = _get_trademark_registration_payment_term(matter)
    expected_cycle_nos: set[int] = set()
    renewal_cycle_no = _trademark_cycle_no_from_dates(reg_date, term_expiry_date)

    if renewal_cycle_no <= _TRADEMARK_SPLIT_PAYMENT_YEARS:
        if _within_requested_cycle_range(
            renewal_cycle_no,
            start_year=start_year,
            end_year=end_year,
        ):
            expected_cycle_nos.add(renewal_cycle_no)
    elif payment_term_years == _TRADEMARK_SPLIT_PAYMENT_YEARS:
        split_due_date = term_expiry_date - relativedelta(years=_TRADEMARK_SPLIT_PAYMENT_YEARS)
        split_cycle_no = _trademark_cycle_no_from_dates(reg_date, split_due_date)
        if _within_requested_cycle_range(
            split_cycle_no,
            start_year=start_year,
            end_year=end_year,
        ):
            expected_cycle_nos.add(split_cycle_no)
        if _within_requested_cycle_range(
            renewal_cycle_no,
            start_year=start_year,
            end_year=end_year,
        ):
            expected_cycle_nos.add(renewal_cycle_no)
    elif _within_requested_cycle_range(
        renewal_cycle_no,
        start_year=start_year,
        end_year=end_year,
    ):
        expected_cycle_nos.add(renewal_cycle_no)

    count = 0
    changed = False

    split_count, split_changed = _ensure_trademark_split_payment_item(
        matter=matter,
        reg_date=reg_date,
        term_expiry_date=term_expiry_date,
        payment_term_years=payment_term_years,
        start_year=start_year,
        end_year=end_year,
    )
    count += split_count
    changed = split_changed or changed

    renewal_count, renewal_changed = _ensure_trademark_renewal_item(
        matter=matter,
        reg_date=reg_date,
        term_expiry_date=term_expiry_date,
        start_year=start_year,
        end_year=end_year,
    )
    count += renewal_count
    changed = renewal_changed or changed

    stale_count, stale_changed = _soft_delete_stale_trademark_annuity_items(
        matter_id=mid,
        expected_cycle_nos=expected_cycle_nos,
        start_year=start_year,
        end_year=end_year,
    )
    count += stale_count
    changed = stale_changed or changed

    return count, changed


def ensure_annuities_for_matter(
    matter_id: str,
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    refresh_registration_date: bool = False,
    commit: bool = True,
) -> int:
    """
    Ensure AnnuityItem records exist for a registered matter.

    Args:
        matter_id: The matter to process
        start_year: First year to generate (default 1, includes years paid at registration)
        end_year: Last year to generate (default: full term when enabled)
        commit: If True, commit the transaction

    Returns:
        Number of annuity items created or updated
    """
    PREPAID_YEARS_AT_REGISTRATION_DOMESTIC = 3

    def _is_domestic_case(matter: Matter) -> bool:
        division, _typ = resolve_profile_case_kind(
            getattr(matter, "right_group", None),
            getattr(matter, "matter_type", None),
        )
        if division == "DOM":
            return True
        if division in ("OUT", "INC"):
            return False
        # Heuristic fallback (many firms encode domestic matters in the country suffix).
        our_ref = (getattr(matter, "our_ref", None) or "").strip().upper()
        if our_ref.endswith("US"):
            return True
        # Fallback to right_name keyword when division is missing.
        if "Domestic" in ((getattr(matter, "right_name", None) or "").strip()):
            return True
        return False

    mid = (matter_id or "").strip()
    if not mid:
        return 0

    matter = Matter.query.get(mid)
    if not matter:
        logger.debug(f"Matter not found: {mid}")
        return 0

    from app.services.annuity.annuity_management import is_annuity_management_disabled_for_matter

    if is_annuity_management_disabled_for_matter(mid):
        logger.info("Skipping annuity auto-gen for %s: annuity management disabled", matter.our_ref)
        return 0

    # Guard: avoid applying domestic maintenance-fee defaults to foreign matters unless explicitly enabled.
    effective_division, _effective_type = resolve_profile_case_kind(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
    )
    if effective_division in ("OUT", "INC"):
        if not _get_bool_config("ANNUITY_ALLOW_FOREIGN", False):
            logger.info(
                f"Skipping foreign annuity auto-gen for {matter.our_ref}: ANNUITY_ALLOW_FOREIGN=0"
            )
            return 0

    # Check right type
    right_type = _infer_right_type(matter)
    if not right_type or right_type not in ANNUITY_RIGHT_TYPES:
        logger.debug(f"Matter {matter.our_ref} is not an annuity-eligible type: {right_type}")
        return 0

    facts_changed = False

    # Get registration date
    refresh_reg = bool(refresh_registration_date)
    if not refresh_reg:
        # If previously generated using a fallback date, refresh to detect whether a
        # real registration_date has since been populated under supported keys.
        try:
            mf = MatterFacts.query.get(mid)
            if (
                getattr(mf, "registration_date_source", "") or ""
            ).strip() == "reg_fee_paid_date_fallback":
                refresh_reg = True
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_service.registration_date_source_refresh_check",
                log_key="annuity_service.registration_date_source_refresh_check",
                log_window_seconds=300,
            )

    reg_date = _get_registration_date(mid, refresh=refresh_reg)
    if not reg_date:
        if _allow_reg_fee_paid_fallback_for_matter(matter, right_type):
            fallback_dt = _get_reg_fee_paid_date(mid)
            if fallback_dt:
                logger.warning(
                    "Using reg_fee_paid_date as registration_date fallback for %s (%s)",
                    matter.our_ref,
                    fallback_dt,
                )
                reg_date = fallback_dt
                facts_changed = (
                    bool(
                        _upsert_matter_facts(
                            mid,
                            reg_date=reg_date,
                            reg_source="reg_fee_paid_date_fallback",
                            right_type_norm=right_type,
                        )
                    )
                    or facts_changed
                )

        if not reg_date:
            logger.debug(f"No registration date found for {matter.our_ref}")
            return 0

    # Normalize facts (right_type + reg_date)
    facts_changed = (
        bool(_upsert_matter_facts(mid, reg_date=reg_date, right_type_norm=right_type))
        or facts_changed
    )

    if right_type == "TRADEMARK":
        if is_uspto_managed_matter(matter):
            count, matter_changed = _ensure_uspto_trademark_annuity_items(
                matter=matter,
                reg_date=reg_date,
                start_year=start_year,
                end_year=end_year,
            )
        else:
            count, matter_changed = _ensure_trademark_annuity_items(
                matter=matter,
                reg_date=reg_date,
                start_year=start_year,
                end_year=end_year,
            )

        if matter_changed:
            enqueue_annuity_sync_for_matter(mid)

        if commit and (count > 0 or facts_changed):
            try:
                db.session.commit()
                if count > 0:
                    logger.info(
                        "Generated/updated %s trademark annuity items for %s",
                        count,
                        matter.our_ref,
                    )
                elif facts_changed:
                    logger.info(
                        "Updated matter_facts for %s (no trademark renewal rows changed)",
                        matter.our_ref,
                    )
            except Exception:
                db.session.rollback()
                raise

        return count

    # Determine year range
    if start_year is None or start_year <= 0:
        start_year = _get_int_config("DEADLINE_DEFAULT_ANNUITY_START_YEAR", 1)
    if start_year <= 0:
        start_year = 1

    # Domestic maintenance-fee practice: the first configured cycles may be prepaid at registration.
    # Even if older data already contains 1-3 as pending, treat them as paid and
    # start tracking annuities from year 4 by default.
    prepaid_years = PREPAID_YEARS_AT_REGISTRATION_DOMESTIC if _is_domestic_case(matter) else 0
    if prepaid_years > 0 and start_year <= prepaid_years:
        start_year = prepaid_years + 1

    # If annuity management starts mid-stream (e.g., rights transfer / partial import),
    # avoid backfilling earlier cycles automatically. Respect the earliest existing
    # cycle_no for this matter when it is later than the configured start_year.
    try:
        from sqlalchemy import func, or_

        min_existing = (
            db.session.query(func.min(AnnuityItem.cycle_no))
            .filter(
                AnnuityItem.matter_id == mid,
                AnnuityItem.cycle_no.isnot(None),
                or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None)),
            )
            .scalar()
        )
        if min_existing is not None:
            try:
                min_existing_int = int(min_existing)
            except Exception:
                min_existing_int = 0
            if min_existing_int > 0 and min_existing_int > start_year:
                logger.info(
                    "Adjusting annuity start_year for %s: start_year=%s -> %s (existing_min_cycle)",
                    matter.our_ref,
                    start_year,
                    min_existing_int,
                )
                start_year = min_existing_int
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_service.start_year.respect_existing_min_cycle",
            log_key="annuity_service.start_year.respect_existing_min_cycle",
            log_window_seconds=300,
        )
    full_term = _get_bool_config("DEADLINE_ANNUITY_FULL_TERM", True)
    term_years = _get_term_years(right_type)
    if end_year is not None and end_year <= 0:
        end_year = None
    if end_year is None:
        if full_term:
            end_year = term_years
        else:
            future_years = _get_int_config("DEADLINE_ANNUITY_AUTOGEN_FUTURE_YEARS", 3)
            if future_years < 0:
                future_years = 0
            current_year = _annuity_year_from_registration(reg_date)
            # Ensure at least one "next" annuity exists even when current_year < start_year.
            end_year = min(term_years, max(current_year + future_years, start_year))
    else:
        end_year = min(end_year, term_years)
    if end_year < start_year:
        logger.debug(f"Invalid annuity year range for {matter.our_ref}: {start_year}..{end_year}")
        return 0

    # Import deadline_engine for date calculation
    compute_annual_fee_deadlines, _ = _load_deadline_engine()
    if not compute_annual_fee_deadlines:
        logger.error("deadline_engine module not available for annuity calculation")
        return 0

    count = 0
    matter_changed = False

    # Best-effort: mark prepaid years (1-3) as paid for domestic cases to avoid
    # showing them as overdue/pending in list views.
    if prepaid_years > 0:
        try:
            prepaid_items = AnnuityItem.query.filter(
                AnnuityItem.matter_id == mid,
                AnnuityItem.cycle_no.isnot(None),
                AnnuityItem.cycle_no >= 1,
                AnnuityItem.cycle_no <= prepaid_years,
            ).all()
            prepaid_paid_date = reg_date.isoformat()
            prepaid_updated = 0
            for item in prepaid_items:
                is_paid = False
                try:
                    is_paid = bool(getattr(item, "is_paid", False))
                except Exception:
                    is_paid = _has_paid(getattr(item, "paid_date", None))
                if is_paid:
                    continue
                item.paid_date = prepaid_paid_date
                item.annuity_status = "paid"
                db.session.add(item)
                prepaid_updated += 1
            if prepaid_updated:
                count += prepaid_updated
                matter_changed = True
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="annuity_service.prepaid_years.auto_mark_paid",
                log_key="annuity_service.prepaid_years.auto_mark_paid",
                log_window_seconds=300,
            )
    existing_by_year: dict[int, AnnuityItem] = {}
    try:
        existing_rows = AnnuityItem.query.filter(
            AnnuityItem.matter_id == mid,
            AnnuityItem.cycle_no.isnot(None),
            AnnuityItem.cycle_no >= start_year,
            AnnuityItem.cycle_no <= end_year,
        ).all()
        existing_by_year = {int(r.cycle_no): r for r in existing_rows if r.cycle_no}
    except Exception:
        existing_by_year = {}
    for year in range(start_year, end_year + 1):
        try:
            deadlines = compute_annual_fee_deadlines(reg_date, year)

            def _code_matches(code_val, needle: str) -> bool:
                if not code_val:
                    return False
                if hasattr(code_val, "value"):
                    code_str = str(code_val.value)
                else:
                    code_str = str(code_val)
                return needle in code_str.upper()

            def _find_by_code(dls, needle: str):
                for d in dls:
                    if _code_matches(getattr(d, "code", None), needle):
                        return d
                return None

            def _find_by_name(dls, name_keywords, exclude_keywords=None):
                keywords = [kw for kw in (name_keywords or []) if kw]
                excludes = [kw for kw in (exclude_keywords or []) if kw]
                if not keywords:
                    return None
                for d in dls:
                    name = getattr(d, "name", "") or ""
                    if any(kw in name for kw in keywords):
                        if excludes and any(ex in name for ex in excludes):
                            continue
                        return d
                return None

            due_deadline = _find_by_code(deadlines, "ANNUITY_DUE")
            grace_deadline = _find_by_code(deadlines, "ANNUITY_GRACE")

            if not due_deadline:
                due_deadline = _find_by_name(deadlines, ["RenewalDeadline", "Deadline"])
            if not grace_deadline:
                grace_deadline = _find_by_name(
                    deadlines,
                    ["RenewalGrace", "AddPayment", "Grace", "Surcharge"],
                )

            if not due_deadline:
                continue

            # Check if already exists
            existing = existing_by_year.get(year)

            due_dt = _parse_date(getattr(due_deadline, "due", None))
            if not due_dt:
                continue
            grace_dt = _parse_date(getattr(grace_deadline, "due", None)) if grace_deadline else None
            if grace_dt and grace_dt < due_dt:
                logger.warning(
                    "Suspicious annuity deadlines for %s year %s: grace(%s) < due(%s)",
                    matter.our_ref,
                    year,
                    grace_dt,
                    due_dt,
                )
                continue

            due_str = due_dt.isoformat()
            extended_str = grace_dt.isoformat() if grace_dt else None
            renewal_open_str, renewal_notice_str = _compute_renewal_schedule(due_dt)

            if existing:
                if _is_user_deleted_annuity_item(existing):
                    continue
                changed = revive_soft_deleted_annuity_item(existing)
                # Update only if not paid
                is_paid = False
                try:
                    is_paid = bool(getattr(existing, "is_paid", False))
                except Exception:
                    is_paid = _has_paid(existing.paid_date)
                if not is_paid:
                    if (existing.due_date or "").split("T")[0] != due_str:
                        existing.due_date = due_str
                        changed = True
                    cur_ext = (existing.extended_due_date or "").split("T")[0]
                    if extended_str:
                        if cur_ext != extended_str:
                            existing.extended_due_date = extended_str
                            changed = True
                    else:
                        if cur_ext:
                            existing.extended_due_date = None
                            changed = True
                    if renewal_open_str and not (existing.renewal_open_date or "").strip():
                        existing.renewal_open_date = renewal_open_str
                        changed = True
                    if renewal_notice_str and not (existing.renewal_notice_due or "").strip():
                        existing.renewal_notice_due = renewal_notice_str
                        changed = True
                if changed:
                    db.session.add(existing)
                    count += 1
                    matter_changed = True
            else:
                # Create new
                import uuid

                default_owner = _get_default_owner(matter)
                item = AnnuityItem(
                    annuity_id=uuid.uuid4().hex,
                    matter_id=mid,
                    cycle_no=year,
                    annuity_status="pending",
                    due_date=due_str,
                    extended_due_date=extended_str,
                    renewal_open_date=renewal_open_str,
                    renewal_notice_due=renewal_notice_str,
                    owner_staff_party_id=default_owner,
                    memo="[AutoCreate] "
                    + json.dumps(
                        {
                            "auto": True,
                            "generated_at": date.today().isoformat(),
                            "registration_date": reg_date.isoformat(),
                        },
                        ensure_ascii=False,
                    ),
                )
                db.session.add(item)
                existing_by_year[year] = item
                count += 1
                matter_changed = True

        except Exception as e:
            logger.warning(f"Failed to generate annuity year {year} for {matter.our_ref}: {e}")
            continue

    # enqueue ONLY ONCE per matter (batch optimization)
    if matter_changed:
        enqueue_annuity_sync_for_matter(mid)

    if commit and (count > 0 or facts_changed):
        try:
            db.session.commit()
            if count > 0:
                logger.info(f"Generated/updated {count} annuity items for {matter.our_ref}")
            elif facts_changed:
                logger.info(f"Updated matter_facts for {matter.our_ref} (no annuity rows changed)")
        except Exception:
            db.session.rollback()
            raise

    return count


def giveup_annuities_for_matter(
    matter_id: str,
    *,
    include_paid: bool = False,
    commit: bool = True,
) -> int:
    """Mark annuities as giveup for a matter (optionally including paid years)."""
    mid = (matter_id or "").strip()
    if not mid:
        return 0

    try:
        items = AnnuityItem.query.filter(AnnuityItem.matter_id == mid).all()
    except Exception:
        return 0

    if not items:
        return 0

    updated = 0
    for item in items:
        status_raw = (getattr(item, "annuity_status", None) or "").strip().lower()
        is_paid = status_raw == "paid" or _has_paid(getattr(item, "paid_date", None))
        if is_paid and not include_paid:
            continue
        if status_raw == "giveup" and not (item.paid_date or "").strip():
            continue
        item.annuity_status = "giveup"
        item.paid_date = None
        db.session.add(item)
        updated += 1

    if updated:
        enqueue_annuity_sync_for_matter(mid)

    if commit and updated:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    return updated


def ensure_annuities_for_all_registered_matters(
    *,
    limit: int = 0,
    start_year: int | None = None,
    end_year: int | None = None,
    commit: bool = True,
) -> tuple[int, int]:
    """
    Generate annuities for all registered matters.

    Args:
        limit: Maximum number of matters to process (0 = all)
        commit: If True, commit after each matter

    Returns:
        Tuple of (matters_processed, total_annuities_created)
    """
    from app.utils.policy_sql import policy_text as text

    if start_year is None or start_year <= 0:
        start_year = _get_int_config("DEADLINE_DEFAULT_ANNUITY_START_YEAR", 1)
    if start_year <= 0:
        start_year = 1

    start_ts = datetime.utcnow()
    watermark = _get_datetime_config("ANNUITY_AUTOGEN_WATERMARK")

    # 0) backfill matter_facts so filtering can be fully normalized
    _backfill_matter_facts_for_annuities(limit=0)

    # 1) Fully normalized selection (eligible only)
    matter_ids: list[str] = []
    facts_ok = True
    try:
        allow_foreign = _get_bool_config("ANNUITY_ALLOW_FOREIGN", False)
        sql = """
            SELECT mf.matter_id
            FROM matter_facts mf
            JOIN matter m ON m.matter_id = mf.matter_id
            WHERE mf.registration_date IS NOT NULL
              AND COALESCE(m.is_deleted, FALSE) = FALSE
              AND mf.right_type_norm IN ('PATENT','UTILITY','DESIGN','TRADEMARK')
        """
        params = {}
        if not allow_foreign:
            sql += """
              AND UPPER(TRIM(COALESCE(m.right_group, ''))) NOT IN ('OUT','INC','ETC')
            """
        if watermark:
            sql += """
              AND (
                    mf.updated_at >= :wm
                    OR NOT EXISTS (
                        SELECT 1
                        FROM annuity_item ai
                        WHERE ai.matter_id = mf.matter_id
                    )
              )
            """
            params["wm"] = watermark
        sql += " ORDER BY mf.updated_at DESC"
        rows = db.session.execute(text(sql), params).all()
        matter_ids = [r[0] for r in rows if r and r[0]]
    except Exception:
        facts_ok = False
        matter_ids = []

    # Fallback only if facts table is unavailable (pre-migration)
    if not facts_ok:
        query = text(
            f"""
            SELECT DISTINCT matter_id FROM (
                SELECT me.matter_id
                FROM matter_event me
                WHERE me.event_key IN ({_REG_EVENT_KEYS_SQL})
                AND me.event_at IS NOT NULL
                AND TRIM(me.event_at) <> ''

                UNION

                SELECT mcf.matter_id
                FROM matter_custom_field mcf
                WHERE mcf.data IS NOT NULL
                  AND (
                      {_REG_CUSTOM_KEYS_SQL}
                  )
            ) AS registered_matters
            """
        )
        try:
            rows = db.session.execute(query).all()
            matter_ids = [r[0] for r in rows if r and r[0]]
        except Exception as e:
            logger.error(f"Failed to query registered matters: {e}")
            return 0, 0

    if limit > 0:
        matter_ids = matter_ids[:limit]

    processed = 0
    total_created = 0
    failed = 0
    batch_size = _get_int_config("ANNUITY_AUTOGEN_BATCH_SIZE", 200)
    pending = 0

    for mid in matter_ids:
        try:
            count = ensure_annuities_for_matter(
                mid,
                start_year=start_year,
                end_year=end_year,
                commit=False,
            )
            if count > 0:
                total_created += count
            processed += 1
            pending += 1
            if commit and batch_size > 0 and pending >= batch_size:
                db.session.commit()
                pending = 0
        except Exception as e:
            failed += 1
            logger.error(f"Failed to process matter {mid}: {e}")
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="annuity_service.generate_missing_annuities.rollback",
                    log_key="annuity_service.generate_missing_annuities.rollback",
                    log_window_seconds=300,
                )
            pending = 0
            continue

    if commit:
        try:
            if pending > 0:
                db.session.commit()
                pending = 0
            if failed == 0:
                SystemConfig.set_config("ANNUITY_AUTOGEN_WATERMARK", start_ts.isoformat())
            else:
                logger.warning(
                    "Skipped ANNUITY_AUTOGEN_WATERMARK advance after %s matter failures",
                    failed,
                )
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="annuity_service.generate_missing_annuities.final_rollback",
                    log_key="annuity_service.generate_missing_annuities.final_rollback",
                    log_window_seconds=300,
                )

    return processed, total_created


def diagnose_annuity_autogen_for_matter(
    matter_id: str,
    *,
    refresh_registration_date: bool = False,
) -> dict:
    """
    Operational diagnostics for annuity auto-generation.

    Returns a JSON-serializable dict describing:
    - why auto-gen would be skipped
    - which dates/keys are present (registration vs reg-fee paid)
    - current config values affecting generation
    """
    from app.utils.policy_sql import policy_text as text

    mid = (matter_id or "").strip()
    if not mid:
        return {"ok": False, "error": "matter_id_required"}

    matter = Matter.query.get(mid)
    if not matter:
        return {"ok": False, "error": "matter_not_found", "matter_id": mid}

    cfg = {
        "ANNUITY_ALLOW_FOREIGN": _get_bool_config("ANNUITY_ALLOW_FOREIGN", False),
        "ANNUITY_ALLOW_REG_FEE_PAID_FALLBACK": _get_bool_config(
            "ANNUITY_ALLOW_REG_FEE_PAID_FALLBACK", False
        ),
        "DEADLINE_DEFAULT_ANNUITY_START_YEAR": _get_int_config(
            "DEADLINE_DEFAULT_ANNUITY_START_YEAR", 1
        ),
        "DEADLINE_ANNUITY_FULL_TERM": _get_bool_config("DEADLINE_ANNUITY_FULL_TERM", True),
        "DEADLINE_ANNUITY_AUTOGEN_FUTURE_YEARS": _get_int_config(
            "DEADLINE_ANNUITY_AUTOGEN_FUTURE_YEARS", 3
        ),
        "DEADLINE_TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT": _get_int_config(
            "DEADLINE_TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT",
            _TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT,
        ),
        "DEADLINE_TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS": _get_int_config(
            "DEADLINE_TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS",
            _TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS_DEFAULT,
        ),
        "DEADLINE_TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS": _get_int_config(
            "DEADLINE_TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS",
            _TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS_DEFAULT,
        ),
        "DEADLINE_DEFAULT_PATENT_TERM": _get_int_config("DEADLINE_DEFAULT_PATENT_TERM", 20),
        "DEADLINE_DEFAULT_UTILITY_TERM": _get_int_config("DEADLINE_DEFAULT_UTILITY_TERM", 10),
        "DEADLINE_DEFAULT_DESIGN_TERM": _get_int_config("DEADLINE_DEFAULT_DESIGN_TERM", 20),
    }

    effective_division, _effective_type = resolve_profile_case_kind(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
    )
    right_type = _infer_right_type(matter)
    reg_fee_paid_fallback_allowed = _allow_reg_fee_paid_fallback_for_matter(matter, right_type)

    mf = None
    try:
        mf = MatterFacts.query.get(mid)
    except Exception:
        mf = None

    facts = {
        "registration_date": (
            mf.registration_date.isoformat() if mf and mf.registration_date else None
        ),
        "registration_date_source": (mf.registration_date_source if mf else None),
        "right_type_norm": (mf.right_type_norm if mf else None),
        "updated_at": (mf.updated_at.isoformat() if mf and mf.updated_at else None),
    }

    reg_date = _get_registration_date(mid, refresh=bool(refresh_registration_date))
    reg_fee_paid_date = _get_reg_fee_paid_date(mid)
    term_expiry_date = _get_term_expiry_date(mid) if right_type == "TRADEMARK" else None

    schedule_reg_date = reg_date
    base_date = reg_date
    base_source = "registration_date"
    if right_type == "TRADEMARK":
        if term_expiry_date:
            base_date = term_expiry_date
            base_source = "term_expiry_date"
        elif reg_date:
            base_date = reg_date + relativedelta(years=_get_term_years("TRADEMARK"))
            base_source = "registration_date_plus_term"
        elif reg_fee_paid_fallback_allowed and reg_fee_paid_date:
            schedule_reg_date = reg_fee_paid_date
            base_date = reg_fee_paid_date + relativedelta(years=_get_term_years("TRADEMARK"))
            base_source = "reg_fee_paid_date_fallback_plus_term"
        else:
            base_date = None
            base_source = "term_expiry_date"
    elif not base_date and reg_fee_paid_fallback_allowed and reg_fee_paid_date:
        base_date = reg_fee_paid_date
        base_source = "reg_fee_paid_date_fallback"

    skip_reason = None
    if effective_division in ("OUT", "INC") and not cfg["ANNUITY_ALLOW_FOREIGN"]:
        skip_reason = "foreign_skipped"
    elif not right_type:
        skip_reason = "right_type_unknown"
    elif right_type not in ANNUITY_RIGHT_TYPES:
        skip_reason = "right_type_not_supported"
    elif not base_date:
        skip_reason = "registration_date_missing"

    # Compute what would be generated (range) if not skipped.
    range_info = None
    if not skip_reason and base_date and right_type in ANNUITY_RIGHT_TYPES:
        term_years = _get_term_years(right_type)
        if right_type == "TRADEMARK" and schedule_reg_date:
            cycle_no = _trademark_cycle_no_from_dates(schedule_reg_date, base_date)
            range_info = {
                "start_year": cycle_no,
                "end_year": cycle_no,
                "term_years": term_years,
                "current_year": cycle_no,
            }
        else:
            try:
                start_year = int(cfg["DEADLINE_DEFAULT_ANNUITY_START_YEAR"] or 1)
            except Exception:
                start_year = 1
            start_year = max(1, start_year)
            current_year = _annuity_year_from_registration(base_date)
            if cfg["DEADLINE_ANNUITY_FULL_TERM"]:
                end_year = term_years
            else:
                future_years = int(cfg["DEADLINE_ANNUITY_AUTOGEN_FUTURE_YEARS"] or 0)
                future_years = max(0, future_years)
                end_year = min(term_years, current_year + future_years)
            range_info = {
                "start_year": start_year,
                "end_year": end_year,
                "term_years": term_years,
                "current_year": current_year,
            }

    # Snapshot what keys exist for this matter (best-effort, small & safe).
    event_rows = []
    try:
        event_rows = db.session.execute(
            text(
                """
                SELECT event_key, event_at
                FROM matter_event
                WHERE matter_id = :mid
                  AND (
                      event_key LIKE '%Registration%'
                      OR UPPER(event_key) LIKE '%REG%'
                  )
                ORDER BY mevent_id DESC
                LIMIT 50
                """
            ),
            {"mid": mid},
        ).all()
    except Exception:
        event_rows = []

    # Existing annuity stats (helps distinguish "no data" vs "filter illusion").
    annuity_stats = {"total": 0, "by_status": {}}
    try:
        items = AnnuityItem.query.filter(AnnuityItem.matter_id == mid).all()
        annuity_stats["total"] = len(items)
        by_status: dict[str, int] = {}
        for it in items:
            s = (getattr(it, "annuity_status", None) or "").strip().lower() or "unknown"
            by_status[s] = by_status.get(s, 0) + 1
        annuity_stats["by_status"] = by_status
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_service.debug_annuity_stats",
            log_key="annuity_service.debug_annuity_stats",
            log_window_seconds=300,
        )

    return {
        "ok": True,
        "matter": {
            "matter_id": mid,
            "our_ref": matter.our_ref,
            "right_group": (matter.right_group or "").strip(),
            "matter_type": (matter.matter_type or "").strip(),
        },
        "config": cfg,
        "facts": facts,
        "inferred": {
            "right_type": right_type,
            "right_group_norm": effective_division,
            "reg_fee_paid_fallback_allowed": reg_fee_paid_fallback_allowed,
            "trademark_registration_payment_term": (
                _get_trademark_registration_payment_term(matter)
                if right_type == "TRADEMARK"
                else None
            ),
        },
        "dates": {
            "registration_date": (reg_date.isoformat() if reg_date else None),
            "reg_fee_paid_date": (reg_fee_paid_date.isoformat() if reg_fee_paid_date else None),
            "term_expiry_date": (term_expiry_date.isoformat() if term_expiry_date else None),
            "annuity_base_date": (base_date.isoformat() if base_date else None),
            "annuity_base_source": base_source if base_date else None,
        },
        "skip_reason": skip_reason,
        "range": range_info,
        "annuity_stats": annuity_stats,
        "recent_event_rows": [{"event_key": r[0], "event_at": r[1]} for r in (event_rows or [])],
    }
