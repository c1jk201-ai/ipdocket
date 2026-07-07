import json
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import current_app
from flask import url_for as _url_for
from werkzeug.routing import BuildError

from app.services.billing.i18n import get_locale, t, t_lang
from app.services.billing.utils import (
  currency_format,
  currency_format_minor,
  format_notes,
  from_minor,
  invoice_logo_url,
  status_label,
)
from app.utils.permissions import get_invoice_roles, get_management_roles

from .auth import get_current_user

# We are using a unified blueprint "billing_invoices" with child blueprints or route prefixes.
# If we mistakenly use 'invoices.list', we want it to map to 'billing_invoices.invoices.list'
# (assuming 'invoices' is the child bp name).
# Let's verify the child blueprint names when we port them.
# The legacy ones were 'invoices', 'clients', etc.
# If we register them as children of 'billing_invoices', the usage will be 'billing_invoices.invoices.list'.


def _url_for_compat(endpoint: str, **values):
  """
  Auto-corrects legacy endpoint names to include the new blueprint prefix.
  """
  try:
    return _url_for(endpoint, **values)
  except BuildError:
    # Known legacy endpoints that might be called without blueprint prefix in templates
    # Map endpoint name -> child blueprint name
    _legacy_shorts = {
      "list_invoices": "invoices",
      "new_invoice": "invoices",
      "view_invoice": "invoices",
      "edit_invoice": "invoices",
      "tax_issue": "invoices",
      "update_invoice_status": "invoices",
      "bulk_update_status": "invoices",
      "bulk_delete": "invoices",
      "export_invoices": "invoices",
      "view_client": "clients",
      "edit_client": "clients",
      "new_client": "clients",
      "list_clients": "clients",
      "dashboard": "core",
      "aging_report": "aging",
      "list_templates": "templates_bp",
      "new_template": "templates_bp",
      "export_all_templates": "templates_bp",
      "import_templates": "templates_bp",
      "export_template": "templates_bp",
      "edit_template": "templates_bp",
      "copy_template": "templates_bp",
      "delete_template": "templates_bp",
      "case_invoice_match": "case_matching",
      "fetch_case_info": "case_matching",
      "case_search": "case_matching",
      "list_users": "admin",
      "new_user": "admin",
      "edit_user": "admin",
      "toggle_user": "admin",
      "reset_password": "admin",
    }

    # Check if the endpoint handles specific legacy short name
    if endpoint in _legacy_shorts:
      child = _legacy_shorts[endpoint]
      target = f"billing_invoices.{child}.{endpoint}"
      try:
        return _url_for(target, **values)
      except BuildError:
        pass

    # Try prepending 'billing_invoices.' + child name logic if needed.
    # Legacy usually did `url_for('invoices.list')`.
    # If we just registered the child blueprints directly, we wouldn't need this wrapper for them,
    # but the request asked to "embed" which implies a containing structure.
    # If we made 'billing_invoices' a blueprint and registered 'invoices' blueprint ON it,
    # the endpoint is 'billing_invoices.invoices.list'.

    # Check if the endpoint already has a dot
    parts = endpoint.split(".")
    if len(parts) > 1:
      # e.g. "invoices.list" -> "billing_invoices.invoices.list"
      try:
        return _url_for("billing_invoices." + endpoint, **values)
      except BuildError:
        pass

    # Also try just prepending 'billing_invoices.' for top-level routes of the BPNew
    try:
      return _url_for("billing_invoices." + endpoint, **values)
    except BuildError:
      pass

    raise


def register_jinja(app):
  # Helper to support both App and Blueprint
  def add_filter(func, name):
    if hasattr(app, "add_app_template_filter"):
      app.add_app_template_filter(func, name)
    else:
      app.add_template_filter(func, name)

  # Filters
  def _currency(amount, cur):
    return currency_format(Decimal(amount), cur)

  add_filter(_currency, "currency")

  def _currency_minor(amount_minor, cur):
    return currency_format_minor(int(amount_minor or 0), cur)

  add_filter(_currency_minor, "currency_minor")

  def _from_json(s):
    # Handle already-parsed data from PostgreSQL's JSON/JSONB columns
    if s is None:
      return []
    if isinstance(s, (dict, list)):
      return s
    try:
      return json.loads(s) if s else []
    except (json.JSONDecodeError, TypeError):
      return []

  add_filter(_from_json, "from_json")

  add_filter(invoice_logo_url, "invoice_logo_url")

  def _configured_date_format() -> str:
    return current_app.config.get("DATE_FORMAT", "%m/%d/%Y")

  def _configured_datetime_format() -> str:
    return current_app.config.get("DATETIME_FORMAT", "%m/%d/%Y %I:%M:%S %p")

  def _configured_datetime_minute_format() -> str:
    return current_app.config.get("DATETIME_MINUTE_FORMAT", "%m/%d/%Y %I:%M %p")

  def _date_only(value):
    s = ("" if value is None else str(value)).strip()
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
      return s[:10]
    return s

  def _us_date(value, fmt: str | None = None):
    if not value:
      return ""
    out_fmt = fmt or _configured_date_format()
    try:
      if isinstance(value, datetime):
        return _local_dt(value, out_fmt)
      if isinstance(value, date):
        return value.strftime(out_fmt)
      s = str(value).strip()
      iso = _date_only(s)
      try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime(out_fmt)
      except ValueError:
        try:
          return datetime.fromisoformat(s).strftime(out_fmt)
        except ValueError:
          return s
    except (ValueError, TypeError, OverflowError):
      return str(value)

  add_filter(_us_date, "us_date")
  add_filter(_us_date, "local_date")

  def _local_dt(value, fmt: str | None = None):
    if not value:
      return ""
    out_fmt = fmt or _configured_datetime_format()
    tzname = current_app.config.get("TIMEZONE", "America/New_York")
    try:
      if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime(out_fmt if fmt else _configured_date_format())
      dt = None
      if isinstance(value, datetime):
        dt = value
      else:
        s = str(value).strip()
        try:
          dt = datetime.fromisoformat(s)
        except ValueError:
          try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
          except ValueError:
            return s
      if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
      local = dt.astimezone(ZoneInfo(tzname))
      return local.strftime(out_fmt)
    except (ValueError, TypeError, OverflowError):
      return str(value)

  add_filter(_local_dt, "local_dt")
  add_filter(_local_dt, "local_datetime")

  def _local_dt_min(value):
    return _local_dt(value, _configured_datetime_minute_format())

  add_filter(_local_dt_min, "local_dt_min")

  def inject():
    def is_admin():
      user = get_current_user()
      try:
        return bool(user and (user["role"] == "admin"))
      except (KeyError, TypeError):
        return False

    def has_role(*roles):
      user = get_current_user()
      if not user:
        return False
      try:
        user_role = (user.get("role") or "").strip().lower()
      except Exception:
        return False
      if not user_role:
        return False

      # 1. Admin superuser bypass (match route decorators behavior)
      if user_role == "admin":
        return True

      # 2. Expand role groups (match billing_invoices.auth.role_required)
      extended_roles = {str(r).strip().lower() for r in (roles or ()) if str(r).strip()}
      if "manager" in extended_roles:
        extended_roles.update(get_management_roles())
      if "staff" in extended_roles:
        extended_roles.update(get_invoice_roles())

      return user_role in extended_roles

    csrf_token_fn = current_app.jinja_env.globals.get("csrf_token")
    if not csrf_token_fn:
      from flask_wtf.csrf import generate_csrf

      csrf_token_fn = generate_csrf

    return {
      # app_title=app.config.get("APP_TITLE", "Invoice Manager"), # Allow main app title to override or stay
      "status_label": status_label,
      "currency": lambda a, c: currency_format(Decimal(a), c),
      "currency_minor": lambda a, c: currency_format_minor(int(a or 0), c),
      "from_minor": from_minor,
      "format_notes": format_notes,
      "t": t,
      "t_lang": t_lang,
      "get_current_user": get_current_user,
      "is_admin": is_admin,
      "has_role": has_role,
      "get_locale": get_locale,
      "url_for": _url_for_compat,
      "csrf_token": csrf_token_fn,
    }

  if hasattr(app, "app_context_processor"):
    app.app_context_processor(inject)
  else:
    app.context_processor(inject)
