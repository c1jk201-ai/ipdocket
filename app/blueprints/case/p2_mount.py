from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.services.billing.invoice_prefill import (
    build_invoice_create_url,
    resolve_invoice_create_base_url,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import require_matter_access


def register_p2_routes(bp):
    """
    P2  Existing case blueprint(bp) "" .
    status_routes.pyfrom blueprint Create  register_p2_routes(bp)   .
    """

    def _p2_enabled() -> bool:
        try:
            from flask import current_app

            return bool(current_app.config.get("CASE_P2_ENABLE", True))
        except Exception:
            return True

    def _parse_date(value: str | None) -> Optional[date]:
        s = (value or "").strip()
        if not s:
            return None
        try:
            # HTML <input type="date"> => YYYY-MM-DD
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    def _set_if(obj: Any, attr: str, value: Any) -> None:
        try:
            if hasattr(obj, attr):
                setattr(obj, attr, value)
        except Exception as exc:
            # Best-effort: some deployments may have read-only/missing attrs.
            report_swallowed_exception(
                exc,
                context="case.p2_mount._set_if",
                log_key="case.p2_mount._set_if",
                log_window_seconds=300,
            )

    @dataclass(frozen=True)
    class _Preset:
        key: str
        label: str
        # legal_due  to Draft/ Due date 
        draft_days_before: int
        submit_days_before: int
        # Create Task Title(Select)
        workflows: tuple[str, ...] = ()

    PRESETS: tuple[_Preset, ...] = (
        _Preset(
            "OA", "OA ", draft_days_before=14, submit_days_before=3, workflows=("OA ",)
        ),
        _Preset(
            "FILING",
            "Filing Open",
            draft_days_before=7,
            submit_days_before=0,
            workflows=("Filing ", " "),
        ),
        _Preset(
            "REG", "Registration/", draft_days_before=10, submit_days_before=2, workflows=("Registration ",)
        ),
        _Preset(
            "CLOSE", "", draft_days_before=0, submit_days_before=0, workflows=(" Process",)
        ),
    )

    def _preset_by_key(key: str) -> _Preset:
        for p in PRESETS:
            if p.key == key:
                return p
        return PRESETS[0]

    # -----------------------
    # (P2-10) Status  
    # -----------------------
    @bp.route("/matter/<matter_id>/status-wizard", methods=["GET", "POST"])
    def p2_status_wizard(matter_id: str):
        if not _p2_enabled():
            from flask import abort

            abort(404)

        from flask import flash, redirect, render_template, request, url_for
        from flask_login import current_user, login_required

        from app.extensions import db
        from app.models.matter import Matter
        from app.services.case.status_task_cleanup import apply_case_status_side_effects

        @login_required
        def _inner():
            require_matter_access(str(matter_id), action="edit_case")
            matter = Matter.query.get_or_404(matter_id)

            # Default the legal due date from the nearest docket due date.
            legal_due_default: Optional[date] = None
            try:
                from app.models.docket import DocketItem

                q = DocketItem.query.filter(DocketItem.matter_id == matter_id)
                if hasattr(DocketItem, "due_date"):
                    q = q.filter(DocketItem.due_date.isnot(None)).order_by(
                        DocketItem.due_date.asc()
                    )
                first = q.first()
                if first is not None:
                    legal_due_default = getattr(first, "due_date", None)
            except Exception:
                legal_due_default = None

            if request.method == "POST":
                old_status = (getattr(matter, "inhouse_status", None) or "").strip()
                new_status = (request.form.get("new_status") or "").strip()
                preset_key = (request.form.get("preset") or "OA").strip()
                create_dockets = (request.form.get("create_dockets") or "") == "y"
                create_workflows = (request.form.get("create_workflows") or "") == "y"
                legal_due = _parse_date(request.form.get("legal_due")) or legal_due_default

                preset = _preset_by_key(preset_key)

                if not new_status:
                    flash(" Status Select.", "warning")
                    return redirect(request.path)

                # Status Change
                _set_if(matter, "inhouse_status", new_status)
                _set_if(matter, "status", new_status)

                # Deadline/Task Create
                created_dockets = 0
                created_workflows = 0

                try:
                    if create_dockets and legal_due:
                        from sqlalchemy import or_

                        from app.models.docket import DocketItem

                        def _exists(name: str, d: date) -> bool:
                            try:
                                qq = DocketItem.query.filter(DocketItem.matter_id == matter_id)
                                name_filters = []
                                for attr_name in ("name_ref", "name_free", "name", "title"):
                                    col = getattr(DocketItem, attr_name, None)
                                    if col is not None:
                                        name_filters.append(col == name)
                                if name_filters:
                                    qq = qq.filter(or_(*name_filters))
                                due_col = getattr(DocketItem, "due_date", None)
                                if due_col is not None:
                                    qq = qq.filter(or_(due_col == d, due_col == d.isoformat()))
                                if hasattr(DocketItem, "is_deleted"):
                                    qq = qq.filter(
                                        or_(
                                            DocketItem.is_deleted == False,  # noqa: E712
                                            DocketItem.is_deleted.is_(None),
                                        )
                                    )
                                return qq.first() is not None
                            except Exception:
                                return False

                        draft_due = legal_due - timedelta(days=preset.draft_days_before)
                        submit_due = legal_due - timedelta(days=preset.submit_days_before)

                        docket_specs = [
                            ("Statutory Due date", legal_due, "HIGH"),
                            ("Draft Due date", draft_due, "MED"),
                            (" Due date", submit_due, "HIGH"),
                        ]

                        for name, d, pr in docket_specs:
                            if not d or _exists(name, d):
                                continue
                            di = DocketItem()
                            _set_if(di, "matter_id", matter_id)
                            _set_if(di, "category", "MGMT" if pr == "HIGH" else "WORK")
                            _set_if(di, "due_date", d)
                            _set_if(di, "extended_due_date", d)
                            _set_if(di, "priority", pr)
                            _set_if(di, "name_ref", name)
                            _set_if(di, "name_free", name)
                            _set_if(di, "name", name)
                            _set_if(di, "title", name)
                            owner = getattr(current_user, "staff_party_id", None) or getattr(
                                current_user, "id", None
                            )
                            _set_if(di, "owner_staff_party_id", str(owner or ""))
                            _set_if(di, "owner_id", getattr(current_user, "id", None))
                            _set_if(di, "assignee_id", getattr(current_user, "id", None))
                            db.session.add(di)
                            created_dockets += 1

                    if create_workflows:
                        # Task    " " Failed  .
                        try:
                            from app.models.workflow import Workflow

                            for title in preset.workflows:
                                existing = (
                                    Workflow.query.filter(
                                        Workflow.case_id == str(matter_id),
                                        Workflow.name == title,
                                        ~Workflow.status.in_(Workflow.TERMINAL_STATUSES),
                                    )
                                    .order_by(Workflow.id.asc())
                                    .first()
                                )
                                if existing:
                                    continue
                                wf = Workflow(
                                    case_id=str(matter_id),
                                    name=title,
                                    status="Pending",
                                )
                                _set_if(wf, "matter_id", matter_id)
                                _set_if(wf, "title", title)
                                _set_if(wf, "progress_status", "Pending")
                                _set_if(wf, "owner_id", getattr(current_user, "id", None))
                                _set_if(wf, "assignee_id", getattr(current_user, "id", None))
                                db.session.add(wf)
                                created_workflows += 1
                        except Exception:
                            created_workflows = 0

                    db.session.commit()
                    apply_case_status_side_effects(
                        matter_id=str(matter_id),
                        old_status=old_status,
                        new_status=new_status,
                        actor_id=getattr(current_user, "id", None),
                    )
                    flash(
                        f"Status changed to {new_status}. Created {created_dockets} deadline(s) and {created_workflows} task(s).",
                        "success",
                    )
                except Exception as e:
                    db.session.rollback()
                    flash(f"Status Change In Progress Error: {e}", "danger")

                # Matter  (Current table  , Legacy/  fallback)
                try:
                    return redirect(url_for("case_work.case_detail", case_id=matter_id))
                except Exception:
                    try:
                        return redirect(url_for("case.matter_view", matter_id=matter_id))
                    except Exception:
                        return redirect(f"/case/{matter_id}")

            # GET
            current_status = (
                getattr(matter, "inhouse_status", "") or getattr(matter, "status", "") or ""
            ).strip()
            # Status  config  
            try:
                from flask import current_app

                status_options = current_app.config.get(
                    "CASE_STATUS_OPTIONS",
                    ["OPEN", "FILING", "OA", "REG", "CLOSE"],
                )
            except Exception:
                status_options = ["OPEN", "FILING", "OA", "REG", "CLOSE"]

            return render_template(
                "case/status_wizard.html",
                matter=matter,
                current_status=current_status,
                status_options=status_options,
                presets=PRESETS,
                legal_due_default=legal_due_default,
            )

        return _inner()

    # -----------------------
    # (P2-11) ICS() 
    # -----------------------
    @bp.route("/matter/<matter_id>/dockets.ics", methods=["GET"])
    def p2_dockets_ics(matter_id: str):
        if not _p2_enabled():
            from flask import abort

            abort(404)

        from flask import make_response
        from flask_login import login_required

        from app.models.docket import DocketItem
        from app.models.matter import Matter
        from app.utils.ics import ICSEvent, build_calendar

        @login_required
        def _inner():
            require_matter_access(str(matter_id), action="view")
            matter = Matter.query.get_or_404(matter_id)
            events: list[ICSEvent] = []

            try:
                from sqlalchemy import or_

                q = DocketItem.query.filter(DocketItem.matter_id == matter_id)
                if hasattr(DocketItem, "is_deleted"):
                    q = q.filter(
                        or_(
                            DocketItem.is_deleted == False,  # noqa: E712
                            DocketItem.is_deleted.is_(None),
                        )
                    )
                if hasattr(DocketItem, "due_date"):
                    q = q.filter(DocketItem.due_date.isnot(None)).order_by(
                        DocketItem.due_date.asc()
                    )
                rows = q.all()

                for d in rows:
                    dd = getattr(d, "due_date", None)
                    if not dd:
                        continue

                    # Due Date Type Check (string or date)
                    if isinstance(dd, str):
                        try:
                            dd = datetime.strptime(dd, "%Y-%m-%d").date()
                        except ValueError:
                            continue

                    title = (
                        (getattr(d, "title", None) or "")
                        or (getattr(d, "name", None) or "")
                        or f"Deadline {getattr(d, 'id', '')}"
                    )
                    uid = f"docket-{getattr(d,'id','x')}@ipm"
                    desc = f"Matter: {getattr(matter,'title','') or getattr(matter,'name','') or matter_id}"
                    events.append(ICSEvent(uid=uid, summary=title, start=dd, description=desc))
            except Exception:
                # docket /     
                events = []

            cal_name = f"Matter {matter_id} Deadline"
            ics = build_calendar(events, cal_name=cal_name)
            resp = make_response(ics, 200)
            resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="matter-{matter_id}-dockets.ics"'
            )
            return resp

        return _inner()

    # -----------------------------------------
    # (P2-12) TC  → Invoice  " "
    #  -  Invoice DB  , Select TC JSON/CSV 
    #    Invoice  to Go (MVP)
    # -----------------------------------------
    @bp.route("/matter/<matter_id>/tc/to-invoice", methods=["GET", "POST"])
    def p2_tc_to_invoice(matter_id: str):
        if not _p2_enabled():
            from flask import abort

            abort(404)

        from flask import redirect, render_template, request
        from flask_login import login_required

        from app.models.matter import Matter
        from app.models.worklog import WorkLog

        @login_required
        def _inner():
            require_matter_access(str(matter_id), action="invoice")
            matter = Matter.query.get_or_404(matter_id)

            q = WorkLog.query.filter(WorkLog.matter_id == matter_id)
            # " Billing"   (     Process)
            try:
                from sqlalchemy import or_

                if hasattr(WorkLog, "billed_invoice_id"):
                    q = q.filter(
                        or_(WorkLog.billed_invoice_id.is_(None), WorkLog.billed_invoice_id == 0)
                    )
                if hasattr(WorkLog, "is_deleted"):
                    q = q.filter(
                        or_(
                            WorkLog.is_deleted == False,  # noqa: E712
                            WorkLog.is_deleted.is_(None),
                        )
                    )
            except Exception as exc:
                # Best-effort: optional filters depending on project schema.
                report_swallowed_exception(
                    exc,
                    context="case.p2_mount.p2_tc_to_invoice.query_filters",
                    log_key="case.p2_mount.p2_tc_to_invoice.query_filters",
                    log_window_seconds=300,
                )

            # 
            if hasattr(WorkLog, "work_date"):
                q = q.order_by(WorkLog.work_date.desc())
            elif hasattr(WorkLog, "created_at"):
                q = q.order_by(WorkLog.created_at.desc())

            rows = q.limit(300).all()

            selected_ids: list[int] = []
            if request.method == "POST":
                raw_ids = request.form.getlist("worklog_id")
                for rid in raw_ids:
                    try:
                        selected_ids.append(int(rid))
                    except (TypeError, ValueError):
                        pass

            selected = (
                [r for r in rows if int(getattr(r, "id", 0) or 0) in set(selected_ids)]
                if selected_ids
                else []
            )

            def _desc(w: Any) -> str:
                for k in ("title", "name", "memo", "description", "note"):
                    v = getattr(w, k, None)
                    if v:
                        return str(v)
                return f"Task Log #{getattr(w,'id','')}"

            def _amount(w: Any) -> float:
                for k in ("amount", "tc_amount", "price", "total"):
                    v = getattr(w, k, None)
                    if v is not None and v != "":
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return 0.0

            def _qty_and_unit(w: Any) -> tuple[float, float]:
                #  hours/price_per_hour   
                minutes = getattr(w, "minutes", None) or getattr(w, "duration_minutes", None)
                rate = getattr(w, "hourly_rate", None) or getattr(w, "rate", None)
                if minutes and rate:
                    try:
                        qty = round(float(minutes) / 60.0, 2)
                        unit = float(rate)
                        return qty, unit
                    except (TypeError, ValueError):
                        pass
                amt = _amount(w)
                return 1.0, float(amt)

            lines: list[dict[str, Any]] = []
            for w in selected:
                qty, unit = _qty_and_unit(w)
                amt = round(qty * unit, 2)
                lines.append(
                    {
                        "worklog_id": int(getattr(w, "id", 0) or 0),
                        "description": _desc(w),
                        "qty": qty,
                        "unit_price": unit,
                        "amount": amt,
                    }
                )

            invoice_create_url = "/accounting/invoice-system/invoices/create"
            try:
                from flask import current_app

                invoice_create_url = resolve_invoice_create_base_url(config=current_app.config)
                invoice_create_url = build_invoice_create_url(
                    invoice_create_url,
                    matter=matter,
                    matter_id=str(getattr(matter, "matter_id", "") or ""),
                    our_ref=str(getattr(matter, "our_ref", "") or ""),
                )
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="case.p2_mount.p2_tc_to_invoice.invoice_create_url",
                    log_key="case.p2_mount.p2_tc_to_invoice.invoice_create_url",
                    log_window_seconds=300,
                )

            return render_template(
                "case/tc_to_invoice.html",
                matter=matter,
                worklogs=rows,
                selected_ids=set(selected_ids),
                lines_json=json.dumps(lines, ensure_ascii=False, indent=2),
                invoice_create_url=invoice_create_url,
            )

        return _inner()

    @bp.route("/matter/<matter_id>/tc/to-invoice.csv", methods=["GET"])
    def p2_tc_to_invoice_csv(matter_id: str):
        if not _p2_enabled():
            from flask import abort

            abort(404)

        from flask import make_response, request
        from flask_login import login_required

        from app.models.worklog import WorkLog

        @login_required
        def _inner():
            require_matter_access(str(matter_id), action="invoice")
            ids: list[int] = []
            for part in (request.args.get("ids") or "").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ids.append(int(part))
                except (TypeError, ValueError):
                    pass

            if not ids:
                csv_text = "worklog_id,description,qty,unit_price,amount\r\n"
            else:
                rows = (
                    WorkLog.query.filter(
                        WorkLog.id.in_(ids),
                        WorkLog.matter_id == str(matter_id),
                    )
                    .order_by(WorkLog.id.asc())
                    .all()
                )
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["worklog_id", "description", "qty", "unit_price", "amount"])
                for r in rows:
                    desc = (
                        getattr(r, "title", None)
                        or getattr(r, "memo", None)
                        or f"Task Log #{getattr(r,'id','')}"
                    )
                    amt = getattr(r, "amount", None) or getattr(r, "tc_amount", None) or ""
                    w.writerow([getattr(r, "id", ""), desc, 1, amt, amt])
                csv_text = out.getvalue()

            resp = make_response(csv_text, 200)
            resp.headers["Content-Type"] = "text/csv; charset=utf-8"
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="matter-{matter_id}-invoice-lines.csv"'
            )
            return resp

        return _inner()
