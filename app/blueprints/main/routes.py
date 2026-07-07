from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import and_, case, false, func, not_, or_, select
from werkzeug.exceptions import HTTPException

from app.blueprints.main import bp
from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.operation import Operation
from app.models.ip_records import DocketItem, Matter, VMatterOverview
from app.models.risk_control import DeadlineReviewQueue, MatterRiskFact
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.models.workflow import Workflow
from app.services.automation.review_feedback import collect_doc_type_feedback_metrics
from app.services.core.config_service import ConfigService
from app.services.deadlines.deadline_verification import (
    resolve_deadline_review_item,
    verify_deadline_queue,
)
from app.services.matter.matter_risk_service import refresh_matter_risk_facts, risk_queue_query
from app.services.ops.operation_context import OperationContext
from app.utils.docket_visibility import visible_on_or_before
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import (
    can_access_matter,
    can_manage_case_globally,
    managed_matter_ids_select,
    policy_accessible_matter_ids_select,
    resolve_role_scope,
)
from app.utils.workflow_list_status import (
    TERMINAL_WORKFLOW_STATUS_KEYS,
    is_workflow_terminal_status,
)
from app.utils.workflow_roles import workflow_user_filter


@bp.get("/favicon.ico")
def favicon_ico():
    # Browsers often request /favicon.ico regardless of HTML <link rel="icon">.
    try:
        from app.services.core.branding import get_branding

        favicon_path = (get_branding().favicon_path or "").strip().replace("\\", "/")
        if favicon_path.lower().startswith(("http://", "https://")):
            return redirect(favicon_path)
        if favicon_path.startswith("/static/"):
            favicon_path = favicon_path[len("/static/") :]
        if favicon_path and not favicon_path.startswith("/") and ".." not in favicon_path:
            return send_from_directory(current_app.static_folder, favicon_path)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="main.routes.favicon_ico.custom_branding",
            log_key="main.routes.favicon_ico.custom_branding",
            log_window_seconds=300,
        )
    return send_from_directory(current_app.static_folder, "favicon.ico")


try:
    from app.models.email_automation import EmailMessage
except Exception:  # pragma: no cover
    EmailMessage = None  # type: ignore

try:
    from app.models.user import (
        ROLE_ADMIN,
        ROLE_LEAD_ATTORNEY,
        ROLE_MGMT_DIRECTOR,
        ROLE_MGMT_STAFF,
        ROLE_PARTNER_ATTORNEY,
        ROLE_PATENT_STAFF,
    )
except ImportError:
    ROLE_ADMIN = "admin"
    ROLE_MGMT_DIRECTOR = "mgmt_director"
    ROLE_MGMT_STAFF = "mgmt_staff"
    ROLE_LEAD_ATTORNEY = "lead_attorney"
    ROLE_PATENT_STAFF = "patent_staff"
    ROLE_PARTNER_ATTORNEY = "partner_attorney"


RISK_CENTER_ENABLED_CONFIG_KEY = "RISK_CENTER_ENABLED"


def _greeting_for(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


# Import category constants from central location
from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES

OVERDUE_EXCLUDED_NAMES = [
    "ForeignFilingDeadline",
    "PCTFilingDeadline",
    "PCT Filing Deadline",
    "PCTPreliminary examinationDeadline",
    "PCTDomesticDeadline",
    "Foreign filing overdue guidance log",
]
URGENT_WINDOW_DAYS = 7
HOME_ACTIVE_USER_WINDOW_MINUTES = 5
HOME_ACTIVE_USER_LIMIT = 5


def _parse_due_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        s = str(value).strip()
        if not s:
            return None
        if "T" in s:
            s = s.split("T")[0]
        if " " in s:
            s = s.split(" ")[0]
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _matter_reference(matter: Matter | None, fallback: str | None = None) -> str:
    if matter:
        return matter.our_ref or matter.your_ref or matter.matter_id or "-"
    return fallback or "-"


def _format_display_date(value: date | None) -> str:
    if not value:
        return "-"
    return value.strftime(current_app.config.get("DATE_FORMAT", "%m/%d/%Y"))


def _build_task_card(di: DocketItem, matter: Matter | None, today: date) -> dict:
    due_date = _parse_due_date(di.extended_due_date) or _parse_due_date(di.due_date)
    days_left = (due_date - today).days if due_date else None
    is_overdue = bool(due_date and due_date < today)

    if days_left is None:
        days_label = "-"
    elif days_left < 0:
        days_label = f"{abs(days_left)}d overdue"
    elif days_left == 0:
        days_label = "Due today"
    else:
        days_label = f"D-{days_left}"

    return {
        "ref": _matter_reference(matter),
        "name": di.name_free or di.name_ref or "-",
        "due_date": due_date,
        "due_label": _format_display_date(due_date),
        "days_label": days_label,
        "is_overdue": is_overdue,
        "priority": "normal",
        "status": "done" if (di.done_date or "").strip() else "pending",
    }


def _build_workflow_task_card(wf: Workflow, matter: Matter | None, today: date) -> dict:
    due_date = getattr(wf, "due_date", None)
    days_left = (due_date - today).days if due_date else None
    is_overdue = bool(due_date and due_date < today)

    if days_left is None:
        days_label = "-"
    elif days_left < 0:
        days_label = f"{abs(days_left)}d overdue"
    elif days_left == 0:
        days_label = "Due today"
    else:
        days_label = f"D-{days_left}"

    return {
        "ref": _matter_reference(matter, getattr(wf, "case_id", None)),
        "name": getattr(wf, "name", None) or "-",
        "due_date": due_date,
        "due_label": _format_display_date(due_date),
        "days_label": days_label,
        "is_overdue": is_overdue,
        "priority": (getattr(wf, "priority", None) or "normal"),
        "status": (
            "done" if is_workflow_terminal_status(getattr(wf, "status", None)) else "pending"
        ),
    }


def _activity_age_label(now_utc: datetime, last_seen: datetime | None) -> str:
    if last_seen is None:
        return "now"
    elapsed_seconds = max(0, int((now_utc - last_seen).total_seconds()))
    if elapsed_seconds < 60:
        return "now"
    elapsed_minutes = elapsed_seconds // 60
    if elapsed_minutes < 60:
        return f"{elapsed_minutes}m ago"
    return f"{elapsed_minutes // 60}h ago"


def _active_user_meta(*, department: str | None, position: str | None, age_label: str) -> str:
    parts = []
    for value in (department, position):
        text = (value or "").strip()
        if text:
            parts.append(text)
    parts.append(age_label)
    return " · ".join(parts)


def _home_active_users_snapshot() -> tuple[list[dict], int]:
    window_minutes = max(
        1,
        int(
            current_app.config.get(
                "HOME_ACTIVE_USERS_WINDOW_MINUTES",
                HOME_ACTIVE_USER_WINDOW_MINUTES,
            )
            or HOME_ACTIVE_USER_WINDOW_MINUTES
        ),
    )
    limit = max(
        1,
        int(
            current_app.config.get("HOME_ACTIVE_USERS_LIMIT", HOME_ACTIVE_USER_LIMIT)
            or HOME_ACTIVE_USER_LIMIT
        ),
    )
    now_utc = datetime.utcnow()
    cutoff = now_utc - timedelta(minutes=window_minutes)
    users: list[dict] = []

    try:
        last_seen_expr = func.max(UserAccessLog.created_at)
        rows = (
            db.session.query(
                User.id.label("user_id"),
                User.username.label("username"),
                User.display_name.label("display_name"),
                User.department.label("department"),
                User.position.label("position"),
                last_seen_expr.label("last_seen"),
            )
            .join(UserAccessLog, UserAccessLog.user_id == User.id)
            .filter(UserAccessLog.created_at >= cutoff)
            .filter(or_(User.is_active.is_(True), User.is_active.is_(None)))
            .group_by(
                User.id,
                User.username,
                User.display_name,
                User.department,
                User.position,
            )
            .order_by(last_seen_expr.desc(), User.id.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            user_id = int(getattr(row, "user_id", 0) or 0)
            display_name = (getattr(row, "display_name", None) or "").strip()
            username = (getattr(row, "username", None) or "").strip()
            age_label = _activity_age_label(now_utc, getattr(row, "last_seen", None))
            meta = _active_user_meta(
                department=getattr(row, "department", None),
                position=getattr(row, "position", None),
                age_label=age_label,
            )
            users.append(
                {
                    "user_id": user_id,
                    "name": display_name or username or f"User {user_id}",
                    "meta": meta,
                    "age_label": age_label,
                    "is_me": False,
                }
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="main._home_active_users_snapshot",
            log_key="main._home_active_users_snapshot",
            log_window_seconds=300,
        )
        users = []

    current_user_id = getattr(current_user, "id", None) if current_user.is_authenticated else None
    if current_user_id is not None:
        found_current = False
        for item in users:
            if item["user_id"] == current_user_id:
                item["is_me"] = True
                found_current = True
                break
        if not found_current:
            display_name = (getattr(current_user, "display_name", None) or "").strip()
            username = (getattr(current_user, "username", None) or "").strip()
            age_label = ""
            meta = _active_user_meta(
                department=getattr(current_user, "department", None),
                position=getattr(current_user, "position", None),
                age_label=age_label,
            )
            users.insert(
                0,
                {
                    "user_id": int(current_user_id),
                    "name": display_name or username or f"User {current_user_id}",
                    "meta": meta,
                    "age_label": age_label,
                    "is_me": True,
                },
            )

    return users[:limit], window_minutes


def _workflow_home_query(user_role: str | None):
    q = (
        db.session.query(Workflow, Matter)
        .join(Matter, Workflow.case_id == Matter.matter_id)
        .filter(
            or_(
                Workflow.business_code.is_(None),
                not_(Workflow.business_code.like("ANNUITY:%")),
            )
        )
    )

    flags = resolve_role_scope(user_role)
    show_all_mgmt = bool(flags.get("show_all_mgmt"))
    show_all_work = bool(flags.get("show_all_work"))
    show_own_mgmt = bool(flags.get("show_own_mgmt"))
    show_own_work = bool(flags.get("show_own_work"))

    if show_all_mgmt and show_all_work:
        return q

    cat_upper = func.upper(Workflow.category)
    is_mine = workflow_user_filter(getattr(current_user, "id", None))
    role_conditions = []

    if show_all_mgmt:
        role_conditions.append(cat_upper.in_(MGMT_CATEGORIES))
    elif show_own_mgmt:
        role_conditions.append(and_(cat_upper.in_(MGMT_CATEGORIES), is_mine))

    if show_all_work:
        role_conditions.append(cat_upper.in_(WORK_CATEGORIES))
    elif show_own_work:
        role_conditions.append(and_(cat_upper.in_(WORK_CATEGORIES), is_mine))

    if not show_all_work:
        try:
            managed_ids = managed_matter_ids_select(current_user)
            role_conditions.append(
                and_(cat_upper.in_(WORK_CATEGORIES), Workflow.case_id.in_(managed_ids))
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="main._workflow_home_query.managed_matter_ids_select",
            )

    if role_conditions:
        return q.filter(or_(*role_conditions))
    return q.filter(false())


def _effective_due_expr():
    # DocketItem due dates are stored as ISO strings (YYYY-MM-DD); keep comparisons lexicographic.
    # Keep this aligned with docket_item's dashboard functional indexes.
    return func.substr(
        func.coalesce(
            func.nullif(func.trim(DocketItem.extended_due_date), ""),
            func.nullif(func.trim(DocketItem.due_date), ""),
        ),
        1,
        10,
    )


def _workflow_status_key_expr():
    raw_status = func.coalesce(Workflow.status, "")
    normalized = func.replace(func.replace(raw_status, "_", " "), "-", " ")
    return func.lower(func.trim(normalized))


def _workflow_open_status_filter():
    return ~_workflow_status_key_expr().in_(tuple(TERMINAL_WORKFLOW_STATUS_KEYS))


def _count_value(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _visibility_conditions(user_role: str | None, staff_pid: str | None) -> list:
    show_all_mgmt = False
    show_all_work = False
    show_own_mgmt = False
    show_own_work = False

    if user_role in (ROLE_ADMIN, ROLE_LEAD_ATTORNEY):
        show_all_mgmt = True
        show_all_work = True
    elif user_role == ROLE_MGMT_DIRECTOR:
        show_all_mgmt = True
    elif user_role == ROLE_PARTNER_ATTORNEY:
        show_all_work = True
    elif user_role == ROLE_MGMT_STAFF:
        show_own_mgmt = True
    elif user_role == ROLE_PATENT_STAFF:
        show_own_work = True
    else:
        show_own_mgmt = True
        show_own_work = True

    conditions = []
    if show_all_mgmt:
        conditions.append(DocketItem.category.in_(MGMT_CATEGORIES))
    elif show_own_mgmt:
        conditions.append(
            and_(
                DocketItem.category.in_(MGMT_CATEGORIES),
                DocketItem.owner_staff_party_id == staff_pid,
            )
        )

    if show_all_work:
        conditions.append(DocketItem.category.in_(WORK_CATEGORIES))
    elif show_own_work:
        conditions.append(
            and_(
                DocketItem.category.in_(WORK_CATEGORIES),
                DocketItem.owner_staff_party_id == staff_pid,
            )
        )

    # Case managers should also be able to see WORK deadlines for matters they manage,
    # even if their global role is MGMT-only (Manager WORK Deadline  ).
    if staff_pid and not show_all_work:
        try:
            managed_ids = managed_matter_ids_select(current_user)
            conditions.append(
                and_(
                    DocketItem.category.in_(WORK_CATEGORIES),
                    DocketItem.matter_id.in_(managed_ids),
                )
            )
        except Exception as exc:
            # Do not silently swallow errors in access-control checks.
            report_swallowed_exception(
                exc,
                context="main._build_docket_conditions.managed_matter_ids_select",
            )

    return conditions


def _table_exists(name: str) -> bool:
    try:
        from sqlalchemy import inspect

        return inspect(db.engine).has_table(name)
    except Exception:
        return False


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _pick_primary_client(raw: str) -> str:
    if not raw:
        return ""
    parts = [p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()]
    return parts[0] if parts else ""


def _finance_focus(staff_pid: str | None, user_role: str | None) -> dict:
    # "Work Home" should focus on the logged-in user's assigned cases.
    # Even if Admin/Partner, if they have a staff_pid, we show THEIR data.
    # Only show global if no staff_pid (e.g. pure sysadmin) or explicit Management role.

    # Exception: MGMT_DIRECTOR sees all.
    if user_role == ROLE_MGMT_DIRECTOR:
        scoped = False
    else:
        # Default to scoped if staff_pid exists
        scoped = bool(staff_pid)

    if scoped and not staff_pid:
        return {"important_client": None, "high_outstanding": None, "scoped": True}

    if not _table_exists("case_flat_index"):
        return {"important_client": None, "high_outstanding": None, "scoped": scoped}

    q = (
        db.session.query(VMatterOverview, CaseFlatIndex)
        .outerjoin(CaseFlatIndex, VMatterOverview.matter_id == CaseFlatIndex.matter_id)
        .filter(VMatterOverview.outstanding_total.isnot(None))
        .filter(VMatterOverview.outstanding_total > 0)
    )
    if scoped and staff_pid:
        q = q.filter(
            or_(
                CaseFlatIndex.attorney_id == staff_pid,
                CaseFlatIndex.manager_id == staff_pid,
                CaseFlatIndex.handler_id == staff_pid,
            )
        )

    rows = q.order_by(VMatterOverview.outstanding_total.desc()).limit(200).all()
    if not rows:
        return {"important_client": None, "high_outstanding": None, "scoped": scoped}

    client_totals: dict[str, float] = {}
    client_sample: dict[str, str] = {}
    for ov, idx in rows:
        client_name = _pick_primary_client(
            (getattr(idx, "client_name", None) or "").strip()
            or (getattr(ov, "clients", None) or "")
        )
        if not client_name:
            continue
        total = _safe_float(getattr(ov, "outstanding_total", None), 0.0)
        client_totals[client_name] = client_totals.get(client_name, 0.0) + total
        if client_name not in client_sample:
            client_sample[client_name] = getattr(ov, "matter_id", None) or ""

    important_client = None
    if client_totals:
        top_name = max(client_totals, key=client_totals.get)
        important_client = {
            "name": top_name,
            "total": client_totals.get(top_name) or 0.0,
            "matter_id": client_sample.get(top_name) or "",
        }

    top_ov = rows[0][0]
    high_outstanding = {
        "matter_id": getattr(top_ov, "matter_id", None),
        "our_ref": getattr(top_ov, "our_ref", None) or getattr(top_ov, "matter_id", None),
        "total": _safe_float(getattr(top_ov, "outstanding_total", None), 0.0),
    }
    return {
        "important_client": important_client,
        "high_outstanding": high_outstanding,
        "scoped": scoped,
    }


@bp.route("/")
@login_required
def index():
    user_role = (getattr(current_user, "role", None) or "").strip().lower()
    if user_role == "user":
        return render_template("main/pending.html")

    # Dashboard Logic
    tzname = current_app.config.get("TIMEZONE", "America/New_York")
    try:
        now = datetime.now(ZoneInfo(tzname))
    except Exception:
        now = datetime.now()
    today = now.date()
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip() or None
    user_role = getattr(current_user, "role", None)
    urgent_date = today + timedelta(days=URGENT_WINDOW_DAYS)
    week_ago = today - timedelta(days=7)
    due_soon_limit = today + timedelta(days=7)

    visibility_conditions = _visibility_conditions(user_role, staff_pid)
    effective_due = _effective_due_expr()
    visible_filter = visible_on_or_before(DocketItem, target_date=today)
    active_docket_filter = None
    if hasattr(DocketItem, "is_deleted"):
        active_docket_filter = or_(
            DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)
        )

    def _apply_active_filter(q):
        if active_docket_filter is not None:
            return q.filter(active_docket_filter)
        return q

    if visibility_conditions:
        today_iso = today.isoformat()
        urgent_iso = urgent_date.isoformat()
        due_soon_iso = due_soon_limit.isoformat()
        base_q = db.session.query(DocketItem).filter(or_(*visibility_conditions))
        if active_docket_filter is not None:
            base_q = base_q.filter(active_docket_filter)
        base_q = base_q.filter(visible_filter)
        pending_q = base_q.filter(
            or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
            effective_due.isnot(None),
        )
        pending_q = pending_q.filter(
            not_(
                and_(
                    or_(
                        DocketItem.name_ref.in_(OVERDUE_EXCLUDED_NAMES),
                        DocketItem.name_free.in_(OVERDUE_EXCLUDED_NAMES),
                    ),
                    effective_due < today_iso,
                )
            )
        )
        count_row = pending_q.with_entities(
            func.count().label("pending_count"),
            func.coalesce(
                func.sum(
                    case(
                        (and_(effective_due >= today_iso, effective_due <= urgent_iso), 1), else_=0
                    )
                ),
                0,
            ).label("urgent_count"),
            func.coalesce(func.sum(case((effective_due < today_iso, 1), else_=0)), 0).label(
                "overdue_count"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (and_(effective_due >= today_iso, effective_due <= due_soon_iso), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("due_soon_total"),
        ).one()
        pending_count = _count_value(count_row.pending_count)
        urgent_count = _count_value(count_row.urgent_count)
        overdue_count = _count_value(count_row.overdue_count)
        due_soon_total = _count_value(count_row.due_soon_total)

        # Fetch overdue tasks list for display
        overdue_rows = (
            _apply_active_filter(
                db.session.query(DocketItem, Matter)
                .join(Matter, DocketItem.matter_id == Matter.matter_id)
                .filter(or_(*visibility_conditions))
                .filter(visible_filter)
            )
            .filter(
                or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
                effective_due.isnot(None),
                effective_due < today_iso,
            )
            .filter(
                not_(
                    or_(
                        DocketItem.name_ref.in_(OVERDUE_EXCLUDED_NAMES),
                        DocketItem.name_free.in_(OVERDUE_EXCLUDED_NAMES),
                    )
                )
            )
            .order_by(effective_due.asc())
            .limit(50)
            .all()
        )
        overdue_tasks = [_build_task_card(di, matter, today) for di, matter in overdue_rows]

        done_text = func.substr(func.trim(DocketItem.done_date), 1, 10)
        done_base = base_q.filter(
            DocketItem.done_date.isnot(None),
            func.trim(DocketItem.done_date) != "",
            ~func.upper(func.trim(DocketItem.done_date)).like("AUTO_%"),
        )
        if getattr(db.engine.dialect, "name", "") == "postgresql":
            completed_count = done_base.filter(
                done_text.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
                done_text >= week_ago.isoformat(),
            ).count()
        else:
            completed_count = done_base.filter(
                done_text.like("____-__-__"),
                done_text >= week_ago.isoformat(),
            ).count()

        urgent_rows = (
            _apply_active_filter(
                db.session.query(DocketItem, Matter)
                .join(Matter, DocketItem.matter_id == Matter.matter_id)
                .filter(or_(*visibility_conditions))
                .filter(visible_filter)
            )
            .filter(
                or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
                effective_due.isnot(None),
            )
            .filter(
                not_(
                    and_(
                        or_(
                            DocketItem.name_ref.in_(OVERDUE_EXCLUDED_NAMES),
                            DocketItem.name_free.in_(OVERDUE_EXCLUDED_NAMES),
                        ),
                        effective_due < today_iso,
                    )
                )
            )
            .filter(effective_due <= urgent_iso)
            .filter(effective_due >= today_iso)
            .order_by(effective_due.asc())
            .limit(50)
            .all()
        )
        urgent_tasks = [_build_task_card(di, matter, today) for di, matter in urgent_rows]

        next_due_row = (
            _apply_active_filter(
                db.session.query(DocketItem, Matter)
                .join(Matter, DocketItem.matter_id == Matter.matter_id)
                .filter(or_(*visibility_conditions))
                .filter(visible_filter)
            )
            .filter(
                or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
                effective_due.isnot(None),
            )
            .filter(
                not_(
                    and_(
                        or_(
                            DocketItem.name_ref.in_(OVERDUE_EXCLUDED_NAMES),
                            DocketItem.name_free.in_(OVERDUE_EXCLUDED_NAMES),
                        ),
                        effective_due < today_iso,
                    )
                )
            )
            .order_by(effective_due.asc())
            .first()
        )
        next_due = (
            _build_task_card(next_due_row[0], next_due_row[1], today) if next_due_row else None
        )

        if staff_pid:
            my_open_query = (
                _apply_active_filter(
                    db.session.query(DocketItem, Matter)
                    .join(Matter, DocketItem.matter_id == Matter.matter_id)
                    .filter(or_(*visibility_conditions))
                    .filter(visible_filter)
                )
                .filter(DocketItem.owner_staff_party_id == staff_pid)
                .filter(
                    or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
                    effective_due.isnot(None),
                )
                .filter(
                    not_(
                        and_(
                            or_(
                                DocketItem.name_ref.in_(OVERDUE_EXCLUDED_NAMES),
                                DocketItem.name_free.in_(OVERDUE_EXCLUDED_NAMES),
                            ),
                            effective_due < today_iso,
                        )
                    )
                )
            )
            my_open_count = my_open_query.count()
            my_tasks_raw = (
                my_open_query.order_by(
                    effective_due.is_(None),
                    effective_due.asc(),
                )
                .limit(12)
                .all()
            )
            my_tasks = [_build_task_card(di, matter, today) for di, matter in my_tasks_raw]
        else:
            my_open_count = 0
            my_tasks = []
    else:
        pending_count = 0
        urgent_count = 0
        overdue_count = 0
        completed_count = 0
        due_soon_total = 0
        urgent_tasks = []
        overdue_tasks = []
        next_due = None
        my_open_count = 0
        my_tasks = []

    # Keep main home urgent/overdue buckets fully aligned with /worklog (Workflow-based source).
    try:
        wf_base_q = _workflow_home_query(user_role)
        wf_open_q = wf_base_q.filter(
            _workflow_open_status_filter(),
            Workflow.due_date.isnot(None),
        )
        wf_urgent_q = wf_open_q.filter(
            Workflow.due_date >= today,
            Workflow.due_date <= urgent_date,
        )
        wf_overdue_q = wf_open_q.filter(Workflow.due_date < today)

        urgent_count = wf_urgent_q.order_by(None).count()
        overdue_count = wf_overdue_q.order_by(None).count()

        urgent_rows = wf_urgent_q.order_by(Workflow.due_date.asc()).limit(50).all()
        overdue_rows = wf_overdue_q.order_by(Workflow.due_date.asc()).limit(50).all()

        urgent_tasks = [_build_workflow_task_card(wf, matter, today) for wf, matter in urgent_rows]
        overdue_tasks = [
            _build_workflow_task_card(wf, matter, today) for wf, matter in overdue_rows
        ]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="main.index.workflow_bucket_override",
            log_key="main.index.workflow_bucket_override",
            log_window_seconds=300,
        )

    total_matters = db.session.query(func.count(VMatterOverview.matter_id)).scalar() or 0
    total_open_tasks = pending_count
    overdue_total = overdue_count

    finance_focus = _finance_focus(staff_pid, user_role)
    automation_review_count = None
    if user_role == ROLE_ADMIN and EmailMessage is not None and _table_exists("email_message"):
        try:
            automation_review_count = (
                db.session.query(func.count(EmailMessage.id))
                .filter(EmailMessage.processing_status.in_(["REVIEW", "READY", "EXTRACTED"]))
                .scalar()
                or 0
            )
        except Exception:
            automation_review_count = 0

    # Data Quality Check (Dummy/Zombie)
    try:
        from app.services.ops.data_quality import get_dummy_candidates

        # Start: Simple caching or check
        # For performance, maybe limit this checkNew But current volume is low.
        dummy_candidates = get_dummy_candidates(staff_pid)
    except Exception:
        dummy_candidates = []

    home_active_users, home_active_user_window_minutes = _home_active_users_snapshot()

    return render_template(
        "main/index.html",
        urgent_tasks=urgent_tasks,
        overdue_tasks=overdue_tasks,
        urgent_count=urgent_count,
        overdue_count=overdue_count,
        next_due=next_due,
        kpis=[
            {
                "label": "All matters",
                "value": total_matters,
                "meta": "Active docket records",
            },
            {
                "label": "Open tasks",
                "value": total_open_tasks,
                "meta": "Pending docket items",
            },
            {
                "label": "Due in 7 days",
                "value": due_soon_total,
                "meta": "Upcoming docket dates",
            },
            {
                "label": "Overdue tasks",
                "value": overdue_total,
                "meta": "Past due items",
            },
            {
                "label": "My open work",
                "value": my_open_count,
                "meta": "Assigned to me",
            },
        ],
        my_tasks=my_tasks,
        my_open_count=my_open_count,
        greeting=_greeting_for(now),
        today=today,
        finance_focus=finance_focus,
        automation_review_count=automation_review_count,
        dummy_candidates=dummy_candidates,
        home_active_users=home_active_users,
        home_active_user_window_minutes=home_active_user_window_minutes,
    )


def _is_global_risk_role(user_role: str | None) -> bool:
    role = (user_role or "").strip().lower()
    return role in {ROLE_ADMIN, ROLE_LEAD_ATTORNEY, ROLE_MGMT_DIRECTOR, ROLE_PARTNER_ATTORNEY}


def _risk_center_enabled() -> bool:
    return ConfigService.get_bool(RISK_CENTER_ENABLED_CONFIG_KEY, True, prefer_env=True)


def _risk_owner_condition(staff_party_id: str | None):
    pid = (staff_party_id or "").strip()
    if not pid:
        return false()
    return or_(
        MatterRiskFact.owner_staff_party_id == pid,
        MatterRiskFact.attorney_id == pid,
        MatterRiskFact.handler_id == pid,
        MatterRiskFact.manager_id == pid,
    )


def _risk_owner_matter_ids_select(staff_party_id: str | None):
    return select(MatterRiskFact.matter_id).where(_risk_owner_condition(staff_party_id)).distinct()


def _risk_center_accessible_matter_ids_select(*, can_global: bool, scope: str, owner_filter: str):
    if can_global and scope == "all":
        if owner_filter:
            return _risk_owner_matter_ids_select(owner_filter)
        return None
    if can_global:
        return _risk_owner_matter_ids_select(getattr(current_user, "staff_party_id", None))
    return policy_accessible_matter_ids_select(current_user)


def _matter_ids_from_select(matter_ids_select, *, limit: int) -> list[str]:
    if matter_ids_select is None:
        return []
    try:
        stmt = matter_ids_select.limit(max(1, int(limit or 1)))
    except AttributeError:
        stmt = matter_ids_select
    rows = db.session.execute(stmt).all()
    return [str(row[0]).strip() for row in rows if row and str(row[0] or "").strip()]


def _apply_matter_scope(query, matter_id_column, matter_ids_select):
    if matter_ids_select is None:
        return query
    return query.filter(matter_id_column.in_(matter_ids_select))


def _risk_summary(rows: list[tuple[MatterRiskFact, Matter, CaseFlatIndex | None]]) -> dict:
    levels = Counter()
    totals = {
        "deadline_reviews": 0,
        "automation_reviews": 0,
        "outstanding_total": 0.0,
        "overdue_items": 0,
    }
    for fact, _matter, _idx in rows:
        levels[fact.risk_level or "LOW"] += 1
        totals["deadline_reviews"] += int(fact.deadline_review_count or 0)
        totals["automation_reviews"] += int(fact.automation_review_count or 0)
        totals["outstanding_total"] += float(fact.outstanding_total or 0.0)
        totals["overdue_items"] += int(fact.overdue_deadline_count or 0) + int(
            fact.overdue_workflow_count or 0
        )
    totals["levels"] = dict(levels)
    totals["total"] = len(rows)
    return totals


@bp.route("/risk-center")
@login_required
def risk_center():
    if not _risk_center_enabled():
        abort(404)

    user_role = getattr(current_user, "role", None)
    staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
    can_global = _is_global_risk_role(user_role) or can_manage_case_globally(current_user)
    scope = (request.args.get("scope") or ("all" if can_global else "mine")).strip()
    if scope not in {"all", "mine"}:
        scope = "all" if can_global else "mine"
    if not can_global:
        scope = "mine"
    owner_filter = (request.args.get("owner") or "").strip() if can_global else ""
    team_filter = (request.args.get("team") or "").strip()
    if scope == "mine":
        owner_filter = staff_pid
    matter_ids_select = _risk_center_accessible_matter_ids_select(
        can_global=can_global,
        scope=scope,
        owner_filter=owner_filter,
    )

    refresh_result = None
    if request.args.get("refresh") == "1":
        try:
            refresh_matter_ids = (
                None
                if matter_ids_select is None
                else _matter_ids_from_select(matter_ids_select, limit=800)
            )
            if refresh_matter_ids == [] and matter_ids_select is not None:
                verification = {"matters": 0, "checked": 0, "open_issues": 0, "resolved": 0}
                risks = {"matters": 0, "updated": 0}
            else:
                verification = verify_deadline_queue(
                    matter_ids=refresh_matter_ids,
                    limit=300,
                    commit=False,
                )
                risks = refresh_matter_risk_facts(
                    matter_ids=refresh_matter_ids,
                    limit=800,
                    commit=False,
                )
            db.session.commit()
            refresh_result = {"verification": verification, "risks": risks}
        except Exception as exc:
            db.session.rollback()
            report_swallowed_exception(
                exc,
                context="main.risk_center.refresh",
                log_key="main.risk_center.refresh",
                log_window_seconds=300,
            )
            refresh_result = {"error": "refresh_failed"}
    elif db.session.query(func.count(MatterRiskFact.matter_id)).scalar() == 0:
        try:
            initial_matter_ids = (
                None
                if matter_ids_select is None
                else _matter_ids_from_select(matter_ids_select, limit=300)
            )
            if initial_matter_ids == [] and matter_ids_select is not None:
                refresh_result = {"risks": {"matters": 0, "updated": 0}}
            else:
                refresh_result = {
                    "risks": refresh_matter_risk_facts(
                        matter_ids=initial_matter_ids,
                        limit=300,
                        commit=False,
                    )
                }
                db.session.commit()
        except Exception as exc:
            db.session.rollback()
            report_swallowed_exception(
                exc,
                context="main.risk_center.initial_refresh",
                log_key="main.risk_center.initial_refresh",
                log_window_seconds=300,
            )

    risk_rows_query = risk_queue_query(
        owner_staff_party_id=(owner_filter or None) if can_global else None,
        team_key=team_filter or None,
    )
    risk_rows_query = _apply_matter_scope(
        risk_rows_query,
        MatterRiskFact.matter_id,
        matter_ids_select,
    )
    rows = risk_rows_query.limit(80).all()
    summary = _risk_summary(rows)

    deadline_reviews_query = (
        db.session.query(DeadlineReviewQueue, Matter)
        .join(Matter, DeadlineReviewQueue.matter_id == Matter.matter_id)
        .filter(DeadlineReviewQueue.status.in_(["OPEN", "REOPENED"]))
    )
    deadline_reviews_query = _apply_matter_scope(
        deadline_reviews_query,
        DeadlineReviewQueue.matter_id,
        matter_ids_select,
    )
    deadline_reviews = (
        deadline_reviews_query.order_by(
            case(
                (DeadlineReviewQueue.severity == "HIGH", 0),
                (DeadlineReviewQueue.severity == "MEDIUM", 1),
                else_=2,
            ),
            DeadlineReviewQueue.created_at.desc(),
        )
        .limit(30)
        .all()
    )
    recent_operations_query = Operation.query
    if not (can_global and scope == "all" and not owner_filter):
        recent_operations_query = recent_operations_query.filter(
            Operation.actor_id == getattr(current_user, "id", None)
        )
    recent_operations = (
        recent_operations_query.order_by(Operation.created_at.desc()).limit(20).all()
    )
    try:
        automation_feedback = collect_doc_type_feedback_metrics(window_days=30)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="main.risk_center.automation_feedback",
            log_key="main.risk_center.automation_feedback",
            log_window_seconds=300,
        )
        automation_feedback = {"doc_types": {}, "suggestions": []}

    teams_query = db.session.query(MatterRiskFact.team_key).filter(
        MatterRiskFact.team_key.isnot(None)
    )
    teams_query = _apply_matter_scope(
        teams_query,
        MatterRiskFact.matter_id,
        matter_ids_select,
    )
    teams = [
        row[0]
        for row in teams_query.distinct().order_by(MatterRiskFact.team_key.asc()).all()
        if row[0]
    ]

    return render_template(
        "main/risk_center.html",
        rows=rows,
        summary=summary,
        deadline_reviews=deadline_reviews,
        recent_operations=recent_operations,
        automation_feedback=automation_feedback,
        refresh_result=refresh_result,
        scope=scope,
        owner_filter=owner_filter,
        team_filter=team_filter,
        teams=teams,
        can_global=can_global,
    )


@bp.post("/risk-center/deadline-reviews/<int:queue_id>/resolve")
@login_required
def risk_center_resolve_deadline_review(queue_id: int):
    if not _risk_center_enabled():
        abort(404)

    note = (request.form.get("note") or "").strip()
    try:
        row = db.session.get(DeadlineReviewQueue, queue_id)
        if row is None:
            return redirect(url_for("main.risk_center"))
        if not can_access_matter(current_user, str(row.matter_id), action="view"):
            abort(403)
        before = {
            "status": row.status,
            "resolution_note": row.resolution_note,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }
        with OperationContext(
            action="deadline_review.resolve",
            risk_level="MEDIUM",
            undo_supported=False,
            targets_json={"deadline_review_id": queue_id, "matter_id": row.matter_id},
            summary_json={"note": note or "manual_resolved"},
            preop_backup_required=False,
        ) as op:
            resolve_deadline_review_item(
                queue_id,
                actor_id=getattr(current_user, "id", None),
                note=note,
                commit=False,
            )
            op.add_change(
                entity_type="DeadlineReviewQueue",
                entity_id=str(queue_id),
                change_type="resolve",
                before=before,
                after={
                    "status": row.status,
                    "resolution_note": row.resolution_note,
                    "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                },
            )
            op.commit()
        db.session.commit()
    except HTTPException:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        report_swallowed_exception(
            exc,
            context="main.risk_center_resolve_deadline_review",
            log_key="main.risk_center_resolve_deadline_review",
            log_window_seconds=300,
        )
    return redirect(url_for("main.risk_center"))


@bp.get("/sidebar/home-active-users")
@login_required
def home_active_users_sidebar():
    home_active_users, home_active_user_window_minutes = _home_active_users_snapshot()
    return render_template(
        "main/_active_users_sidebar.html",
        home_active_users=home_active_users,
        home_active_user_window_minutes=home_active_user_window_minutes,
    )


@bp.route("/dashboard/")
@login_required
def legacy_dashboard_redirect():
    # Legacy path kept for compatibility; Business Dashboard moved under /business/dashboard/
    from flask import redirect, url_for

    return redirect(url_for("dashboard.index"))
