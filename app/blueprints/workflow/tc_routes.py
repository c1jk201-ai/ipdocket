from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from flask import Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from app.blueprints.workflow import bp
from app.blueprints.workflow.routes import (
    _apply_tc_scope_filter,
    _parse_date,
    _tc_scope_value,
    _workflow_assignee_filter_for_user,
    _workflow_has_user,
)
from app.extensions import db
from app.models.ip_records import Matter
from app.models.user import User
from app.models.workflow import Workflow
from app.services.audit.entity_audit import record_entity_change_audit
from app.utils.permissions import require_matter_access
from app.utils.tc import parse_tc_hours


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_float(value: str | None) -> float | None:
    return parse_tc_hours(value)


@bp.get("/tc/<case_id>")
@login_required
def tc_report(case_id: str):
    matter = Matter.query.get_or_404(str(case_id))
    require_matter_access(str(matter.matter_id), action="view")

    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"))
    basis = (request.args.get("basis") or "").strip().lower() or "due"
    status = (request.args.get("status") or "").strip() or "all"
    show = (request.args.get("show") or "").strip().lower() or "all"
    assignee_id = _parse_int(request.args.get("assignee_id"))
    tc_scope = _tc_scope_value()

    if show == "missing" and status == "all":
        status = "Completed"

    q = Workflow.query.filter_by(case_id=str(case_id))
    q = _apply_tc_scope_filter(q, tc_scope)
    if status != "all":
        q = q.filter(Workflow.status == status)
    if assignee_id is not None:
        q = q.filter(_workflow_assignee_filter_for_user(assignee_id))

    date_col = Workflow.due_date
    date_is_datetime = False
    if basis == "created":
        date_col = Workflow.created_at
        date_is_datetime = True
    elif basis == "completed":
        date_col = Workflow.completed_date

    if start:
        if date_is_datetime:
            q = q.filter(date_col >= datetime.combine(start, datetime.min.time()))
        else:
            q = q.filter(date_col >= start)
    if end:
        if date_is_datetime:
            q = q.filter(date_col <= datetime.combine(end, datetime.max.time()))
        else:
            q = q.filter(date_col <= end)

    workflows = q.order_by(Workflow.due_date.asc(), Workflow.id.asc()).all()

    if show == "tc":
        workflows = [wf for wf in workflows if (wf.work_hours or 0) > 0]
    elif show == "missing":
        workflows = [wf for wf in workflows if (wf.work_hours is None or (wf.work_hours or 0) <= 0)]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "OurRef",
            "WorkflowID",
            "Status",
            "Priority",
            "Task/Task",
            "Handler",
            "Responsible attorney",
            "Manager",
            "Task",
            "Statutory deadline",
            "DraftDue date",
            "DraftDue date2",
            "Due date",
            "DraftSend",
            "",
            "page",
            "Task",
            "",
            "/Details Content",
            "Notes",
            "",
            "Create",
            "Create",
        ]
    )
    for wf in workflows:
        writer.writerow(
            [
                getattr(matter, "our_ref", "") or "",
                wf.id,
                wf.status or "",
                wf.priority or "",
                wf.name or "",
                (wf.assignee.username if wf.assignee else "") or "",
                (wf.attorney_assignee.username if getattr(wf, "attorney_assignee", None) else "")
                or "",
                (wf.inspector.username if getattr(wf, "inspector", None) else "") or "",
                (
                    wf.request_start_date.isoformat()
                    if getattr(wf, "request_start_date", None)
                    else ""
                ),
                (
                    wf.legal_due_date.isoformat()
                    if getattr(wf, "legal_due_date", None)
                    else (wf.due_date.isoformat() if wf.due_date else "")
                ),
                wf.draft_due_date.isoformat() if getattr(wf, "draft_due_date", None) else "",
                wf.draft_due_date2.isoformat() if getattr(wf, "draft_due_date2", None) else "",
                wf.submit_due_date.isoformat() if getattr(wf, "submit_due_date", None) else "",
                wf.draft_sent_date.isoformat() if getattr(wf, "draft_sent_date", None) else "",
                wf.submit_date.isoformat() if getattr(wf, "submit_date", None) else "",
                wf.page_count if getattr(wf, "page_count", None) is not None else "",
                wf.work_hours if getattr(wf, "work_hours", None) is not None else "",
                wf.difficulty if getattr(wf, "difficulty", None) is not None else "",
                wf.note or "",
                wf.send_memo or "",
                wf.completed_date.isoformat() if wf.completed_date else "",
                (wf.created_by.username if wf.created_by else "") or "",
                wf.created_at.isoformat() if wf.created_at else "",
            ]
        )

    filename = f"tc_{(getattr(matter, 'our_ref', None) or str(case_id)).replace('/', '_')}.csv"
    resp = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@bp.get("/tc/<case_id>/view")
@login_required
def tc_report_view(case_id: str):
    matter = Matter.query.get_or_404(str(case_id))
    require_matter_access(str(matter.matter_id), action="view")

    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"))
    basis = (request.args.get("basis") or "").strip().lower() or "due"
    status = (request.args.get("status") or "").strip() or "all"
    show = (request.args.get("show") or "").strip().lower() or "all"
    assignee_id = _parse_int(request.args.get("assignee_id"))

    if show == "missing" and status == "all":
        status = "Completed"

    tc_scope = _tc_scope_value()
    q = Workflow.query.filter_by(case_id=str(case_id))
    workflows = (
        _apply_tc_scope_filter(q, tc_scope)
        .order_by(Workflow.due_date.asc(), Workflow.id.asc())
        .all()
    )

    assignee_ids = sorted(
        {
            uid
            for wf in workflows
            for uid in (
                getattr(wf, "assignee_id", None),
                getattr(wf, "attorney_assignee_id", None),
                getattr(wf, "inspector_id", None),
            )
            if uid
        }
    )
    user_map: dict[int, str] = {}
    if assignee_ids:
        users = User.query.filter(User.id.in_(assignee_ids)).all()
        user_map = {u.id: (u.display_name or u.username or f"User {u.id}") for u in users}

    def _get_date_value(wf: Workflow):
        if basis == "created":
            return getattr(wf, "created_at", None)
        if basis == "completed":
            return getattr(wf, "completed_date", None)
        return getattr(wf, "due_date", None)

    def _in_range(value):
        if value is None:
            return False if (start or end) else True
        if start and value < start:
            return False
        if end and value > end:
            return False
        return True

    filtered = []
    for wf in workflows:
        if status != "all" and (wf.status or "") != status:
            continue
        if assignee_id is not None and not _workflow_has_user(wf, assignee_id):
            continue

        date_value = _get_date_value(wf)
        if basis == "created" and date_value is not None:
            if isinstance(date_value, datetime):
                date_value = date_value.date()
            elif not isinstance(date_value, date):
                date_value = None
        if not _in_range(date_value):
            continue
        filtered.append(wf)

    total_count = len(filtered)
    tc_rows = [wf for wf in filtered if (wf.work_hours or 0) > 0]
    tc_count = len(tc_rows)
    total_hours = float(sum((wf.work_hours or 0) for wf in tc_rows))
    missing_completed = [
        wf
        for wf in filtered
        if (wf.status or "") == "Completed" and (wf.work_hours is None or (wf.work_hours or 0) <= 0)
    ]
    missing_count = len(missing_completed)
    avg_hours = round((total_hours / tc_count) if tc_count else 0.0, 2)

    view_rows = filtered
    if show == "tc":
        view_rows = tc_rows
    elif show == "missing":
        view_rows = missing_completed

    return render_template(
        "workflow/tc_report.html",
        matter=matter,
        workflows=view_rows,
        summary={
            "total_count": total_count,
            "tc_count": tc_count,
            "missing_count": missing_count,
            "total_hours": round(total_hours, 2),
            "avg_hours": avg_hours,
        },
        filters={
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "basis": basis,
            "status": status,
            "assignee_id": str(assignee_id) if assignee_id is not None else "",
            "show": show,
            "tc_scope": tc_scope,
        },
        assignee_options=[
            {"id": uid, "name": user_map.get(uid, f"User {uid}")} for uid in assignee_ids
        ],
        csv_url=url_for("workflow.tc_report", case_id=case_id, **request.args),
    )


@bp.route("/tc/my", methods=["GET", "POST"])
@login_required
def tc_my():
    if request.method == "POST":
        tc_scope = _tc_scope_value(from_form=True)
        ids = []
        for raw_id in request.form.getlist("workflow_id"):
            try:
                workflow_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if workflow_id > 0:
                ids.append(workflow_id)

        updated = 0
        invalid = 0
        try:
            if ids:
                rows = Workflow.query.filter(
                    Workflow.id.in_(ids),
                    _workflow_assignee_filter_for_user(current_user.id),
                ).all()
                wf_map = {wf.id: wf for wf in rows if wf.id}
                for wf_id in ids:
                    wf = wf_map.get(wf_id)
                    if not wf:
                        continue
                    raw = request.form.get(f"work_hours_{wf_id}")
                    if raw is None:
                        continue
                    if not str(raw).strip():
                        if wf.work_hours is None:
                            continue
                        old_value = wf.work_hours
                        wf.work_hours = None
                        record_entity_change_audit(
                            action="workflow.update",
                            target_type="workflow",
                            target_id=wf.id,
                            actor_id=getattr(current_user, "id", None),
                            changes={"work_hours": {"from": old_value, "to": None}},
                            meta={
                                "matter_id": str(getattr(wf, "case_id", "") or ""),
                                "source": "workflow.tc_my",
                            },
                            title=str(getattr(wf, "name", None) or f"Workflow #{wf.id}"),
                        )
                        updated += 1
                        continue
                    val = _parse_float(str(raw))
                    if val is None or val < 0:
                        invalid += 1
                        continue
                    if (wf.work_hours or 0) == val:
                        continue
                    old_value = wf.work_hours
                    wf.work_hours = val
                    record_entity_change_audit(
                        action="workflow.update",
                        target_type="workflow",
                        target_id=wf.id,
                        actor_id=getattr(current_user, "id", None),
                        changes={"work_hours": {"from": old_value, "to": val}},
                        meta={
                            "matter_id": str(getattr(wf, "case_id", "") or ""),
                            "source": "workflow.tc_my",
                        },
                        title=str(getattr(wf, "name", None) or f"Workflow #{wf.id}"),
                    )
                    updated += 1

                if updated:
                    db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Bulk TC update failed")
            flash("TC save failed.", "danger")
        else:
            if updated:
                flash(f"TC saved. ({updated} item(s))", "success")
            if invalid:
                flash(
                    f"Some TC values could not be saved. ({invalid} item(s))",
                    "warning",
                )

        redirect_args = {}
        for key in ("start", "end", "basis", "status", "show", "case_q", "limit"):
            value = (request.form.get(key) or "").strip()
            if value:
                redirect_args[key] = value
        redirect_args["tc_scope"] = tc_scope
        return redirect(url_for("workflow.tc_my", **redirect_args))

    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"))
    basis = (request.args.get("basis") or "").strip().lower() or "completed"
    status = (request.args.get("status") or "").strip() or "all"
    show = (request.args.get("show") or "").strip().lower() or "missing"
    tc_scope = _tc_scope_value()
    case_q = (request.args.get("case_q") or "").strip()
    try:
        limit = int((request.args.get("limit") or "").strip() or 300)
    except (TypeError, ValueError):
        limit = 300
    limit = max(50, min(limit, 1000))

    if not start and not end:
        end = date.today()
        start = end - timedelta(days=30)

    if show == "missing" and status == "all":
        status = "Completed"

    date_col = Workflow.completed_date
    date_is_datetime = False
    if basis == "created":
        date_col = Workflow.created_at
        date_is_datetime = True
    elif basis == "due":
        date_col = Workflow.due_date

    q = Workflow.query.join(Matter, Workflow.case_id == Matter.matter_id).filter(
        _workflow_assignee_filter_for_user(current_user.id)
    )
    q = _apply_tc_scope_filter(q, tc_scope)

    if status != "all":
        q = q.filter(Workflow.status == status)

    if case_q:
        term = case_q.lower()
        q = q.filter(
            or_(
                func.lower(Matter.our_ref).contains(term),
                func.lower(Matter.matter_id).contains(term),
            )
        )

    if start:
        if date_is_datetime:
            q = q.filter(date_col >= datetime.combine(start, datetime.min.time()))
        else:
            q = q.filter(date_col >= start)
    if end:
        if date_is_datetime:
            q = q.filter(date_col <= datetime.combine(end, datetime.max.time()))
        else:
            q = q.filter(date_col <= end)

    if show == "tc":
        q = q.filter(Workflow.work_hours.isnot(None), Workflow.work_hours > 0)
    elif show == "missing":
        q = q.filter(or_(Workflow.work_hours.is_(None), Workflow.work_hours <= 0))

    workflows = q.order_by(date_col.desc(), Workflow.id.desc()).limit(limit).all()

    total_hours = float(sum((wf.work_hours or 0) for wf in workflows if (wf.work_hours or 0) > 0))
    tc_count = sum(1 for wf in workflows if (wf.work_hours or 0) > 0)
    missing_count = sum(
        1
        for wf in workflows
        if (wf.status or "") == "Completed" and (wf.work_hours is None or (wf.work_hours or 0) <= 0)
    )

    export_url = url_for(
        "stats.api_tc_export_csv",
        mode=("missing" if show == "missing" else "tc"),
        assignee_id=current_user.id,
        tc_scope=tc_scope,
        start=start.isoformat() if start else "",
        end=end.isoformat() if end else "",
        basis=basis,
        status=status,
        case_q=case_q,
        limit=5000,
    )

    return render_template(
        "workflow/tc_my.html",
        workflows=workflows,
        summary={
            "count": len(workflows),
            "total_hours": round(total_hours, 2),
            "tc_count": tc_count,
            "missing_count": missing_count,
        },
        filters={
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "basis": basis,
            "status": status,
            "show": show,
            "tc_scope": tc_scope,
            "case_q": case_q,
            "limit": str(limit),
        },
        export_url=export_url,
    )
