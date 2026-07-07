from flask import Blueprint

bp = Blueprint("mgmt_info", __name__)

from app.blueprints.billing_invoices.context import register_jinja
from app.blueprints.mgmt_info import (  # noqa: E402,F401
    business_profiles,
    routes,
    tax_invoice_profiles,
)

register_jinja(bp)
