from flask import current_app, jsonify, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from app.blueprints.billing_invoices.routes.core import api_summary as invoice_summary
from app.blueprints.dashboard import bp
from app.extensions import db
from app.models.case import Case
from app.models.invoice import Invoice
from app.utils.permissions import is_invoice_manager


def _rollback_session() -> None:
    try:
        db.session.rollback()
    except Exception as exc:
        current_app.logger.debug("dashboard.metrics rollback failed: %s", exc)


@bp.route("/")
@login_required
def index():
    return render_template("dashboard/index.html")


@bp.route("/metrics")
@login_required
def metrics():
    # Case stage counts: heuristic mapping by status
    total_cases = Case.query.count()
    new_cases = Case.query.filter(func.lower(func.coalesce(Case.status, "")) == "pending").count()
    registered_cases = Case.query.filter(
        func.lower(func.coalesce(Case.status, "")) == "registered"
    ).count()
    mid_cases = max(total_cases - new_cases - registered_cases, 0)

    receivables_by_currency = {}
    receivables_locked = False
    if is_invoice_manager(current_user):
        try:
            summary_resp = invoice_summary()
            summary_data = summary_resp.get_json() if summary_resp else None
            receivables_by_currency = (summary_data or {}).get("value", {}).get("ar", {}).get(
                "outstanding_total_by_currency", {}
            ) or {}
        except Exception:
            _rollback_session()
            receivables_by_currency = {}
    else:
        receivables_locked = True

    # Unbilled: cases without any invoice
    try:
        unbilled_cases = (
            Case.query.outerjoin(Invoice, Invoice.case_id == Case.id)
            .filter(Invoice.id == None)  # noqa: E711
            .count()
        )
    except Exception:
        _rollback_session()
        unbilled_cases = 0

    # Holdings: by case_type and division distributions
    try:
        by_type = (
            Case.query.with_entities(Case.case_type, func.count()).group_by(Case.case_type).all()
        )
        by_division = (
            Case.query.with_entities(Case.division, func.count()).group_by(Case.division).all()
        )
    except Exception:
        _rollback_session()
        by_type = []
        by_division = []

    return jsonify(
        {
            "cases": {
                "total": total_cases,
                "new": new_cases,
                "mid": mid_cases,
                "registered": registered_cases,
            },
            "finance": {
                "receivables_by_currency": receivables_by_currency,
                "receivables_locked": receivables_locked,
                "unbilled_cases": unbilled_cases,
            },
            "holdings": {
                "by_type": {k or "UNKNOWN": v for k, v in by_type},
                "by_division": {k or "UNKNOWN": v for k, v in by_division},
            },
        }
    )
