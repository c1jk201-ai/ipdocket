from datetime import date, timedelta

from flask import abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.blueprints.business import bp
from app.models.case import Case
from app.models.client import Client
from app.models.permissions import Permissions
from app.services.executive_analytics_service import build_executive_analytics
from app.utils.permissions import is_invoice_manager


def _get_date_range():
    """Parse optional Newstart=YYYY-MM-DD&end=YYYY-MM-DD into date objects."""
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


from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models.crm import CRMActivity, CRMLead, CRMOpportunity

# ... existing imports ...


@bp.route("/status")
@login_required
def status():
    return render_template("business/index.html")


def _require_business_accounting_access() -> None:
    if not is_invoice_manager(current_user):
        abort(403, "You do not have permission to access this business page.")


@bp.route("/accounting/reports")
@bp.route("/accounting/reports/")
@login_required
def accounting_reports_home():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import reports_home

    return reports_home()


@bp.route("/accounting/ledger")
@login_required
def accounting_general_ledger():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import general_ledger

    return general_ledger()


@bp.route("/accounting/ledger/accounts", methods=["GET", "POST"])
@login_required
def accounting_ledger_accounts():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import accounts

    return accounts()


@bp.route("/accounting/ledger/accounts/<int:account_id>/toggle", methods=["POST"])
@login_required
def accounting_ledger_toggle_account(account_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import toggle_account

    return toggle_account(account_id)


@bp.route("/accounting/ledger/journal")
@login_required
def accounting_journal():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal

    return journal()


@bp.route("/accounting/ledger/journal/new", methods=["GET", "POST"])
@login_required
def accounting_journal_new():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_new

    return journal_new()


@bp.route("/accounting/ledger/journal/<int:entry_id>")
@login_required
def accounting_journal_view(entry_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_view

    return journal_view(entry_id)


@bp.route("/accounting/ledger/journal/<int:entry_id>/approve", methods=["POST"])
@login_required
def accounting_journal_approve(entry_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_approve

    return journal_approve(entry_id)


@bp.route("/accounting/ledger/journal/<int:entry_id>/post", methods=["POST"])
@login_required
def accounting_journal_post(entry_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_post

    return journal_post(entry_id)


@bp.route("/accounting/ledger/journal/<int:entry_id>/reverse", methods=["POST"])
@login_required
def accounting_journal_reverse(entry_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_reverse

    return journal_reverse(entry_id)


@bp.route("/accounting/ledger/journal/<int:entry_id>/delete", methods=["POST"])
@login_required
def accounting_journal_delete(entry_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.ledger import journal_delete

    return journal_delete(entry_id)


@bp.route("/accounting/reports/trial-balance")
@login_required
def accounting_trial_balance_report():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import trial_balance_report

    return trial_balance_report()


@bp.route("/accounting/reports/income-statement")
@login_required
def accounting_income_statement_report():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import income_statement_report

    return income_statement_report()


@bp.route("/accounting/reports/balance-sheet")
@login_required
def accounting_balance_sheet_report():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import balance_sheet_report

    return balance_sheet_report()


@bp.route("/accounting/reports/period-close", methods=["GET", "POST"])
@login_required
def accounting_period_close():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import period_close

    return period_close()


@bp.route("/accounting/reports/period-close/<int:period_id>/reopen", methods=["POST"])
@login_required
def accounting_period_reopen(period_id: int):
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import period_reopen

    return period_reopen(period_id)


@bp.route("/accounting/reports/vat")
@login_required
def accounting_vat_report():
    _require_business_accounting_access()
    from app.blueprints.billing_invoices.routes.reports import vat_report

    return vat_report()


@bp.route("/api/kpi")
@login_required
def api_kpi():
    """Return simple KPI metrics for business dashboard."""
    start, end = _get_date_range()

    # Cases filtered by created_at when available
    q_cases = Case.query
    if hasattr(Case, "created_at"):
        if start:
            q_cases = q_cases.filter(Case.created_at >= start)
        if end:
            q_cases = q_cases.filter(Case.created_at <= end)
    total_cases = q_cases.count()

    # Clients: show overall count (no reliable created_at assumed)
    total_clients = Client.query.count()

    return jsonify(
        {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "metrics": {
                "total_cases": total_cases,
                "total_clients": total_clients,
                "total_invoices": None,
                "invoice_total": None,
                "invoice_paid": None,
                "invoice_outstanding": None,
            },
        }
    )


@bp.route("/")
@login_required
def index():
    if not is_invoice_manager(current_user):
        abort(403, "You do not have permission to access this business page.")
    from app.blueprints.billing_invoices.routes.core import dashboard as invoice_dashboard_view

    return invoice_dashboard_view()


@bp.route("/crm-dashboard")
@login_required
def crm_dashboard():
    """CRM Dashboard with key metrics and recent activities."""
    # Metrics
    new_leads_count = CRMLead.query.filter_by(status="new").count()
    open_opportunities = CRMOpportunity.query.filter(
        CRMOpportunity.stage.notin_(["closed_won", "closed_lost"])
    ).count()

    # Revenue forecast (sum of amount * probability / 100 for open opportunities)
    revenue_forecast = (
        db.session.query(
            func.coalesce(
                func.sum(
                    (
                        func.coalesce(CRMOpportunity.amount, 0)
                        * func.coalesce(CRMOpportunity.probability, 0)
                    )
                    / 100.0
                ),
                0,
            )
        )
        .filter(CRMOpportunity.stage.notin_(["closed_won", "closed_lost"]))
        .scalar()
    )

    # Won this month
    first_of_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    won_this_month = CRMOpportunity.query.filter(
        CRMOpportunity.stage == "closed_won", CRMOpportunity.closed_at >= first_of_month
    ).count()

    # Recent activities (last 10)
    recent_activities = CRMActivity.query.order_by(CRMActivity.activity_date.desc()).limit(10).all()

    # Recent clients (last 10)
    recent_clients = (
        Client.query.filter_by(is_deleted=False).order_by(Client.id.desc()).limit(10).all()
    )

    # Recent leads (last 10)
    recent_leads = CRMLead.query.order_by(CRMLead.created_at.desc()).limit(10).all()

    return render_template(
        "crm/dashboard.html",
        new_leads_count=new_leads_count,
        open_opportunities=open_opportunities,
        revenue_forecast=revenue_forecast,
        won_this_month=won_this_month,
        recent_activities=recent_activities,
        recent_clients=recent_clients,
        recent_leads=recent_leads,
    )


@bp.route("/executive")
@login_required
def executive():
    if not current_user.has_permission(Permissions.MENU_MGMT):
        abort(403)
    today = date.today()
    default_start = today - timedelta(days=365)
    start, end = _get_date_range()
    if not start:
        start = default_start
    if not end:
        end = today
    if end < start:
        start, end = end, start
    analytics = build_executive_analytics(start, end)
    return render_template("business/executive.html", analytics=analytics, start=start, end=end)
