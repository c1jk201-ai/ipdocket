import json

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.settings import bp
from app.extensions import db
from app.models.system_config import SystemConfig
from app.services.audit.entity_audit import diff_snapshots, record_entity_change_audit
from app.utils.permissions import (
    can_access_uploads,
    is_admin,
    is_invoice_manager,
    is_manager,
    resolve_role_scope,
)

# Available menu items for favorites selection
ALL_MENUS = [
    {
        "category": "Cases",
        "items": [
            {"id": "case_dom_patent", "name": "US · Patent", "url": "case_work.list_dom_patent"},
            {"id": "case_dom_design", "name": "US · Design", "url": "case_work.list_dom_design"},
            {"id": "case_dom_tm", "name": "US · Trademark", "url": "case_work.list_dom_trademark"},
            {"id": "case_inc_patent", "name": "Inbound US · Patent", "url": "case_work.list_inc_patent"},
            {
                "id": "case_inc_design",
                "name": "Inbound US · Design",
                "url": "case_work.list_inc_design",
            },
            {"id": "case_inc_tm", "name": "Inbound US · Trademark", "url": "case_work.list_inc_trademark"},
            {"id": "case_out_patent", "name": "Foreign · Patent", "url": "case_work.list_out_patent"},
            {"id": "case_out_design", "name": "Foreign · Design", "url": "case_work.list_out_design"},
            {"id": "case_out_tm", "name": "Foreign · Trademark", "url": "case_work.list_out_trademark"},
            {"id": "case_litigation", "name": "Proceedings / Litigation", "url": "case_work.list_litigation"},
            {"id": "case_all", "name": "All matters", "url": "case_work.case_list"},
            {"id": "case_notices", "name": "Office actions", "url": "doc.all_notices"},
            {"id": "case_responses", "name": "Responses", "url": "doc.all_responses"},
            {"id": "case_letters", "name": "Correspondence", "url": "doc.all_letters"},
        ],
    },
    {
        "category": "Statistics",
        "items": [
            {"id": "stats_clients", "name": "Client analytics", "url": "stats.by_clients"},
            {"id": "stats_costs", "name": "Cost analytics", "url": "stats.by_costs"},
            {"id": "stats_tc", "name": "TC analytics", "url": "stats.tc_stats"},
            {"id": "tc_my", "name": "My TC", "url": "workflow.tc_my"},
            {"id": "stats_performance", "name": "Performance", "url": "stats.performance"},
        ],
    },
    {
        "category": "Docketing",
        "items": [
            {"id": "worklog", "name": "Work queue", "url": "worklog.index"},
            {"id": "deadline_month", "name": "Monthly docket", "url": "deadlines.calendar_month"},
            {"id": "deadline_list", "name": "Docket list", "url": "deadlines.list_view"},
        ],
    },
    {
        "category": "Renewals",
        "items": [
            {"id": "renewal_month", "name": "Monthly annuities", "url": "annuities.calendar_month"},
            {"id": "renewal_list", "name": "Annuity list", "url": "annuities.fees"},
            {"id": "renewal_giveup", "name": "Abandoned", "url": "annuities.giveup"},
        ],
    },
    {
        "category": "CRM",
        "items": [
            {"id": "crm_dashboard", "name": "CRM dashboard", "url": "customers.dashboard"},
            {"id": "crm_clients", "name": "Contacts", "url": "customers.clients"},
            {"id": "crm_leads", "name": "Leads", "url": "customers.lead_list"},
            {"id": "crm_opportunities", "name": "Opportunities", "url": "customers.opportunity_list"},
        ],
    },
    {
        "category": "Accounting",
        "items": [
            {
                "id": "acc_dashboard",
                "name": "Billing dashboard",
                "url": "billing_invoices.core.dashboard",
            },
            {
                "id": "acc_invoices",
                "name": "Invoices",
                "url": "billing_invoices.invoices.list_invoices",
            },
            {
                "id": "acc_clients",
                "name": "Billing contacts",
                "url": "billing_invoices.clients.list_clients",
            },
            {
                "id": "acc_templates",
                "name": "Templates",
                "url": "billing_invoices.templates_bp.list_templates",
            },
            {"id": "acc_bank", "name": "Bank accounts", "url": "billing_invoices.bank_activity.page"},
            {
                "id": "acc_matching",
                "name": "Payment matching",
                "url": "billing_invoices.bank_activity.matching_page",
            },
            {
                "id": "acc_case_matching",
                "name": "Matter matching",
                "url": "billing_invoices.case_matching.page",
            },
            {"id": "acc_business", "name": "Business profiles", "url": "mgmt_info.list_business_profiles"},
            {"id": "acc_aging", "name": "Aging", "url": "billing_invoices.aging.aging_report"},
        ],
    },
]

_SENSITIVE_CONFIG_MARKERS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "WEBHOOK",
    "API_KEY",
    "CLIENT_SECRET",
    "DATABASE_URL",
    "SQLALCHEMY_DATABASE_URI",
)


def _settings_sensitive_config_key(key: str) -> bool:
    upper = str(key or "").upper()
    return any(marker in upper for marker in _SENSITIVE_CONFIG_MARKERS)


def _settings_audit_config_value(key: str, value: object, *, incoming: bool = False) -> str:
    if _settings_sensitive_config_key(key):
        if incoming and str(value or "").strip():
            return "[Changed]"
        return "[Settings]" if str(value or "").strip() else "[]"
    return "" if value is None else str(value)


def _get_menu_by_id(menu_id):
    """Helper to find menu item by ID"""
    for category in ALL_MENUS:
        for item in category["items"]:
            if item["id"] == menu_id:
                return item
    return None


def _get(key: str) -> str:
    cfg = SystemConfig.query.filter_by(key=key).first()
    return cfg.value if cfg else ""


def _set(key: str, value: str):
    cfg = SystemConfig.query.filter_by(key=key).first()
    if not cfg:
        cfg = SystemConfig(key=key, value=value)
        db.session.add(cfg)
    else:
        cfg.value = value


@bp.route("/", methods=["GET"])
@login_required
def index():
    has_password = bool(current_user.password_hash)

    # Parse current favorites
    try:
        current_favorites = json.loads(current_user.menu_favorites or "[]")
    except (json.JSONDecodeError, TypeError):
        current_favorites = []

    normalized_role = (getattr(current_user, "role", "") or "").strip().lower()
    role_scope = resolve_role_scope(normalized_role)
    permission_scope = {
        "role": normalized_role,
        "is_limited_user": normalized_role == "user",
        "is_admin": is_admin(current_user),
        "is_manager": is_manager(current_user),
        "is_invoice_manager": is_invoice_manager(current_user),
        "uploads_access": can_access_uploads(current_user),
        **role_scope,
    }

    return render_template(
        "settings/index.html",
        has_password=has_password,
        available_menus=[] if permission_scope["is_limited_user"] else ALL_MENUS,
        current_favorites=current_favorites,
        permission_scope=permission_scope,
    )


@bp.route("/save", methods=["POST"])
@login_required
def save():
    # Only admin can save system configs
    if current_user.role != "admin":
        flash("You do not have permission to change system settings.", "danger")
        return redirect(url_for("main.index"))

    payload = request.get_json(silent=True) if request.is_json else None
    data = payload if isinstance(payload, dict) else dict(request.form or {})
    data.pop("csrf_token", None)
    if not data:
        flash("No settings were provided.", "warning")
        return redirect(url_for("settings.index"))

    for key, value in data.items():
        if not key:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        key_str = str(key)
        old_value = _get(key_str)
        new_value = "" if value is None else str(value)
        _set(key_str, new_value)
        before = {
            "key": key_str,
            "value": _settings_audit_config_value(key_str, old_value),
        }
        after = {
            "key": key_str,
            "value": _settings_audit_config_value(
                key_str,
                new_value,
                incoming=_settings_sensitive_config_key(key_str),
            ),
        }
        changes = diff_snapshots(before, after)
        if changes:
            record_entity_change_audit(
                action="settings.config.update",
                target_type="system_config",
                actor_id=getattr(current_user, "id", None),
                changes=changes,
                meta={"key": key_str, "source": "settings.save"},
                title=key_str,
            )

    db.session.commit()
    flash("Settings saved.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/me", methods=["GET"])
@login_required
def me():
    return redirect(url_for("settings.index"))


@bp.route("/favorites", methods=["POST"])
@login_required
def favorites():
    """Save user's favorite menu selections"""
    selected_ids = request.form.getlist("favorites")

    # Validate that all selected IDs exist in ALL_MENUS
    valid_ids = []
    for menu_id in selected_ids:
        if _get_menu_by_id(menu_id):
            valid_ids.append(menu_id)

    # Save to user
    current_user.menu_favorites = json.dumps(valid_ids)
    db.session.commit()

    flash("Menu favorites saved.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/change-password", methods=["POST"])
@login_required
def change_password():
    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")
    confirm_password = request.form.get("confirm_password")

    if not current_user.check_password(current_password):
        flash("Current password does not match.", "danger")
        return redirect(url_for("settings.index"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "danger")
        return redirect(url_for("settings.index"))

    if not new_password or len(new_password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(url_for("settings.index"))

    current_user.set_password(new_password)
    db.session.commit()
    flash("Password changed successfully.", "success")
    return redirect(url_for("settings.index"))
