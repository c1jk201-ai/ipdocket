from flask import Blueprint, abort, jsonify, redirect, request, url_for
from flask_login import current_user

from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_invoice_manager

bp = Blueprint(
  "billing_invoices",
  __name__,
  template_folder="../../templates/billing_invoices",
  static_folder="../../static/billing_invoices",
)


@bp.before_request
def _guard_invoice_system_access():
  # Allow unauthenticated health checks and robots
  try:
    ep = (request.endpoint or "").strip().lower()
    if ep.endswith(".healthcheck") or ep.endswith(".robots_txt") or ep.endswith(".static"):
      return None
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices._guard_invoice_system_access.endpoint",
      log_key="billing_invoices._guard_invoice_system_access.endpoint",
      log_window_seconds=300,
    )

  def _is_api_request() -> bool:
    try:
      path = (request.path or "").lower()
      if "/accounting/invoice-system/api/" in path:
        return True
      if path.endswith("/accounting/invoice-system/api"):
        return True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices._guard_invoice_system_access.is_api_request.path",
        log_key="billing_invoices._guard_invoice_system_access.is_api_request.path",
        log_window_seconds=300,
      )
    try:
      accept = (request.headers.get("Accept") or "").lower()
      if "application/json" in accept:
        return True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices._guard_invoice_system_access.is_api_request.accept",
        log_key="billing_invoices._guard_invoice_system_access.is_api_request.accept",
        log_window_seconds=300,
      )
    return False

  # Always require login
  if not current_user.is_authenticated:
    if _is_api_request():
      return (
        jsonify(
          {
            "ok": False,
            "error": {"code": "unauthenticated", "message": " required."},
          }
        ),
        401,
      )
    return redirect(url_for("auth.login", next=request.path))

  # Require invoice permission group
  if not is_invoice_manager(current_user):
    if _is_api_request():
      return (
        jsonify(
          {
            "ok": False,
            "error": {"code": "forbidden", "message": "Permission denied."},
          }
        ),
        403,
      )
    abort(403, "Permission denied.")

  return None


@bp.route("/taxinvoice/drafts")
def legacy_taxinvoice_drafts():
  return redirect(url_for("billing_invoices.invoices.tax_issue", **request.args.to_dict()))


from .routes.admin import bp as admin_bp
from .routes.aging import bp as aging_bp
from .routes.case_matching import bp as case_matching_bp
from .routes.clients import bp as clients_bp
from .routes.core import bp as core_bp
from .routes.expenses import bp as expenses_bp
from .routes.guardrail import bp as guardrail_bp

# Route registration
from .routes.invoices import bp as invoices_bp
from .routes.ledger import bp as ledger_bp
from .routes.reports import bp as reports_bp
from .routes.templates_bp import bp as templates_bp_bp

try:
  from .routes.bank_activity import bp as bank_activity_bp
except ModuleNotFoundError:
  bank_activity_bp = None

# Register child blueprints with legacy naming compatibility
# The prefix is relative to billing_invoices prefix (/accounting/invoice-system)
# So /accounting/invoice-system/invoices becomes billing_invoices.invoices.*

bp.register_blueprint(invoices_bp, url_prefix="/invoices")
bp.register_blueprint(clients_bp, url_prefix="/clients")
bp.register_blueprint(core_bp, url_prefix="/") # Core handles dashboard

bp.register_blueprint(aging_bp, url_prefix="/aging")
if bank_activity_bp is not None:
  bp.register_blueprint(bank_activity_bp, url_prefix="/bank_activity")
bp.register_blueprint(templates_bp_bp, url_prefix="/templates")
bp.register_blueprint(case_matching_bp, url_prefix="/case-matching")
bp.register_blueprint(admin_bp, url_prefix="/admin")
bp.register_blueprint(expenses_bp, url_prefix="/expenses")
bp.register_blueprint(guardrail_bp, url_prefix="/guardrail")
bp.register_blueprint(ledger_bp, url_prefix="/ledger")
bp.register_blueprint(reports_bp, url_prefix="/reports")

# Import context processors to register them
# Import context processors to register them
from . import context

context.register_jinja(bp)
