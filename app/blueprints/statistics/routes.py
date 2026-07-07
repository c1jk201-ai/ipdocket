import csv
import io
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import Response, current_app, jsonify, render_template, request
from flask_login import login_required
from sqlalchemy import case, func, literal, or_
from sqlalchemy.exc import SQLAlchemyError

from app.blueprints.statistics import bp
from app.extensions import db
from app.models.ip_records import Matter
from app.models.user import User
from app.models.workflow import Workflow
from app.services.billing.invoice_services import InvoiceService
from app.utils.error_logging import report_swallowed_exception
from app.utils.tc import apply_tc_scope_filter, normalize_tc_scope
from app.utils.workflow_deadline_labels import strip_workflow_deadline_title_suffix
from app.utils.workflow_roles import (
    workflow_assignee_columns,
    workflow_primary_assignee_expr,
    workflow_user_filter,
)

def _workflow_display_name(value: object) -> str:
    return strip_workflow_deadline_title_suffix(value) or ""


def _get_date_range():
    """Parse optional Newstart=YYYY-MM-DD&end=YYYY-MM-DD query params into date objects."""
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    start = end = None
    try:
        if start_str:
            start = date.fromisoformat(start_str)
        if end_str:
            end = date.fromisoformat(end_str)
    except ValueError:
        start = end = None
    return start, end


def _default_date_range():
    """Return default date range (last 12 months) if not specified."""
    end = date.today()
    start = date(end.year - 1, end.month, 1)
    return start, end


def _try_int(value: object | None) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: object | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        pass
    try:
        n = Decimal(s)
    except (InvalidOperation, TypeError, ValueError):
        return 0
    if not n.is_finite():
        return 0
    return int(n.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _safe_pct(numerator: float, denominator: float, digits: int = 1) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, digits)


def _tc_basis() -> str:
    basis = (request.args.get("basis") or "").strip().lower()
    if basis in ("completed", "completed_date"):
        return "completed"
    if basis in ("due", "due_date"):
        return "due"
    return "created"


def _tc_scope() -> str:
    """
    TC target 
    - candidate(default): WORK  +  (, MGMT:* )
    - work: WORK 
    - all: All(Renewal )
    - mgmt: MGMT ()
    """
    default_scope = (
        (current_app.config.get("STATS_TC_SCOPE_DEFAULT") or "candidate").strip().lower()
    )
    return normalize_tc_scope(request.args.get("tc_scope"), default=default_scope)


def _tc_date_column(basis: str):
    if basis == "completed":
        return Workflow.completed_date, "date"
    if basis == "due":
        return Workflow.due_date, "date"
    return Workflow.created_at, "datetime"


def _apply_tc_scope_filter(q, scope: str):
    return apply_tc_scope_filter(q, scope)


def _apply_date_range(q, col, col_kind: str, start: date | None, end: date | None):
    if start:
        if col_kind == "datetime":
            q = q.filter(col >= datetime.combine(start, datetime.min.time()))
        else:
            q = q.filter(col >= start)
    if end:
        if col_kind == "datetime":
            q = q.filter(col <= datetime.combine(end, datetime.max.time()))
        else:
            q = q.filter(col <= end)
    return q


def _tc_case_ids(case_q: str) -> list[str]:
    term = (case_q or "").strip()
    if not term:
        return []

    term_lower = term.lower()
    try:
        rows = (
            Matter.query.filter(
                or_(
                    func.lower(Matter.our_ref).contains(term_lower),
                    func.lower(Matter.matter_id).contains(term_lower),
                )
            )
            .limit(2000)
            .all()
        )
        return [str(m.matter_id) for m in rows if getattr(m, "matter_id", None)]
    except SQLAlchemyError:
        return []


def _tc_base_query(*, start: date | None, end: date | None, basis: str, status: str | None):
    q = Workflow.query
    scope = _tc_scope()
    q = _apply_tc_scope_filter(q, scope)

    status = (status or "").strip()
    if status and status.lower() != "all":
        q = q.filter(Workflow.status == status)

    assignee_id = _try_int(request.args.get("assignee_id"))
    if assignee_id is not None:
        q = q.filter(workflow_user_filter(assignee_id))

    case_q = (request.args.get("case_q") or "").strip()
    if case_q:
        q = q.filter(Workflow.case_id.in_(_tc_case_ids(case_q)))

    date_col, col_kind = _tc_date_column(basis)
    q = _apply_date_range(q, date_col, col_kind, start, end)
    return q, date_col, col_kind, scope


def _tc_selected_assignee_id() -> int | None:
    return _try_int(request.args.get("assignee_id"))


def _tc_assignee_expr():
    selected_assignee_id = _tc_selected_assignee_id()
    if selected_assignee_id is not None:
        return literal(int(selected_assignee_id))
    return workflow_primary_assignee_expr()


def _month_expr(date_col) -> object:
    # - PostgreSQL: to_char
    # - SQLite: strftime
    dialect = ""
    try:
        bind = db.session.get_bind()
        dialect = (getattr(bind, "dialect", None) and bind.dialect.name) or ""
    except (AttributeError, RuntimeError, SQLAlchemyError):
        try:
            dialect = db.engine.dialect.name
        except (AttributeError, RuntimeError):
            dialect = ""
    dialect = (dialect or "").lower()

    if "sqlite" in dialect:
        return func.strftime("%Y-%m", date_col)
    return func.to_char(date_col, "YYYY-MM")


def _include_trace() -> bool:
    """
    API Error  trace  .
    - debug from 
    - Hidden by default; set API_ERROR_TRACE_ENABLED=1 when debug traces are needed.
    """
    try:
        return bool(current_app.debug) or bool(
            current_app.config.get("API_ERROR_TRACE_ENABLED", False)
        )
    except (AttributeError, RuntimeError):
        return False


def _error_response(exc: Exception, *, context: str, data=None):
    """
     Process In Progress 2 (NameError ) Reopen    ,
    report/trace  best-effort Process.
    """
    payload = {"error": str(exc)}
    if data is not None:
        payload["data"] = data
    try:
        from flask import g

        rid = getattr(g, "request_id", None)
        if rid:
            payload["request_id"] = rid
    except (AttributeError, RuntimeError) as exc:
        report_swallowed_exception(
            exc,
            context=f"{context}.request_id",
            log_key=f"{context}.request_id",
            log_window_seconds=300,
        )

    try:
        report_swallowed_exception(exc, context=context, log_key=context)
    except Exception as inner_exc:
        report_swallowed_exception(
            inner_exc,
            context=f"{context}.report_swallowed_exception",
            log_key=f"{context}.report_swallowed_exception",
            log_window_seconds=300,
        )

    if _include_trace():
        import traceback

        payload["trace"] = traceback.format_exc()
    return jsonify(payload), 500


# ========================== Page Routes ==========================


@bp.route("/")
@login_required
def index():
    return render_template("statistics/index.html", page="index")


@bp.route("/clients")
@login_required
def by_clients():
    return render_template("statistics/index.html", page="clients")


@bp.route("/costs")
@login_required
def by_costs():
    return render_template("statistics/index.html", page="costs")


@bp.route("/tc")
@login_required
def tc_stats():
    return render_template("statistics/index.html", page="tc")


@bp.route("/performance")
@login_required
def performance():
    return render_template("statistics/index.html", page="performance")


# ========================== API Endpoints ==========================


@bp.route("/api/clients")
@login_required
def api_clients():
    """
    Client Billing/  - Billing Invoices DB  (InvoiceService)
    """
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        limit = _try_int(request.args.get("limit")) or 30
        limit = max(1, min(limit, 500))

        sort_key = (request.args.get("sort") or "billed").strip().lower()
        if sort_key not in {"billed", "paid", "outstanding", "collection_rate", "client_name"}:
            sort_key = "billed"

        order = (request.args.get("order") or "desc").strip().lower()
        reverse = order != "asc"

        client_q = (request.args.get("client_q") or "").strip().lower()
        min_outstanding = _try_int(request.args.get("min_outstanding")) or 0
        min_outstanding = max(0, min_outstanding)

        fetch_limit = max(limit * 5, 200)
        fetch_limit = min(fetch_limit, 2000)
        rows = InvoiceService.get_client_statistics(
            start.isoformat(), end.isoformat(), limit=fetch_limit
        )

        data = []
        for row in rows:
            billed = _int_or_zero(row.get("total_billed"))
            paid = _int_or_zero(row.get("total_paid"))
            data.append(
                {
                    "client_id": row.get("client_id"),
                    "client_name": row.get("client_name") or f"Client #{row.get('client_id')}",
                    "total_billed": billed,
                    "total_paid": paid,
                    "outstanding": billed - paid,
                    "collection_rate": _safe_pct(paid, billed),
                }
            )

        if client_q:
            data = [
                x
                for x in data
                if client_q in str(x.get("client_name") or "").lower()
                or client_q in str(x.get("client_id") or "").lower()
            ]

        if min_outstanding > 0:
            data = [x for x in data if int(x.get("outstanding") or 0) >= min_outstanding]

        if sort_key == "client_name":
            data.sort(key=lambda x: str(x.get("client_name") or "").lower(), reverse=reverse)
        else:
            sort_field = {
                "billed": "total_billed",
                "paid": "total_paid",
                "outstanding": "outstanding",
                "collection_rate": "collection_rate",
            }.get(sort_key, "total_billed")
            data.sort(key=lambda x: float(x.get(sort_field) or 0), reverse=reverse)

        data = data[:limit]
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_clients")
        return _error_response(e, context="statistics.api_clients", data=[])


@bp.route("/api/costs")
@login_required
def api_costs():
    """
     Billing  - Billing Invoices DB  (InvoiceService)
    """
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        rows = InvoiceService.get_monthly_statistics(start.isoformat(), end.isoformat())

        data = []
        running_billed = 0
        running_paid = 0
        for row in rows:
            billed = _int_or_zero(row.get("billed"))
            paid = _int_or_zero(row.get("paid"))
            running_billed += billed
            running_paid += paid
            data.append(
                {
                    "month": row.get("month") or "",
                    "billed": billed,
                    "paid": paid,
                    "outstanding": billed - paid,
                    "collection_rate": _safe_pct(paid, billed),
                    "running_billed": running_billed,
                    "running_paid": running_paid,
                    "running_outstanding": running_billed - running_paid,
                }
            )
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_costs")
        return _error_response(e, context="statistics.api_costs", data=[])


@bp.route("/api/tc")
@login_required
def api_tc():
    """
    Contact Task(TC)  - Workflow.work_hours 
    """
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = request.args.get("status")
        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        # Contact Task (Default Contact: assignee -> attorney -> manager)
        assignee_expr = _tc_assignee_expr().label("assignee_id")
        q = base_q.with_entities(
            assignee_expr,
            func.sum(Workflow.work_hours).label("total_hours"),
            func.count(Workflow.id).label("task_count"),
        ).filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)

        rows = q.group_by(assignee_expr).all()

        # User Name Search
        user_map = {}
        user_ids = [r[0] for r in rows if r[0]]
        if user_ids:
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        data = []
        for assignee_id, total_hours, task_count in rows:
            data.append(
                {
                    "assignee_id": assignee_id,
                    "assignee_name": user_map.get(
                        assignee_id, "" if not assignee_id else f"User {assignee_id}"
                    ),
                    "total_hours": float(total_hours or 0),
                    "task_count": int(task_count or 0),
                }
            )

        #   
        data.sort(key=lambda x: x["total_hours"], reverse=True)

        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc")
        return _error_response(e, context="statistics.api_tc")


@bp.route("/api/tc/detail")
@login_required
def api_tc_detail():
    """
    Matter TC Details 
    """
    start, end = _get_date_range()
    if not start and not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = request.args.get("status")
        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        limit = _try_int(request.args.get("limit")) or 100
        limit = max(1, min(limit, 1000))

        assignee_expr = _tc_assignee_expr().label("assignee_id")
        q = base_q.with_entities(
            Workflow.case_id,
            Workflow.id,
            Workflow.name,
            Workflow.work_hours,
            assignee_expr,
            Workflow.status,
            Workflow.completed_date,
            Workflow.created_at,
        ).filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)

        rows = q.order_by(date_col.desc()).limit(limit).all()

        # Matter  Search
        case_ids = list({r[0] for r in rows if r[0]})
        case_map = {}
        if case_ids:
            matters = Matter.query.filter(Matter.matter_id.in_(case_ids)).all()
            case_map = {m.matter_id: m.our_ref or m.matter_id for m in matters}

        # User  Search
        # rows tuple: (case_id, workflow_id, name, work_hours, assignee_id, status, completed_date, created_at)
        user_ids = list({r[4] for r in rows if r[4]})
        user_map = {}
        if user_ids:
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        data = []
        for (
            case_id,
            workflow_id,
            name,
            work_hours,
            assignee_id,
            wf_status,
            completed_date,
            created_at,
        ) in rows:
            data.append(
                {
                    "case_id": case_id,
                    "our_ref": case_map.get(case_id, case_id),
                    "workflow_id": workflow_id,
                    "task_name": _workflow_display_name(name),
                    "work_hours": float(work_hours or 0),
                    "assignee_id": assignee_id,
                    "assignee_name": user_map.get(assignee_id, ""),
                    "status": wf_status or "",
                    "completed_date": completed_date.isoformat() if completed_date else "",
                    "created_at": created_at.isoformat() if created_at else "",
                }
            )

        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc_detail")
        return _error_response(e, context="statistics.api_tc_detail")


@bp.route("/api/tc/summary")
@login_required
def api_tc_summary():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = request.args.get("status")
        base_q, date_col, col_kind, tc_scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        completed_base_q, _, _, _ = _tc_base_query(
            start=start, end=end, basis=basis, status="Completed"
        )

        tc_q = base_q.filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)
        total_hours = float(tc_q.with_entities(func.sum(Workflow.work_hours)).scalar() or 0)
        tc_task_count = int(tc_q.with_entities(func.count(Workflow.id)).scalar() or 0)
        avg_hours = round((total_hours / tc_task_count) if tc_task_count else 0.0, 2)
        total_task_count = int(base_q.with_entities(func.count(Workflow.id)).scalar() or 0)

        completed_count = int(completed_base_q.with_entities(func.count(Workflow.id)).scalar() or 0)
        completed_tc_count = int(
            completed_base_q.filter(
                Workflow.work_hours.isnot(None),
                Workflow.work_hours > 0,
            )
            .with_entities(func.count(Workflow.id))
            .scalar()
            or 0
        )

        missing_q = completed_base_q.filter(
            or_(Workflow.work_hours.is_(None), Workflow.work_hours <= 0),
        )
        missing_count = int(missing_q.with_entities(func.count(Workflow.id)).scalar() or 0)

        case_count = int(
            tc_q.with_entities(func.count(func.distinct(Workflow.case_id))).scalar() or 0
        )
        assignee_expr = _tc_assignee_expr()
        assignee_count = int(
            tc_q.with_entities(func.count(func.distinct(assignee_expr))).scalar() or 0
        )
        hours_per_case = round((total_hours / case_count) if case_count else 0.0, 2)

        top_assignee_row = (
            tc_q.with_entities(func.sum(Workflow.work_hours).label("hours"))
            .group_by(assignee_expr)
            .order_by(func.sum(Workflow.work_hours).desc())
            .first()
        )
        top_assignee_hours = float((top_assignee_row[0] if top_assignee_row else 0) or 0)

        return jsonify(
            {
                "total_hours": round(total_hours, 2),
                "tc_task_count": tc_task_count,
                "avg_hours": avg_hours,
                "missing_count": missing_count,
                "case_count": case_count,
                "assignee_count": assignee_count,
                "total_task_count": total_task_count,
                "completed_count": completed_count,
                "completed_tc_count": completed_tc_count,
                "tc_coverage_rate": _safe_pct(completed_tc_count, completed_count),
                "hours_per_case": hours_per_case,
                "top_assignee_share": _safe_pct(top_assignee_hours, total_hours),
                "tc_scope": tc_scope,
            }
        )
    except Exception as e:
        current_app.logger.exception("Error in api_tc_summary")
        return _error_response(e, context="statistics.api_tc_summary")


@bp.route("/api/tc/assignees")
@login_required
def api_tc_assignees():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = (request.args.get("status") or "").strip()
        if status and status.lower() == "all":
            status = ""

        # NOTE: ignore assignee_id filter for dropdown population.
        q = Workflow.query
        q = _apply_tc_scope_filter(q, _tc_scope())
        if status:
            q = q.filter(Workflow.status == status)

        case_q = (request.args.get("case_q") or "").strip()
        if case_q:
            q = q.filter(Workflow.case_id.in_(_tc_case_ids(case_q)))

        date_col, col_kind = _tc_date_column(basis)
        q = _apply_date_range(q, date_col, col_kind, start, end)

        assignee_ids: set[int] = set()
        for col in workflow_assignee_columns():
            rows = q.with_entities(col).filter(col.isnot(None)).distinct().all()
            for row in rows:
                raw_uid = row[0] if row else None
                uid = _try_int(raw_uid)
                if uid is None:
                    continue
                if uid > 0:
                    assignee_ids.add(uid)
        ids = sorted(assignee_ids)
        user_map = {}
        if ids:
            users = User.query.filter(User.id.in_(ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        data = [{"assignee_id": None, "assignee_name": "All"}]
        for uid in ids:
            data.append({"assignee_id": uid, "assignee_name": user_map.get(uid, f"User {uid}")})
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc_assignees")
        return _error_response(e, context="statistics.api_tc_assignees")


@bp.route("/api/tc/monthly")
@login_required
def api_tc_monthly():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = request.args.get("status")
        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        month_expr = _month_expr(date_col).label("month")
        rows = (
            base_q.with_entities(
                month_expr,
                func.sum(Workflow.work_hours).label("total_hours"),
                func.count(Workflow.id).label("task_count"),
            )
            .filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)
            .group_by(month_expr)
            .order_by(month_expr)
            .all()
        )

        data = []
        for m, total_hours, task_count in rows:
            data.append(
                {
                    "month": m or "",
                    "total_hours": float(total_hours or 0),
                    "task_count": int(task_count or 0),
                }
            )
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc_monthly")
        return _error_response(e, context="statistics.api_tc_monthly")


@bp.route("/api/tc/by-case")
@login_required
def api_tc_by_case():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = request.args.get("status")
        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        limit = _try_int(request.args.get("limit")) or 20
        limit = max(1, min(limit, 200))

        rows = (
            base_q.with_entities(
                Workflow.case_id,
                func.sum(Workflow.work_hours).label("total_hours"),
                func.count(Workflow.id).label("task_count"),
            )
            .filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)
            .group_by(Workflow.case_id)
            .order_by(func.sum(Workflow.work_hours).desc())
            .limit(limit)
            .all()
        )

        case_ids = [r[0] for r in rows if r and r[0]]
        matter_map = {}
        if case_ids:
            matters = Matter.query.filter(Matter.matter_id.in_(case_ids)).all()
            matter_map = {
                m.matter_id: {
                    "our_ref": getattr(m, "our_ref", None) or m.matter_id,
                    "right_name": getattr(m, "right_name", None) or "",
                }
                for m in matters
            }

        data = []
        for case_id, total_hours, task_count in rows:
            mi = matter_map.get(case_id) or {}
            data.append(
                {
                    "case_id": case_id,
                    "our_ref": mi.get("our_ref") or case_id,
                    "right_name": mi.get("right_name") or "",
                    "total_hours": float(total_hours or 0),
                    "task_count": int(task_count or 0),
                }
            )
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc_by_case")
        return _error_response(e, context="statistics.api_tc_by_case")


@bp.route("/api/tc/missing")
@login_required
def api_tc_missing():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        status = "Completed"

        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        limit = _try_int(request.args.get("limit")) or 200
        limit = max(1, min(limit, 1000))

        assignee_expr = _tc_assignee_expr().label("assignee_id")
        q = base_q.with_entities(
            Workflow.case_id,
            Workflow.id,
            Workflow.name,
            assignee_expr,
            Workflow.status,
            Workflow.completed_date,
            Workflow.created_at,
        ).filter(or_(Workflow.work_hours.is_(None), Workflow.work_hours <= 0))

        rows = q.order_by(date_col.desc()).limit(limit).all()

        case_ids = list({r[0] for r in rows if r and r[0]})
        case_map = {}
        if case_ids:
            matters = Matter.query.filter(Matter.matter_id.in_(case_ids)).all()
            case_map = {m.matter_id: m.our_ref or m.matter_id for m in matters}

        user_ids = list({r[3] for r in rows if r and r[3]})
        user_map = {}
        if user_ids:
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        data = []
        for case_id, workflow_id, name, assignee_id, wf_status, completed_date, created_at in rows:
            data.append(
                {
                    "case_id": case_id,
                    "our_ref": case_map.get(case_id, case_id),
                    "workflow_id": workflow_id,
                    "task_name": _workflow_display_name(name),
                    "assignee_id": assignee_id,
                    "assignee_name": user_map.get(assignee_id, ""),
                    "status": wf_status or "",
                    "completed_date": completed_date.isoformat() if completed_date else "",
                    "created_at": created_at.isoformat() if created_at else "",
                }
            )
        return jsonify(data)
    except Exception as e:
        current_app.logger.exception("Error in api_tc_missing")
        return _error_response(e, context="statistics.api_tc_missing")


@bp.route("/api/tc/export.csv")
@login_required
def api_tc_export_csv():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        basis = _tc_basis()
        mode = (request.args.get("mode") or "").strip().lower() or "tc"
        status = request.args.get("status")
        if mode == "missing":
            status = "Completed"
        base_q, date_col, col_kind, _scope = _tc_base_query(
            start=start, end=end, basis=basis, status=status
        )

        limit = _try_int(request.args.get("limit")) or 2000
        limit = max(1, min(limit, 5000))

        q = base_q
        if mode == "missing":
            q = q.filter(or_(Workflow.work_hours.is_(None), Workflow.work_hours <= 0))
        else:
            q = q.filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)

        assignee_expr = _tc_assignee_expr().label("assignee_id")
        rows = (
            q.with_entities(
                Workflow.case_id,
                Workflow.id,
                Workflow.name,
                Workflow.business_code,
                Workflow.work_hours,
                assignee_expr,
                Workflow.status,
                Workflow.completed_date,
                Workflow.created_at,
            )
            .order_by(date_col.desc())
            .limit(limit)
            .all()
        )

        case_ids = list({r[0] for r in rows if r and r[0]})
        case_map = {}
        if case_ids:
            matters = Matter.query.filter(Matter.matter_id.in_(case_ids)).all()
            case_map = {m.matter_id: m.our_ref or m.matter_id for m in matters}

        user_ids = list({r[5] for r in rows if r and r[5]})
        user_map = {}
        if user_ids:
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "case_id",
                "our_ref",
                "workflow_id",
                "business_code",
                "task_name",
                "work_hours",
                "assignee_id",
                "assignee_name",
                "status",
                "completed_date",
                "created_at",
            ]
        )
        for (
            case_id,
            workflow_id,
            name,
            business_code,
            work_hours,
            assignee_id,
            wf_status,
            completed_date,
            created_at,
        ) in rows:
            w.writerow(
                [
                    case_id,
                    case_map.get(case_id, case_id),
                    workflow_id,
                    business_code or "",
                    _workflow_display_name(name),
                    work_hours if work_hours is not None else "",
                    assignee_id or "",
                    user_map.get(assignee_id, ""),
                    wf_status or "",
                    completed_date.isoformat() if completed_date else "",
                    created_at.isoformat() if created_at else "",
                ]
            )

        # Excel-friendly UTF-8 (BOM)
        csv_text = "\ufeff" + out.getvalue()
        resp = Response(csv_text, mimetype="text/csv; charset=utf-8")
        filename = f"tc_export_{date.today().isoformat()}.csv"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        current_app.logger.exception("Error in api_tc_export_csv")
        return _error_response(e, context="statistics.api_tc_export_csv")


@bp.route("/api/performance")
@login_required
def api_performance():
    """
     Task Done   Contact 
    """
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        #  Done items
        # - PostgreSQL: to_char
        # - SQLite: strftime
        dialect = ""
        try:
            bind = db.session.get_bind()
            dialect = (getattr(bind, "dialect", None) and bind.dialect.name) or ""
        except (AttributeError, RuntimeError, SQLAlchemyError):
            try:
                dialect = db.engine.dialect.name
            except (AttributeError, RuntimeError):
                dialect = ""
        dialect = (dialect or "").lower()

        completed_dt_candidates = [Workflow.completed_date]
        workflow_updated_at = getattr(Workflow, "updated_at", None)
        if workflow_updated_at is not None:
            completed_dt_candidates.append(workflow_updated_at)
        completed_dt_candidates.append(Workflow.created_at)
        completed_date_fallback = func.coalesce(*completed_dt_candidates)

        if "sqlite" in dialect:
            month_expr = func.strftime("%Y-%m", completed_date_fallback)
        else:
            month_expr = func.to_char(completed_date_fallback, "YYYY-MM")

        completed_base = Workflow.query.filter(
            Workflow.status == "Completed",
        )
        if start:
            completed_base = completed_base.filter(completed_date_fallback >= start)
        if end:
            completed_base = completed_base.filter(completed_date_fallback <= end)

        monthly_rows = (
            completed_base.with_entities(
                month_expr.label("month"),
                func.count(Workflow.id).label("completed_count"),
                func.sum(func.coalesce(Workflow.work_hours, 0)).label("total_hours"),
                func.sum(
                    case(
                        ((Workflow.work_hours.isnot(None)) & (Workflow.work_hours > 0), 1), else_=0
                    )
                ).label("tc_input_count"),
            )
            .group_by(month_expr)
            .order_by(month_expr)
            .all()
        )

        monthly_data = []
        for month, completed_count, total_hours, tc_input_count in monthly_rows:
            completed_count_i = int(completed_count or 0)
            total_hours_f = float(total_hours or 0)
            tc_input_count_i = int(tc_input_count or 0)
            monthly_data.append(
                {
                    "month": month or "",
                    "count": completed_count_i,
                    "total_hours": round(total_hours_f, 2),
                    "avg_hours": round(
                        (total_hours_f / tc_input_count_i) if tc_input_count_i else 0.0, 2
                    ),
                    "tc_coverage_rate": _safe_pct(tc_input_count_i, completed_count_i),
                }
            )

        # Contact Done items
        assignee_expr = workflow_primary_assignee_expr().label("assignee_id")
        assignee_rows = (
            completed_base.with_entities(
                assignee_expr,
                func.count(Workflow.id).label("completed_count"),
                func.sum(func.coalesce(Workflow.work_hours, 0)).label("total_hours"),
                func.sum(
                    case(
                        ((Workflow.work_hours.isnot(None)) & (Workflow.work_hours > 0), 1), else_=0
                    )
                ).label("tc_input_count"),
            )
            .group_by(assignee_expr)
            .all()
        )

        # User Name Search
        user_ids = [r[0] for r in assignee_rows if r[0]]
        user_map = {}
        if user_ids:
            users = User.query.filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u.username or u.display_name or f"User {u.id}" for u in users}

        assignee_data = []
        for assignee_id, count, hours, tc_input_count in assignee_rows:
            completed_count_i = int(count or 0)
            total_hours_f = float(hours or 0) if hours else 0
            tc_input_count_i = int(tc_input_count or 0)
            assignee_data.append(
                {
                    "assignee_id": assignee_id,
                    "assignee_name": user_map.get(
                        assignee_id, "" if not assignee_id else f"User {assignee_id}"
                    ),
                    "completed_count": completed_count_i,
                    "total_hours": total_hours_f,
                    "tc_input_count": tc_input_count_i,
                    "avg_hours": round(
                        (total_hours_f / tc_input_count_i) if tc_input_count_i else 0.0, 2
                    ),
                    "tc_coverage_rate": _safe_pct(tc_input_count_i, completed_count_i),
                }
            )

        assignee_data.sort(key=lambda x: (x["completed_count"], x["total_hours"]), reverse=True)

        completed_total = int(sum(int(x.get("completed_count") or 0) for x in assignee_data))
        completed_with_tc = int(sum(int(x.get("tc_input_count") or 0) for x in assignee_data))
        total_hours = float(sum(float(x.get("total_hours") or 0) for x in assignee_data))

        billed_total = 0
        paid_total = 0
        try:
            monthly_invoice = InvoiceService.get_monthly_statistics(
                start.isoformat(), end.isoformat()
            )
            billed_total = int(sum(_int_or_zero(r.get("billed")) for r in monthly_invoice))
            paid_total = int(sum(_int_or_zero(r.get("paid")) for r in monthly_invoice))
        except Exception:
            current_app.logger.warning(
                "Failed to compute invoice-backed KPI for performance dashboard",
                exc_info=True,
            )

        summary = {
            "completed_total": completed_total,
            "completed_with_tc": completed_with_tc,
            "tc_coverage_rate": _safe_pct(completed_with_tc, completed_total),
            "total_hours": round(total_hours, 2),
            "avg_hours_per_completed": round(
                (total_hours / completed_total) if completed_total else 0.0, 2
            ),
            "avg_hours_per_tc": round(
                (total_hours / completed_with_tc) if completed_with_tc else 0.0, 2
            ),
            "completed_per_hour": round((completed_total / total_hours) if total_hours else 0.0, 2),
            "invoice_billed": billed_total,
            "invoice_paid": paid_total,
            "invoice_collection_rate": _safe_pct(paid_total, billed_total),
            "invoice_per_tc_hour": round((billed_total / total_hours) if total_hours else 0.0, 1),
        }

        return jsonify({"monthly": monthly_data, "by_assignee": assignee_data, "summary": summary})
    except Exception as e:
        current_app.logger.exception("Error in api_performance")
        return _error_response(e, context="statistics.api_performance")


@bp.route("/api/clients/export.csv")
@login_required
def api_clients_export_csv():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        client_q = (request.args.get("client_q") or "").strip().lower()
        min_outstanding = _try_int(request.args.get("min_outstanding")) or 0
        min_outstanding = max(0, min_outstanding)

        rows = InvoiceService.get_client_statistics(start.isoformat(), end.isoformat(), limit=5000)

        data = []
        for row in rows:
            billed = _int_or_zero(row.get("total_billed"))
            paid = _int_or_zero(row.get("total_paid"))

            outstanding = billed - paid

            client_name = row.get("client_name") or f"Client #{row.get('client_id')}"

            if (
                client_q
                and client_q not in client_name.lower()
                and client_q not in str(row.get("client_id") or "").lower()
            ):
                continue
            if min_outstanding > 0 and outstanding < min_outstanding:
                continue

            data.append(
                {
                    "client_id": row.get("client_id") or "",
                    "client_name": client_name,
                    "total_billed": billed,
                    "total_paid": paid,
                    "outstanding": outstanding,
                    "collection_rate": _safe_pct(paid, billed),
                }
            )

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["ClientID", "Client name", "Invoice amount", "Amount", "Outstanding balance", "(%)"])
        for x in data:
            w.writerow(
                [
                    x["client_id"],
                    x["client_name"],
                    x["total_billed"],
                    x["total_paid"],
                    x["outstanding"],
                    x["collection_rate"],
                ]
            )

        csv_text = "\ufeff" + out.getvalue()
        resp = Response(csv_text, mimetype="text/csv; charset=utf-8")
        filename = f"clients_export_{date.today().isoformat()}.csv"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        current_app.logger.exception("Error in api_clients_export_csv")
        return _error_response(e, context="statistics.api_clients_export_csv")


@bp.route("/api/costs/export.csv")
@login_required
def api_costs_export_csv():
    start, end = _get_date_range()
    if not start or not end:
        start, end = _default_date_range()

    try:
        rows = InvoiceService.get_monthly_statistics(start.isoformat(), end.isoformat())

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "",
                "Invoice amount",
                "Amount",
                "Outstanding balance",
                "(%)",
                "TotalBilling",
                "Total",
                "Outstanding balance",
            ]
        )

        running_billed = 0
        running_paid = 0
        for row in rows:
            billed = _int_or_zero(row.get("billed"))
            paid = _int_or_zero(row.get("paid"))

            running_billed += billed
            running_paid += paid

            w.writerow(
                [
                    row.get("month") or "",
                    billed,
                    paid,
                    billed - paid,
                    _safe_pct(paid, billed),
                    running_billed,
                    running_paid,
                    running_billed - running_paid,
                ]
            )

        csv_text = "\ufeff" + out.getvalue()
        resp = Response(csv_text, mimetype="text/csv; charset=utf-8")
        filename = f"costs_export_{date.today().isoformat()}.csv"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        current_app.logger.exception("Error in api_costs_export_csv")
        return _error_response(e, context="statistics.api_costs_export_csv")


@bp.route("/api/performance/export.csv")
@login_required
def api_performance_export_csv():
    try:
        resp = api_performance()
        if resp.status_code != 200:
            return resp

        d = resp.get_json()
        out = io.StringIO()
        w = csv.writer(out)

        w.writerow(["=== Contact  ==="])
        w.writerow(
            [
                "ContactID",
                "Contact name",
                "Done items",
                "Total Task",
                "TC Registration items",
                " TC(h)",
                "TC Log(%)",
            ]
        )
        for x in d.get("by_assignee") or []:
            w.writerow(
                [
                    x.get("assignee_id") or "",
                    x.get("assignee_name") or "",
                    x.get("completed_count") or 0,
                    x.get("total_hours") or 0,
                    x.get("tc_input_count") or 0,
                    x.get("avg_hours") or 0,
                    x.get("tc_coverage_rate") or 0,
                ]
            )

        w.writerow([])
        w.writerow(["===   ==="])
        w.writerow(["", "Done items", "Total Task", " TC(h)", "TC Log(%)"])
        for x in d.get("monthly") or []:
            w.writerow(
                [
                    x.get("month") or "",
                    x.get("count") or 0,
                    x.get("total_hours") or 0,
                    x.get("avg_hours") or 0,
                    x.get("tc_coverage_rate") or 0,
                ]
            )

        w.writerow([])
        w.writerow(["===   ==="])
        summary = d.get("summary") or {}
        for k, v in summary.items():
            w.writerow([k, v])

        csv_text = "\ufeff" + out.getvalue()
        csv_resp = Response(csv_text, mimetype="text/csv; charset=utf-8")
        filename = f"performance_export_{date.today().isoformat()}.csv"
        csv_resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return csv_resp
    except Exception as e:
        current_app.logger.exception("Error in api_performance_export_csv")
        return _error_response(e, context="statistics.api_performance_export_csv")
