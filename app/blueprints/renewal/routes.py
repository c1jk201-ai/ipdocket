from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from flask import jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import Integer, and_, case, cast, func, literal_column, or_

from app.blueprints.renewal import bp
from app.extensions import db
from app.models.matter_facts import MatterFacts
from app.models.party import Party
from app.models.ip_records import AnnuityItem, Matter, MatterPartyRole
from app.services.annuity.annuity_management import (
    is_annuity_management_disabled_for_matter,
    resolve_annuity_management_disabled_matter_ids,
)
from app.services.annuity.annuity_service import (
    ensure_annuities_for_matter,
    revive_soft_deleted_annuity_item,
    soft_delete_annuity_item,
)
from app.services.annuity.annuity_visibility import (
    clamp_visible_cycle_count,
    get_visible_cycle_count,
)
from app.services.audit.entity_audit import (
    diff_snapshots,
    record_entity_change_audit,
    snapshot_attrs,
)
from app.services.core.config_service import ConfigService
from app.services.deletion_manager import DeletionService
from app.services.workflow.sync_requests import enqueue_annuity_sync_for_item
from app.services.workflow.task_sync import sync_annuity_workflows_for_matter
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import (
    can_access_matter,
    can_manage_case_globally,
    policy_accessible_matter_ids_select,
)
from app.utils.renewal_labels import (
    normalize_renewal_jurisdiction,
    normalize_renewal_right_type,
    renewal_cycle_label,
)

logger = logging.getLogger(__name__)
_ANNUITY_AUDIT_FIELDS = (
    "annuity_id",
    "matter_id",
    "owner_staff_party_id",
    "cycle_no",
    "annuity_status",
    "due_date",
    "extended_due_date",
    "renewal_open_date",
    "renewal_notice_due",
    "official_fee",
    "vat_amount",
    "service_fee",
    "paid_date",
    "paid_amount",
    "internal_due_date",
    "memo",
    "raw_id",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
)


def _annuity_audit_snapshot(item: AnnuityItem) -> dict[str, object]:
    return snapshot_attrs(item, _ANNUITY_AUDIT_FIELDS)


def _annuity_audit_meta(item: AnnuityItem, *, source: str) -> dict[str, object]:
    return {
        "annuity_id": str(getattr(item, "annuity_id", "") or ""),
        "matter_id": str(getattr(item, "matter_id", "") or ""),
        "cycle_no": getattr(item, "cycle_no", None),
        "source": source,
    }


def _annuity_right_type_for_item(item: AnnuityItem) -> str | None:
    mid = str(getattr(item, "matter_id", "") or "").strip()
    if not mid:
        return None
    facts = MatterFacts.query.get(mid)
    matter = Matter.query.get(mid)
    return normalize_renewal_right_type(
        getattr(facts, "right_type_norm", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "right_group", None),
        getattr(matter, "our_ref", None),
    )


def _annuity_jurisdiction_for_item(item: AnnuityItem) -> str | None:
    mid = str(getattr(item, "matter_id", "") or "").strip()
    if not mid:
        return None
    matter = Matter.query.get(mid)
    return normalize_renewal_jurisdiction(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
        getattr(matter, "our_ref", None),
    )


def _record_annuity_audit(
    *,
    item: AnnuityItem,
    action: str,
    before: dict[str, object] | None,
    source: str,
    include_snapshots: bool = False,
) -> None:
    after = _annuity_audit_snapshot(item)
    changes = diff_snapshots(before or {}, after)
    if not changes and before is not None and not include_snapshots:
        return
    record_entity_change_audit(
        action=action,
        target_type="annuity_item",
        actor_id=getattr(current_user, "id", None),
        changes=changes,
        before=before,
        after=after if include_snapshots else None,
        meta=_annuity_audit_meta(item, source=source),
        title=renewal_cycle_label(
            getattr(item, "cycle_no", None),
            right_type=_annuity_right_type_for_item(item),
            jurisdiction=_annuity_jurisdiction_for_item(item),
        ),
        include_snapshots=include_snapshots,
    )


def _active_annuity_filter():
    return or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))


def _can_edit_annuity_matter(matter_id: str) -> bool:
    mid = str(matter_id or "").strip()
    if not mid:
        return False
    if can_manage_case_globally(current_user):
        return True
    return bool(can_access_matter(current_user, mid, action="edit_case"))


def _resolve_next_window_size(raw: object | None) -> int:
    return clamp_visible_cycle_count(raw, default=get_visible_cycle_count())


def _get_applicants_map(matter_ids: list) -> dict:
    """Get applicants for multiple matters in one query."""
    if not matter_ids:
        return {}
    try:
        # Directly join matter_party_role and party (ORM) to avoid policy_bypass.
        # Use case-insensitive check for role_code ('applicant' vs 'APPLICANT').
        if getattr(db.engine.dialect, "name", "") == "postgresql":
            agg = func.string_agg(Party.name_display, "; ")
        else:
            agg = func.group_concat(Party.name_display, "; ")
        rows = (
            db.session.query(MatterPartyRole.matter_id, agg)
            .join(Party, Party.party_id == MatterPartyRole.party_id)
            .filter(MatterPartyRole.matter_id.in_(list(matter_ids)))
            .filter(func.lower(MatterPartyRole.role_code) == "applicant")
            .group_by(MatterPartyRole.matter_id)
            .all()
        )
        return {row[0]: row[1] for row in rows}
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="renewal._get_applicants_map",
            log_key="renewal._get_applicants_map",
            log_window_seconds=300,
        )
        return {}


_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")


def _safe_preview(value: object | None, limit: int = 80) -> str | None:
    if value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    if not s:
        return None
    return s[:limit]


def _normalize_date(value: object | None) -> str | None:
    """Normalize common date formats to YYYY-MM-DD."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    try:
        s = str(value).strip()
    except Exception:
        return None
    if not s:
        return None
    s = s.strip("[](){}<>").replace("/", "-").replace(".", "-").split("T")[0]
    match = _DATE_RE.search(s)
    if not match:
        return None
    try:
        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return date(y, m, d).isoformat()
    except Exception:
        return None


def _normalize_status(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    if not s:
        return None
    if "Abandoned" in s:
        return "giveup"
    lower = s.lower()
    if lower in ("paid", "pending", "giveup"):
        return lower
    if lower == "overdue":
        return "pending"
    if s in ("In Progress",):
        return "pending"
    if "Done" in s:
        return "paid"
    return lower


def _annuity_status_text_expr():
    return func.coalesce(
        func.nullif(func.lower(func.trim(AnnuityItem.annuity_status)), ""),
        "pending",
    )


def _annuity_is_giveup_expr(status_text=None):
    status_text = status_text if status_text is not None else _annuity_status_text_expr()
    return or_(
        status_text.in_(("giveup", "give_up", "give-up", "abandoned", "waived", "forfeit")),
        status_text.like("%Abandoned%"),
        status_text.like("%Withdrawn%"),
    )


def _annuity_is_paid_status_expr(status_text=None):
    status_text = status_text if status_text is not None else _annuity_status_text_expr()
    return or_(
        status_text.in_(("paid", "payed", "done", "complete", "completed")),
        status_text.like("%receipt%"),
    )


def _annuity_is_paid_expr(status_text=None):
    paid_date_text = func.nullif(func.trim(AnnuityItem.paid_date), "")
    return or_(
        _annuity_is_paid_status_expr(status_text),
        paid_date_text.isnot(None),
    )


def _has_paid(paid_date) -> bool:
    """Type-safe paid check - handles str/date/None."""
    return _normalize_date(paid_date) is not None


def _annuity_legal_due_date_str(annuity: object) -> str | None:
    """
    Legal annuity due date for renewal management.

    Prefer `due_date` (Statutory deadline) and only fall back to `extended_due_date`
    when legal due is missing.
    """
    return _normalize_date(getattr(annuity, "due_date", None)) or _normalize_date(
        getattr(annuity, "extended_due_date", None)
    )


def _annuity_display_due_date_str(annuity: object) -> str | None:
    """
    Renewal display/filter due date.

    - `internal_due_date` is used only when it is earlier than legal due.
    - legal due is `due_date` first, then `extended_due_date` fallback.
    """
    internal = _normalize_date(getattr(annuity, "internal_due_date", None))
    legal = _annuity_legal_due_date_str(annuity)
    if internal and legal:
        return internal if internal <= legal else legal
    return internal or legal


def _compute_annuity_display_status(annuity: object, *, today: date | None = None) -> str:
    """Return one of pending/overdue/paid/giveup using renewal display due basis."""
    today = today or date.today()
    st = _normalize_status(getattr(annuity, "annuity_status", None)) or "pending"
    if st in ("paid", "giveup"):
        return st
    if _has_paid(getattr(annuity, "paid_date", None)):
        return "paid"
    due = _annuity_display_due_date_str(annuity)
    if due and due < today.isoformat():
        return "overdue"
    return "pending"


def _pick_existing_annuity_item(matter_id: str, cycle_no: int) -> AnnuityItem | None:
    live_rank = case(
        (or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None)), 0),
        else_=1,
    )
    deleted_at_rank = case((AnnuityItem.deleted_at.is_(None), 0), else_=1)
    return (
        AnnuityItem.query.filter_by(
            matter_id=str(matter_id or ""),
            cycle_no=cycle_no,
        )
        .order_by(
            live_rank.asc(),
            deleted_at_rank.desc(),
            AnnuityItem.deleted_at.desc(),
            AnnuityItem.annuity_id.desc(),
        )
        .first()
    )


def _build_next_open_annuity_ranked_subquery(q_src, *, today: date, due_text=None):
    if due_text is None:
        _, due_text = _apply_effective_due_date_range(q_src, None, None)
    if due_text is None:
        return None

    today_str = today.isoformat()
    base_subq = (
        q_src.order_by(None)
        .with_entities(
            AnnuityItem.annuity_id.label("annuity_id"),
            AnnuityItem.matter_id.label("matter_id"),
            AnnuityItem.cycle_no.label("cycle_no"),
            due_text.label("due_text"),
        )
        .filter(AnnuityItem.cycle_no.isnot(None), AnnuityItem.cycle_no > 0)
        .filter(due_text.isnot(None), due_text != "")
        .subquery()
    )

    has_upcoming = func.max(case((base_subq.c.due_text >= today_str, 1), else_=0)).over(
        partition_by=base_subq.c.matter_id
    )
    anchor_cycle = func.first_value(base_subq.c.cycle_no).over(
        partition_by=base_subq.c.matter_id,
        order_by=(
            case((base_subq.c.due_text >= today_str, 0), else_=1).asc(),
            base_subq.c.due_text.asc(),
            base_subq.c.cycle_no.asc(),
            base_subq.c.annuity_id.asc(),
        ),
    )
    meta_subq = (
        db.session.query(
            base_subq.c.annuity_id.label("annuity_id"),
            base_subq.c.matter_id.label("matter_id"),
            base_subq.c.cycle_no.label("cycle_no"),
            base_subq.c.due_text.label("due_text"),
            has_upcoming.label("has_upcoming"),
            anchor_cycle.label("anchor_cycle"),
        )
        .select_from(base_subq)
        .subquery()
    )

    eligible = or_(
        meta_subq.c.has_upcoming == 0,
        meta_subq.c.cycle_no >= meta_subq.c.anchor_cycle,
    )
    rank_order = (
        case((meta_subq.c.has_upcoming == 1, meta_subq.c.cycle_no), else_=None).asc(),
        case((meta_subq.c.has_upcoming == 1, meta_subq.c.due_text), else_=None).asc(),
        case((meta_subq.c.has_upcoming == 1, meta_subq.c.annuity_id), else_=None).asc(),
        case((meta_subq.c.has_upcoming == 0, meta_subq.c.due_text), else_=None).desc(),
        case((meta_subq.c.has_upcoming == 0, meta_subq.c.cycle_no), else_=None).desc(),
        case((meta_subq.c.has_upcoming == 0, meta_subq.c.annuity_id), else_=None).desc(),
    )
    rn = func.row_number().over(partition_by=meta_subq.c.matter_id, order_by=rank_order)
    return (
        db.session.query(
            meta_subq.c.annuity_id.label("annuity_id"),
            rn.label("rn"),
        )
        .select_from(meta_subq)
        .filter(eligible)
        .subquery()
    )


def _select_next_open_annuity_ids(q_src, *, next_n: int, today: date, due_text=None) -> list[str]:
    """
    Pick "next open" annuity rows using the same policy as annuity workflow:
    - if upcoming exists, anchor on earliest upcoming due and keep consecutive cycles.
    - else, keep most-recent overdue cycles.
    """
    if next_n <= 0:
        return []
    ranked = _build_next_open_annuity_ranked_subquery(q_src, today=today, due_text=due_text)
    if ranked is None:
        return []
    rows = db.session.query(ranked.c.annuity_id).filter(ranked.c.rn <= next_n).all()
    return [str(row.annuity_id) for row in rows]


def _count_next_open_annuity_rows(q_src, *, next_n: int, today: date, due_text=None) -> int:
    if next_n <= 0:
        return 0
    ranked = _build_next_open_annuity_ranked_subquery(q_src, today=today, due_text=due_text)
    if ranked is None:
        return 0
    return int(
        db.session.query(func.count()).select_from(ranked).filter(ranked.c.rn <= next_n).scalar()
        or 0
    )


def _cascade_giveup_items(items, *, force_annuity_ids: set[str] | None = None) -> set[str]:
    force_ids = {str(v) for v in (force_annuity_ids or set()) if str(v or "").strip()}
    updated_ids: set[str] = set()
    for item in items:
        item_id = str(getattr(item, "annuity_id", "") or "")
        status_raw = _normalize_status(getattr(item, "annuity_status", None)) or "pending"
        if status_raw == "paid" or _has_paid(getattr(item, "paid_date", None)):
            continue
        if status_raw == "giveup" and item_id not in force_ids:
            continue
        item.annuity_status = "giveup"
        item.paid_date = None
        enqueue_annuity_sync_for_item(annuity_item=item)
        if item_id:
            updated_ids.add(item_id)
    return updated_ids


def _apply_effective_due_date_range(q, start: str | None, end: str | None):
    """
    Apply renewal due date range (internal only if earlier than legal).

    Renewal  Default reference date "Renewal Due date" legal due
    due_date  , due_date   extended_due_date .

    IMPORTANT:
    - Legacy data can contain non-canonical date strings:
        * 2026-1-2 (no zero-padding)
        * 2026/1/2 or 2026.1.2 (separator variants)
        * 2026-01-02T00:00:00 (time suffix)
      Existing (due_text LIKE '____-__-__')    
      " items "  .
    -  DB dialect  normalize   Filter Apply.
    """
    try:
        # NOTE: Use SQL literals (not bind params) for constants heavily reused in
        # expressions. On SQLite, complex expressions can be cloned many times
        # (e.g., window functions + subqueries) and hit the "too many SQL variables"
        # limit if constants are bound parameters.
        _EMPTY = literal_column("''")
        internal_raw = func.nullif(func.trim(AnnuityItem.internal_due_date), _EMPTY)
        extended_raw = func.nullif(func.trim(AnnuityItem.extended_due_date), _EMPTY)
        due_raw = func.nullif(func.trim(AnnuityItem.due_date), _EMPTY)
        legal_raw = func.coalesce(due_raw, extended_raw)

        dialect = getattr(db.engine.dialect, "name", "") or ""

        # ---------- PostgreSQL: to_date + split_part   ----------
        if dialect == "postgresql":

            def _clean(expr):
                expr = func.replace(func.replace(expr, "/", "-"), ".", "-")
                # strip time part if exists
                expr = func.split_part(expr, "T", 1)
                expr = func.split_part(expr, " ", 1)
                return expr

            internal_dt = func.to_date(_clean(internal_raw), "YYYY-MM-DD")
            legal_dt = func.to_date(_clean(legal_raw), "YYYY-MM-DD")
            effective_dt = case(
                (
                    and_(internal_dt.isnot(None), legal_dt.isnot(None), internal_dt <= legal_dt),
                    internal_dt,
                ),
                (and_(internal_dt.isnot(None), legal_dt.is_(None)), internal_dt),
                else_=legal_dt,
            )
            due_text = func.to_char(effective_dt, "YYYY-MM-DD")

            if not start and not end:
                return q, due_text
            if start:
                q = q.filter(effective_dt >= func.to_date(start, "YYYY-MM-DD"))
            if end:
                q = q.filter(effective_dt <= func.to_date(end, "YYYY-MM-DD"))
            return q, due_text

        # ---------- SQLite: string split + yyyymmdd  ----------
        if dialect == "sqlite":
            _SLASH = literal_column("'/'")
            _DOT = literal_column("'.'")
            _DASH = literal_column("'-'")
            _ZERO = literal_column("0")
            _ONE = literal_column("1")
            _FOUR = literal_column("4")
            _HUNDRED = literal_column("100")
            _TEN_THOUSAND = literal_column("10000")
            _TWELVE = literal_column("12")
            _THIRTY_ONE = literal_column("31")
            _PRINTF_FMT = literal_column("'%04d-%02d-%02d'")

            def _parts(expr):
                # normalize separators
                expr = func.replace(func.replace(expr, _SLASH, _DASH), _DOT, _DASH)
                p1 = func.instr(expr, _DASH)
                rest = func.substr(expr, p1 + _ONE)
                p2 = func.instr(rest, _DASH)

                y = cast(func.substr(expr, _ONE, _FOUR), Integer)
                m = cast(func.substr(rest, _ONE, p2 - _ONE), Integer)
                d = cast(func.substr(rest, p2 + _ONE), Integer)

                key = y * _TEN_THOUSAND + m * _HUNDRED + d
                valid = and_(
                    p1 > _ZERO,
                    p2 > _ZERO,
                    y > _ZERO,
                    m >= _ONE,
                    m <= _TWELVE,
                    d >= _ONE,
                    d <= _THIRTY_ONE,
                )
                return case((valid, key), else_=None), y, m, d

            internal_key, iy, im, iday = _parts(internal_raw)
            legal_key, ly, lm, lday = _parts(legal_raw)

            use_internal = or_(
                and_(internal_key.isnot(None), legal_key.isnot(None), internal_key <= legal_key),
                and_(internal_key.isnot(None), legal_key.is_(None)),
            )

            eff_key = case((use_internal, internal_key), else_=legal_key)
            eff_y = case((use_internal, iy), else_=ly)
            eff_m = case((use_internal, im), else_=lm)
            eff_d = case((use_internal, iday), else_=lday)

            # printf(NULL,NULL,NULL) => '0000-00-00' ,  key  Create
            due_text = case(
                (eff_key.isnot(None), func.printf(_PRINTF_FMT, eff_y, eff_m, eff_d)),
                else_=None,
            )

            if start or end:

                def _iso_to_key(val: str | None) -> int | None:
                    try:
                        s = str(val or "").strip()
                        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                            return int(s[0:4] + s[5:7] + s[8:10])
                    except Exception:
                        return None
                    return None

                start_key = _iso_to_key(start)
                end_key = _iso_to_key(end)

                q = q.filter(eff_key.isnot(None))
                if start_key is not None:
                    q = q.filter(eff_key >= start_key)
                if end_key is not None:
                    q = q.filter(eff_key <= end_key)
            return q, due_text

        # ---------- Other dialect: Existing   ----------
        effective_due = case(
            (
                and_(internal_raw.isnot(None), legal_raw.isnot(None), internal_raw <= legal_raw),
                internal_raw,
            ),
            (and_(internal_raw.isnot(None), legal_raw.is_(None)), internal_raw),
            else_=legal_raw,
        )
        due_text = func.substr(effective_due, 1, 10)
        if not start and not end:
            return q, due_text
        q = q.filter(due_text.like("____-__-__"))
        if start:
            q = q.filter(due_text >= start)
        if end:
            q = q.filter(due_text <= end)
        return q, due_text
    except Exception as exc:
        # Best-effort: don't break the page; but log for diagnosis.
        report_swallowed_exception(
            exc,
            context="renewal._apply_effective_due_date_range",
            log_key="renewal._apply_effective_due_date_range",
            log_window_seconds=300,
        )
        return q, None


@bp.route("/")
@login_required
def index():
    return redirect(url_for("annuities.calendar_month"))


@bp.route("/fees")
@login_required
def fees():
    return render_template(
        "renewal/index.html",
        page="fees",
        annuity_visible_cycle_count=_resolve_next_window_size(None),
    )


@bp.route("/calendar/month")
@login_required
def calendar_month():
    return render_template("renewal/index.html", page="calendar_month")


@bp.route("/api/calendar/events", methods=["GET"])
@login_required
def api_calendar_events():
    """Calendar-optimized annuity events (range required)."""
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    start = _normalize_date(start_raw)
    end = _normalize_date(end_raw)
    if not start or not end:
        return jsonify({"error": "start/end required"}), 400

    try:
        start_dt = datetime.fromisoformat(start).date()
        end_dt = datetime.fromisoformat(end).date()
    except Exception:
        return jsonify({"error": "invalid date range"}), 400

    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    max_days = ConfigService.get_int("ANNUITY_CALENDAR_MAX_RANGE_DAYS", 120)
    if max_days is not None and max_days > 0:
        if (end_dt - start_dt).days > max_days:
            end_dt = start_dt + timedelta(days=max_days)

    include_giveup = str(request.args.get("include_giveup") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    debug = str(request.args.get("debug") or "").strip().lower() in ("1", "true", "yes")
    can_debug = bool(debug and can_manage_case_globally(current_user))
    missing_due_samples = []
    giveup_samples = []
    missing_due_reason_counts: dict[str, int] = {}

    def _count_reason(reason: str) -> None:
        missing_due_reason_counts[reason] = missing_due_reason_counts.get(reason, 0) + 1

    q = db.session.query(
        AnnuityItem.annuity_id,
        AnnuityItem.annuity_status,
        AnnuityItem.due_date,
        AnnuityItem.extended_due_date,
        AnnuityItem.internal_due_date,
        AnnuityItem.paid_date,
        AnnuityItem.cycle_no,
        AnnuityItem.matter_id,
        Matter.our_ref,
        Matter.right_name,
        Matter.matter_type,
        Matter.right_group,
        MatterFacts.right_type_norm,
    )
    q = q.join(Matter, AnnuityItem.matter_id == Matter.matter_id).outerjoin(
        MatterFacts,
        MatterFacts.matter_id == Matter.matter_id,
    )
    q = q.filter(_active_annuity_filter())

    accessible_matter_ids = policy_accessible_matter_ids_select(current_user)
    q = q.filter(AnnuityItem.matter_id.in_(accessible_matter_ids))
    disabled_matter_ids = resolve_annuity_management_disabled_matter_ids()
    if disabled_matter_ids:
        q = q.filter(~AnnuityItem.matter_id.in_(list(disabled_matter_ids)))

    q, due_text = _apply_effective_due_date_range(q, None, None)
    paid_date_text = func.nullif(func.trim(AnnuityItem.paid_date), "")
    if due_text is not None:
        q = q.filter(
            or_(
                and_(
                    due_text.isnot(None),
                    due_text != "",
                    due_text >= start_dt.isoformat(),
                    due_text <= end_dt.isoformat(),
                ),
                and_(
                    or_(due_text.is_(None), due_text == ""),
                    paid_date_text.isnot(None),
                    paid_date_text >= start_dt.isoformat(),
                    paid_date_text <= end_dt.isoformat(),
                ),
            )
        )
        range_sort = func.coalesce(due_text, paid_date_text)
    else:
        q, due_text = _apply_effective_due_date_range(
            q,
            start_dt.isoformat(),
            end_dt.isoformat(),
        )
        range_sort = due_text

    annuity_status_text = _annuity_status_text_expr()
    if not include_giveup:
        q = q.filter(~_annuity_is_giveup_expr(annuity_status_text))

    if range_sort is not None:
        q = q.order_by(range_sort.asc(), AnnuityItem.cycle_no.asc())
    else:
        q = q.order_by(AnnuityItem.cycle_no.asc())

    max_events = ConfigService.get_int("ANNUITY_CALENDAR_MAX_EVENTS", 5000)
    if max_events is None:
        max_events = 5000
    max_events = max(1, min(int(max_events), 20000))

    rows = q.limit(max_events + 1).all()
    truncated = len(rows) > max_events
    rows = rows[:max_events]

    today = date.today()
    events = []
    skipped_missing_due = 0
    skipped_giveup = 0
    filled_from_paid_date = 0
    for (
        annuity_id,
        annuity_status,
        due_date,
        extended_due_date,
        internal_due_date,
        paid_date,
        cycle_no,
        matter_id,
        our_ref,
        right_name,
        matter_type,
        right_group,
        right_type_norm,
    ) in rows:
        item = type(
            "AnnuityRow",
            (),
            {
                "annuity_status": annuity_status,
                "due_date": due_date,
                "extended_due_date": extended_due_date,
                "internal_due_date": internal_due_date,
                "paid_date": paid_date,
            },
        )()
        display_due = _annuity_display_due_date_str(item)
        if not display_due:
            # --- reason classification (debug friendly) ---
            n_due = _normalize_date(due_date)
            n_ext = _normalize_date(extended_due_date)
            n_int = _normalize_date(internal_due_date)
            n_paid = _normalize_date(paid_date)

            # if field has content but normalize failed => "unparseable_*"
            has_due_raw = bool(_safe_preview(due_date))
            has_ext_raw = bool(_safe_preview(extended_due_date))
            has_int_raw = bool(_safe_preview(internal_due_date))

            unparseable_due = bool(has_due_raw and not n_due)
            unparseable_ext = bool(has_ext_raw and not n_ext)
            unparseable_int = bool(has_int_raw and not n_int)

            reason = "missing_due_and_unpaid"
            if n_paid:
                # Policy: keep visibility by using paid_date when due info is missing.
                reason = "missing_due_but_paid"
                display_due = n_paid
                filled_from_paid_date += 1
            elif unparseable_due or unparseable_ext or unparseable_int:
                reason = "unparseable_due_fields"
            elif not (has_due_raw or has_ext_raw or has_int_raw):
                reason = "missing_all_due_fields"

            _count_reason(reason)

            if reason != "missing_due_but_paid":
                skipped_missing_due += 1

            if can_debug and len(missing_due_samples) < 20:
                missing_due_samples.append(
                    {
                        "reason": reason,
                        "annuity_id": annuity_id,
                        "matter_id": matter_id,
                        "our_ref": our_ref,
                        "right_name": right_name,
                        "annuity_status": annuity_status,
                        "cycle_no": cycle_no,
                        "raw": {
                            "due_date": _safe_preview(due_date),
                            "extended_due_date": _safe_preview(extended_due_date),
                            "internal_due_date": _safe_preview(internal_due_date),
                            "paid_date": _safe_preview(paid_date),
                        },
                        "normalized": {
                            "due_date": n_due,
                            "extended_due_date": n_ext,
                            "internal_due_date": n_int,
                            "paid_date": n_paid,
                        },
                        "flags": {
                            "unparseable_due": unparseable_due,
                            "unparseable_extended": unparseable_ext,
                            "unparseable_internal": unparseable_int,
                        },
                    }
                )

            # If we used paid_date fallback, continue to compute status and render.
            if reason != "missing_due_but_paid":
                continue
        status = _compute_annuity_display_status(item, today=today)
        if status == "giveup" and not include_giveup:
            skipped_giveup += 1
            if can_debug and len(giveup_samples) < 20:
                giveup_samples.append(
                    {
                        "annuity_id": annuity_id,
                        "matter_id": matter_id,
                        "our_ref": our_ref,
                        "right_name": right_name,
                        "annuity_status": annuity_status,
                        "effective_due_date": display_due,
                        "cycle_no": cycle_no,
                    }
                )
            continue

        year_label = renewal_cycle_label(
            cycle_no,
            right_type=normalize_renewal_right_type(right_type_norm, matter_type, right_group),
            jurisdiction=normalize_renewal_jurisdiction(right_group, matter_type, our_ref),
        )
        title = f"[{year_label}] {our_ref or ''}"
        color = (
            "#198754"
            if status == "paid"
            else (
                "#dc3545" if status == "overdue" else "#6c757d" if status == "giveup" else "#ffc107"
            )
        )
        events.append(
            {
                "id": annuity_id,
                "title": title.strip(),
                "start": display_due,
                "color": color,
                "url": url_for("case_work.case_detail", case_id=matter_id),
                "extendedProps": {
                    "status": status,
                    "cycle_no": cycle_no,
                    "cycle_label": year_label,
                    "matter_id": matter_id,
                    "case_ref": our_ref,
                    "case_title": right_name,
                },
            }
        )

    return jsonify(
        {
            "events": events,
            "truncated": truncated,
            "max_events": max_events,
            "skipped_missing_due": skipped_missing_due,
            "skipped_giveup": skipped_giveup,
            "filled_from_paid_date": filled_from_paid_date,
            **(
                {
                    "debug": {
                        "missing_due_samples": missing_due_samples,
                        "giveup_samples": giveup_samples,
                        "missing_due_reason_counts": missing_due_reason_counts,
                    }
                }
                if can_debug
                else {}
            ),
            "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        }
    )


@bp.route("/giveup")
@login_required
def giveup():
    return render_template("renewal/index.html", page="giveup")


# --- JSON APIs ---


@bp.route("/api/fees", methods=["GET", "POST"])
@login_required
def api_fees():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        matter_id = str(data.get("matter_id") or data.get("case_id") or "").strip()
        if not matter_id:
            return jsonify({"error": "matter_id required"}), 400

        matter = Matter.query.get(matter_id)
        if not matter:
            return jsonify({"error": "invalid matter_id"}), 400
        if not _can_edit_annuity_matter(str(matter.matter_id)):
            return jsonify({"error": "forbidden"}), 403
        if is_annuity_management_disabled_for_matter(str(matter.matter_id)):
            return jsonify({"error": "annuity management disabled"}), 409

        raw_due = data.get("due_date")
        if raw_due in (None, ""):
            return jsonify({"error": "due_date required"}), 400
        due = _normalize_date(raw_due)
        if not due:
            return jsonify({"error": "invalid due_date (YYYY-MM-DD)"}), 400

        year = data.get("year")
        year_text = str(year).strip() if year is not None else ""
        if not year_text:
            return jsonify({"error": "year required"}), 400
        try:
            cycle_no = int(year_text)
        except Exception:
            return jsonify({"error": "invalid year (positive integer required)"}), 400
        if cycle_no <= 0:
            return jsonify({"error": "invalid year (positive integer required)"}), 400
        fee_amount = data.get("fee_amount")
        try:
            fee_amount = float(fee_amount) if fee_amount not in (None, "") else 0.0
        except Exception:
            fee_amount = 0.0

        existing = _pick_existing_annuity_item(str(matter.matter_id), cycle_no)
        if existing:
            revive_soft_deleted_annuity_item(existing)

        ai = existing or AnnuityItem(
            matter_id=str(matter.matter_id),
            cycle_no=cycle_no,
            annuity_status=_normalize_status("pending"),
        )
        if due or not existing:
            ai.due_date = due
        if "fee_amount" in data or not existing:
            ai.official_fee = fee_amount
        if "notes" in data or not existing:
            ai.memo = data.get("notes") or None
        db.session.add(ai)
        enqueue_annuity_sync_for_item(annuity_item=ai)
        db.session.commit()
        return (
            jsonify(
                {"id": ai.annuity_id, "matter_id": str(ai.matter_id), "updated": bool(existing)}
            ),
            201,
        )

    # GET list
    q = (
        db.session.query(AnnuityItem, Matter, MatterFacts)
        .join(Matter, AnnuityItem.matter_id == Matter.matter_id)
        .outerjoin(MatterFacts, MatterFacts.matter_id == Matter.matter_id)
    )
    q = q.filter(_active_annuity_filter())
    # Enforce matter-level visibility on GET (prevents broad data leaks for logged-in users).
    accessible_matter_ids = policy_accessible_matter_ids_select(current_user)
    q = q.filter(AnnuityItem.matter_id.in_(accessible_matter_ids))
    disabled_matter_ids = resolve_annuity_management_disabled_matter_ids()
    if disabled_matter_ids:
        q = q.filter(~AnnuityItem.matter_id.in_(list(disabled_matter_ids)))

    today = date.today()

    # range filter (YYYY-MM-DD); if omitted, return all rows (paged)
    start = _normalize_date(request.args.get("start"))
    end = _normalize_date(request.args.get("end"))
    q, due_text = _apply_effective_due_date_range(q, start, end)

    status = (request.args.get("status") or "").strip().lower()
    reg_source = (request.args.get("reg_source") or "").strip().lower()
    mode = (request.args.get("mode") or "").strip().lower()
    next_n = _resolve_next_window_size(request.args.get("next_n"))
    include_stats = str(request.args.get("stats") or "").strip().lower() in ("1", "true", "yes")
    today_str = today.isoformat()

    if mode == "next" and status in ("", "all"):
        # "next" view is intended for actionable items, not paid/giveup history.
        status = "open"

    annuity_status_text = _annuity_status_text_expr()
    is_paid = _annuity_is_paid_expr(annuity_status_text)
    is_giveup = _annuity_is_giveup_expr(annuity_status_text)

    if status in ("", "all"):
        pass
    elif status == "paid":
        q = q.filter(is_paid)
    elif status == "giveup":
        q = q.filter(is_giveup)
    elif status == "open":
        q = q.filter(~is_paid, ~is_giveup)
    elif status == "overdue":
        q = q.filter(~is_paid, ~is_giveup)
        if due_text is not None:
            q = q.filter(due_text.isnot(None), due_text != "", due_text < today_str)
    elif status == "pending":
        q = q.filter(~is_paid, ~is_giveup)
        if due_text is not None:
            q = q.filter(or_(due_text.is_(None), due_text == "", due_text >= today_str))

    def _apply_reg_source_filter(q_src, value: str):
        if not value or value in ("all", "any"):
            return q_src
        source_text = func.nullif(func.trim(MatterFacts.registration_date_source), "")
        if value in ("missing", "none", "unknown"):
            return q_src.filter(or_(source_text.is_(None), source_text == ""))
        if value in ("set", "known", "present"):
            return q_src.filter(source_text.isnot(None), source_text != "")
        if value in ("fallback", "reg_fee_paid_date_fallback"):
            return q_src.filter(source_text == "reg_fee_paid_date_fallback")
        if value.startswith("source:"):
            raw = value.split(":", 1)[1].strip().lower()
            if not raw:
                return q_src
            return q_src.filter(func.lower(source_text) == raw)
        return q_src.filter(func.lower(source_text) == value)

    q = _apply_reg_source_filter(q, reg_source)
    current_next_open_ids: list[str] | None = None

    def _build_next_subquery(q_src):
        due_sort = due_text
        due_sort_isnull = case((or_(due_sort.is_(None), due_sort == ""), 1), else_=0)
        rn = func.row_number().over(
            partition_by=AnnuityItem.matter_id,
            order_by=(
                due_sort_isnull.asc(),
                due_sort.asc(),
                AnnuityItem.cycle_no.asc(),
                AnnuityItem.annuity_id.asc(),
            ),
        )
        subq = (
            q_src.order_by(None)
            .with_entities(
                AnnuityItem.annuity_id.label("annuity_id"),
                rn.label("rn"),
            )
            .subquery()
        )
        return subq, due_sort, due_sort_isnull

    if mode == "next" and status == "open":
        current_next_open_ids = _select_next_open_annuity_ids(
            q,
            next_n=next_n,
            today=today,
            due_text=due_text,
        )
        if current_next_open_ids:
            q = q.filter(AnnuityItem.annuity_id.in_(current_next_open_ids))
        else:
            q = q.filter(AnnuityItem.annuity_id == "__none__")
        if due_text is not None:
            due_sort = due_text
            due_sort_isnull = case((or_(due_sort.is_(None), due_sort == ""), 1), else_=0)
            q = q.order_by(
                due_sort_isnull.asc(),
                due_sort.asc(),
                AnnuityItem.matter_id.asc(),
                AnnuityItem.cycle_no.asc(),
            )
        else:
            q = q.order_by(AnnuityItem.matter_id.asc(), AnnuityItem.cycle_no.asc())
    elif (
        mode == "next"
        and due_text is not None
        and status in ("open", "overdue", "pending", "giveup")
    ):
        subq, due_sort, due_sort_isnull = _build_next_subquery(q)
        q = (
            q.join(subq, subq.c.annuity_id == AnnuityItem.annuity_id)
            .filter(subq.c.rn <= next_n)
            .order_by(
                due_sort_isnull.asc(),
                due_sort.asc(),
                AnnuityItem.matter_id.asc(),
                AnnuityItem.cycle_no.asc(),
            )
        )

    # pagination (always on)
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 200))
    except Exception:
        per_page = 200
    page = max(page, 1)
    per_page = min(max(per_page, 1), 5000)

    if mode != "next":
        q = q.order_by(AnnuityItem.matter_id.asc(), AnnuityItem.cycle_no.asc())
    rows = q.offset((page - 1) * per_page).limit(per_page + 1).all()
    has_next = len(rows) > per_page
    rows = rows[:per_page]

    matter_ids = list({r.Matter.matter_id for r in rows})
    applicants_map = _get_applicants_map(matter_ids)

    data = []

    for ai, matter, facts in rows:
        display_due = _annuity_display_due_date_str(ai)
        st = _compute_annuity_display_status(ai, today=today)  # pending/overdue/paid/giveup
        reg_source_val = ((facts.registration_date_source or "").strip() if facts else "") or None
        needs_review = reg_source_val == "reg_fee_paid_date_fallback"
        data.append(
            {
                "id": ai.annuity_id,
                "matter_id": ai.matter_id,
                "case_ref": matter.our_ref,
                "case_title": matter.right_name,
                "applicants": applicants_map.get(matter.matter_id),
                "cycle_no": ai.cycle_no,
                "cycle_label": renewal_cycle_label(
                    ai.cycle_no,
                    right_type=(getattr(facts, "right_type_norm", None) or matter.matter_type),
                    jurisdiction=normalize_renewal_jurisdiction(
                        matter.right_group,
                        matter.matter_type,
                        matter.our_ref,
                    ),
                ),
                "year": ai.cycle_no,
                "due_date": _normalize_date(ai.due_date),
                "extended_due_date": _normalize_date(ai.extended_due_date),
                "internal_due_date": _normalize_date(ai.internal_due_date),
                "effective_due_date": display_due,
                "paid_date": _normalize_date(ai.paid_date),
                "annuity_status": ai.annuity_status,  # Savevalue is pending/paid/giveup
                "derived_status": st,  # Display/Filter
                "status": st,
                "fee_amount": ai.official_fee or 0,
                "official_fee": ai.official_fee or 0,
                "vat_amount": ai.vat_amount or 0,
                "service_fee": ai.service_fee or 0,
                "total_fee": (ai.official_fee or 0) + (ai.vat_amount or 0) + (ai.service_fee or 0),
                "currency": "USD",
                "url": url_for("case_work.case_detail", case_id=matter.matter_id),
                "reg_source": reg_source_val,
                "needs_review": needs_review,
            }
        )

    payload = {
        "page": page,
        "per_page": per_page,
        "has_next": has_next,
        "next_n": next_n,
        "items": data,
    }

    if include_stats:
        next_open_count_cache: dict[str, int] = {}

        def _apply_status_filter(q_src, status_val: str | None):
            if not status_val or status_val in ("", "all"):
                return q_src
            if status_val == "paid":
                return q_src.filter(is_paid)
            if status_val == "giveup":
                return q_src.filter(is_giveup)
            if status_val == "open":
                return q_src.filter(~is_paid, ~is_giveup)
            if status_val == "overdue":
                qf = q_src.filter(~is_paid, ~is_giveup)
                if due_text is not None:
                    qf = qf.filter(due_text.isnot(None), due_text != "", due_text < today_str)
                return qf
            if status_val == "pending":
                qf = q_src.filter(~is_paid, ~is_giveup)
                if due_text is not None:
                    qf = qf.filter(or_(due_text.is_(None), due_text == "", due_text >= today_str))
                return qf
            return q_src

        def _count_query(q_src, *, status_val: str | None):
            qf = _apply_status_filter(q_src, status_val)
            if mode == "next" and status_val == "open":
                if current_next_open_ids is not None:
                    return len(current_next_open_ids)
                cache_key = "open"
                if cache_key in next_open_count_cache:
                    return next_open_count_cache[cache_key]
                count = _count_next_open_annuity_rows(
                    qf,
                    next_n=next_n,
                    today=today,
                    due_text=due_text,
                )
                next_open_count_cache[cache_key] = count
                return count
            if (
                mode == "next"
                and due_text is not None
                and status_val in ("open", "overdue", "pending", "giveup")
            ):
                subq, _, _ = _build_next_subquery(qf)
                return int(
                    db.session.query(func.count())
                    .select_from(subq)
                    .filter(subq.c.rn <= next_n)
                    .scalar()
                    or 0
                )
            try:
                return int(qf.count() or 0)
            except Exception:
                return 0

        base_q = (
            db.session.query(AnnuityItem, Matter, MatterFacts)
            .join(Matter, AnnuityItem.matter_id == Matter.matter_id)
            .outerjoin(MatterFacts, MatterFacts.matter_id == Matter.matter_id)
        )
        base_q = base_q.filter(_active_annuity_filter())
        base_q = base_q.filter(AnnuityItem.matter_id.in_(accessible_matter_ids))
        if disabled_matter_ids:
            base_q = base_q.filter(~AnnuityItem.matter_id.in_(list(disabled_matter_ids)))
        if start or end:
            base_q, _ = _apply_effective_due_date_range(base_q, start, end)
        base_q = _apply_reg_source_filter(base_q, reg_source)

        by_status = {}
        for key in ("open", "pending", "overdue", "paid", "giveup"):
            by_status[key] = _count_query(base_q, status_val=key)
        total_all = _count_query(base_q, status_val="all")
        filtered_total = _count_query(base_q, status_val=status if status else "all")

        payload["stats"] = {
            "total": total_all,
            "filtered_total": filtered_total,
            "by_status": by_status,
            "mode": mode,
            "status": status or "all",
        }

    return jsonify(payload)


@bp.route("/api/matters/bulk-action", methods=["POST"])
@login_required
def api_matter_bulk_action():
    """
    Bulk annuity operations by matter.

    action:
      - ensure: Renewal AutoCreate/Updated
      - workflow_sync: Renewal Task 
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()
    matter_ids_raw = data.get("matter_ids") or []
    if action not in {"ensure", "workflow_sync"}:
        return jsonify({"error": "invalid action"}), 400
    if not isinstance(matter_ids_raw, list) or not matter_ids_raw:
        return jsonify({"error": "matter_ids(list) required"}), 400

    max_matters = ConfigService.get_int(
        "ANNUITY_BULK_OP_MAX_MATTERS", 200, min_value=1, max_value=2000
    )
    if max_matters is None:
        max_matters = 200

    matter_ids: list[str] = []
    seen: set[str] = set()
    for v in matter_ids_raw:
        mid = str(v or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        matter_ids.append(mid)

    if not matter_ids:
        return jsonify({"error": "valid matter_ids required"}), 400
    if len(matter_ids) > int(max_matters):
        return (
            jsonify(
                {"error": f"too many matter_ids (max={int(max_matters)})", "max": int(max_matters)}
            ),
            400,
        )

    denied = [mid for mid in matter_ids if not _can_edit_annuity_matter(mid)]
    if denied:
        return (
            jsonify(
                {
                    "error": "forbidden",
                    "denied_matter_ids": denied[:20],
                    "denied_count": len(denied),
                }
            ),
            403,
        )

    processed = 0
    changed_total = 0
    errors: list[dict[str, str]] = []

    for mid in matter_ids:
        try:
            if action == "ensure":
                changed = ensure_annuities_for_matter(
                    str(mid),
                    refresh_registration_date=True,
                    commit=False,
                )
                sync_annuity_workflows_for_matter(str(mid))
                changed_total += int(changed or 0)
            elif action == "workflow_sync":
                sync_annuity_workflows_for_matter(str(mid))

            db.session.commit()
            processed += 1
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="renewal.api_matter_bulk_action.rollback",
                    log_key="renewal.api_matter_bulk_action.rollback",
                    log_window_seconds=300,
                )
            errors.append({"matter_id": str(mid), "error": str(exc)})

    return jsonify(
        {
            "success": not errors,
            "action": action,
            "requested": len(matter_ids),
            "processed": processed,
            "failed": len(errors),
            "changed": changed_total,
            "errors": errors[:20],
        }
    )


@bp.route("/api/fees/<fid>", methods=["PATCH", "DELETE"])
@login_required
def api_fee_detail(fid: str):
    rf = AnnuityItem.query.get(fid)
    if not rf or bool(getattr(rf, "is_deleted", False)):
        return jsonify({"error": "not found"}), 404

    if not _can_edit_annuity_matter(str(rf.matter_id or "")):
        return jsonify({"error": "forbidden"}), 403
    if request.method == "PATCH" and is_annuity_management_disabled_for_matter(str(rf.matter_id)):
        return jsonify({"error": "annuity management disabled"}), 409

    if request.method == "DELETE":
        audit_before = _annuity_audit_snapshot(rf)
        # DeletionLog is best-effort; isolate so failures don't abort deletion.
        try:
            with db.session.begin_nested():
                DeletionService().archive(
                    rf,
                    user_id=getattr(current_user, "id", None),
                    tags=("manual", "renewal-route"),
                )
                db.session.flush()
        except Exception:
            logger.warning(
                f"Failed to create DeletionLog for annuity {rf.annuity_id}",
                exc_info=True,
            )
        # IMPORTANT: deletion  annuity_id workflow   
        # matter  rebuild(sync_annuity_workflows_for_matter)   .
        enqueue_annuity_sync_for_item(annuity_item=rf)
        soft_delete_annuity_item(
            rf,
            reason="renewal_fee_delete",
            deleted_by=getattr(current_user, "id", None),
        )
        _record_annuity_audit(
            item=rf,
            action="annuity.delete",
            before=audit_before,
            source="renewal.api_fee_detail.delete",
            include_snapshots=True,
        )
        db.session.commit()
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    audit_before = _annuity_audit_snapshot(rf)
    if "paid_date" in data:
        raw_paid = data.get("paid_date")
        if raw_paid is None or (isinstance(raw_paid, str) and not raw_paid.strip()):
            rf.paid_date = None
        else:
            normalized = _normalize_date(raw_paid)
            if not normalized:
                return jsonify({"error": "invalid paid_date (YYYY-MM-DD)"}), 400
            rf.paid_date = normalized
    if "status" in data:
        s = _normalize_status(data.get("status"))
        if s == "giveup":
            # Cascade giveup to all future cycles in the same matter.
            cascade_q = AnnuityItem.query.filter_by(matter_id=rf.matter_id).filter(
                _active_annuity_filter()
            )
            if rf.cycle_no:
                cascade_q = cascade_q.filter(
                    AnnuityItem.cycle_no.isnot(None),
                    AnnuityItem.cycle_no >= rf.cycle_no,
                )
            cascade_items = cascade_q.all()
            before_by_id = {
                str(item.annuity_id): _annuity_audit_snapshot(item) for item in cascade_items
            }
            updated_ids = _cascade_giveup_items(
                cascade_items,
                force_annuity_ids={str(rf.annuity_id)},
            )
            for item in cascade_items:
                if str(item.annuity_id) in updated_ids:
                    _record_annuity_audit(
                        item=item,
                        action="annuity.status_change",
                        before=before_by_id.get(str(item.annuity_id)),
                        source="renewal.api_fee_detail.giveup_cascade",
                    )
            db.session.commit()
            return jsonify({"success": True, "updated": len(updated_ids), "cascade": True})
        else:
            rf.annuity_status = s
            if s == "paid":
                # If no paid date exists, default to today
                if not _has_paid(rf.paid_date):
                    rf.paid_date = date.today().isoformat()
            else:
                # If status changes to non-paid (e.g. pending), clear the paid date
                rf.paid_date = None
    if "notes" in data:
        rf.memo = data["notes"]
    if "due_date" in data:
        raw_due = data.get("due_date")
        if raw_due is None or (isinstance(raw_due, str) and not raw_due.strip()):
            rf.due_date = None
        else:
            normalized = _normalize_date(raw_due)
            if not normalized:
                return jsonify({"error": "invalid due_date (YYYY-MM-DD)"}), 400
            rf.due_date = normalized
    if "owner_staff_party_id" in data:
        rf.owner_staff_party_id = data["owner_staff_party_id"] or None
    if "internal_due_date" in data:
        raw_internal = data.get("internal_due_date")
        if raw_internal is None or (isinstance(raw_internal, str) and not raw_internal.strip()):
            rf.internal_due_date = None
        else:
            normalized = _normalize_date(raw_internal)
            if not normalized:
                return jsonify({"error": "invalid internal_due_date (YYYY-MM-DD)"}), 400
            rf.internal_due_date = normalized
    enqueue_annuity_sync_for_item(annuity_item=rf)
    _record_annuity_audit(
        item=rf,
        action=(
            "annuity.status_change"
            if set(diff_snapshots(audit_before, _annuity_audit_snapshot(rf)).keys())
            <= {"annuity_status", "paid_date"}
            else "annuity.update"
        ),
        before=audit_before,
        source="renewal.api_fee_detail.patch",
    )
    db.session.commit()
    return jsonify({"success": True})


@bp.route("/api/fees/bulk", methods=["PATCH", "DELETE"])
@login_required
def api_fees_bulk():
    """Bulk update or delete multiple annuity items."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])

    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids (list) required"}), 400

    # Fetch all items
    items = (
        AnnuityItem.query.filter(AnnuityItem.annuity_id.in_(ids))
        .filter(_active_annuity_filter())
        .all()
    )
    if not items:
        return jsonify({"error": "no items found"}), 404

    denied_matter_ids = sorted(
        {
            str(getattr(it, "matter_id", "") or "")
            for it in items
            if not _can_edit_annuity_matter(str(getattr(it, "matter_id", "") or ""))
        }
    )
    if denied_matter_ids:
        return (
            jsonify(
                {
                    "error": "forbidden",
                    "denied_matter_ids": denied_matter_ids[:20],
                    "denied_count": len(denied_matter_ids),
                }
            ),
            403,
        )

    if request.method == "DELETE":
        for rf in items:
            audit_before = _annuity_audit_snapshot(rf)
            try:
                with db.session.begin_nested():
                    DeletionService().archive(
                        rf,
                        user_id=getattr(current_user, "id", None),
                        tags=("manual", "renewal-route", "bulk"),
                    )
                    db.session.flush()
            except Exception:
                logger.warning(
                    f"Failed to create DeletionLog for annuity {rf.annuity_id}",
                    exc_info=True,
                )
            enqueue_annuity_sync_for_item(annuity_item=rf)
            soft_delete_annuity_item(
                rf,
                reason="renewal_fee_bulk_delete",
                deleted_by=getattr(current_user, "id", None),
            )
            _record_annuity_audit(
                item=rf,
                action="annuity.delete",
                before=audit_before,
                source="renewal.api_fees_bulk.delete",
                include_snapshots=True,
            )
        db.session.commit()
        return jsonify({"success": True, "deleted": len(items)})

    # PATCH: bulk update status
    new_status = (data.get("status") or "").strip().lower()
    if not new_status:
        return jsonify({"error": "status required for PATCH"}), 400

    normalized_status = _normalize_status(new_status)
    if normalized_status == "giveup":
        # Cascade giveup for each matter (from the earliest selected cycle).
        by_matter: dict[str, dict[str, int | bool | None]] = {}
        for rf in items:
            mid = str(rf.matter_id)
            entry = by_matter.setdefault(mid, {"min_cycle": None, "has_null": False})
            if rf.cycle_no is None:
                entry["has_null"] = True
            else:
                try:
                    cycle_no = int(rf.cycle_no)
                except Exception:
                    entry["has_null"] = True
                else:
                    if entry["min_cycle"] is None or cycle_no < entry["min_cycle"]:
                        entry["min_cycle"] = cycle_no

        updated_ids: set[str] = set()
        # Avoid N+1 queries: fetch all annuity items for these matters in one query, then
        # apply per-matter cycle filtering in Python.
        cascade_matter_ids = [str(mid) for mid in by_matter.keys() if str(mid).strip()]
        cascade_items_all = (
            AnnuityItem.query.filter(AnnuityItem.matter_id.in_(cascade_matter_ids))
            .filter(_active_annuity_filter())
            .all()
            if cascade_matter_ids
            else []
        )
        before_by_id = {
            str(item.annuity_id): _annuity_audit_snapshot(item) for item in cascade_items_all
        }
        items_by_mid: dict[str, list[AnnuityItem]] = {}
        for it in cascade_items_all:
            m = str(getattr(it, "matter_id", "") or "")
            if not m:
                continue
            items_by_mid.setdefault(m, []).append(it)

        for mid, info in by_matter.items():
            mid = str(mid or "").strip()
            if not mid:
                continue
            cascade_items = items_by_mid.get(mid) or []
            if not info["has_null"]:
                min_cycle = info.get("min_cycle")
                if isinstance(min_cycle, int):
                    cascade_items = [
                        it
                        for it in cascade_items
                        if it.cycle_no is not None and int(it.cycle_no) >= min_cycle
                    ]
            forced_ids = {
                str(rf.annuity_id) for rf in items if str(getattr(rf, "matter_id", "") or "") == mid
            }
            updated_ids.update(
                _cascade_giveup_items(
                    cascade_items,
                    force_annuity_ids=forced_ids,
                )
            )

        for item in cascade_items_all:
            if str(item.annuity_id) in updated_ids:
                _record_annuity_audit(
                    item=item,
                    action="annuity.status_change",
                    before=before_by_id.get(str(item.annuity_id)),
                    source="renewal.api_fees_bulk.giveup_cascade",
                )
        db.session.commit()
        return jsonify({"success": True, "updated": len(updated_ids), "cascade": True})

    before_by_id = {str(rf.annuity_id): _annuity_audit_snapshot(rf) for rf in items}
    for rf in items:
        rf.annuity_status = normalized_status
        if normalized_status == "paid":
            if not _has_paid(rf.paid_date):
                rf.paid_date = date.today().isoformat()
        else:
            rf.paid_date = None
        enqueue_annuity_sync_for_item(annuity_item=rf)
        _record_annuity_audit(
            item=rf,
            action="annuity.status_change",
            before=before_by_id.get(str(rf.annuity_id)),
            source="renewal.api_fees_bulk.patch",
        )

    db.session.commit()
    return jsonify({"success": True, "updated": len(items)})
