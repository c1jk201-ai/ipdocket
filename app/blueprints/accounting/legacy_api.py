"""Legacy accounting JSON APIs.

These endpoints keep pre-invoice-system callers working. New invoice writes
should use the billing_invoices routes and services instead of the
SQLAlchemy facade models in this module.
"""

from __future__ import annotations

from datetime import date

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.accounting import bp
from app.extensions import db
from app.models.case import Case
from app.models.invoice import Invoice
from app.utils.api_errors import json_error
from app.utils.legacy_compat import legacy_compat_endpoint
from app.utils.permissions import can_access_legacy_case, check_permission

LEGACY_ACCOUNTING_API_REPLACEMENT = "/accounting/invoice-system/invoices"


@bp.route("/api/invoices", methods=["POST"])
@legacy_compat_endpoint(
    compat_id="accounting-invoice-api",
    successor=LEGACY_ACCOUNTING_API_REPLACEMENT,
)
@login_required
def api_invoices():
    if not check_permission("manage_invoice"):
        return json_error("forbidden", "forbidden", status=403)

    data = request.get_json(silent=True) or {}
    cid = data.get("case_id")
    if not cid:
        return json_error("bad_request", "case_id required", status=400)
    case = Case.query.get(cid)
    if not case:
        return json_error("not_found", "case_not_found", status=404)
    if not can_access_legacy_case(current_user, case, action="invoice"):
        return json_error("forbidden", "forbidden", status=403)

    issue_date_raw = data.get("issue_date")
    due_date_raw = data.get("due_date")
    try:
        issue_date_val = (
            date.fromisoformat(str(issue_date_raw).strip()) if issue_date_raw else date.today()
        )
    except Exception:
        return json_error("bad_request", "invalid issue_date", status=400)
    try:
        due_date_val = date.fromisoformat(str(due_date_raw).strip()) if due_date_raw else None
    except Exception:
        return json_error("bad_request", "invalid due_date", status=400)
    try:
        total_raw = data.get("total")
        total_val = float(total_raw) if total_raw is not None else 0.0
    except Exception:
        return json_error("bad_request", "invalid total", status=400)

    inv = Invoice(
        case_id=cid,
        client_id=data.get("client_id"),
        issue_date=issue_date_val,
        due_date=due_date_val,
        status=data.get("status") or "draft",
        currency=(data.get("currency") or "USD"),
        total=total_val,
        tax_no=data.get("tax_no"),
    )
    db.session.add(inv)
    db.session.commit()
    return jsonify({"id": inv.id}), 201


@bp.route("/api/invoices/<int:iid>", methods=["PATCH", "DELETE"])
@legacy_compat_endpoint(
    compat_id="accounting-invoice-api",
    successor=LEGACY_ACCOUNTING_API_REPLACEMENT,
)
@login_required
def api_invoice_detail(iid: int):
    inv = db.session.get(Invoice, iid)
    if not inv:
        return json_error("not_found", "not found", status=404)
    if not check_permission("manage_invoice"):
        return json_error("forbidden", "forbidden", status=403)
    if inv.case_id:
        case = Case.query.get(inv.case_id)
        if case and not can_access_legacy_case(current_user, case, action="invoice"):
            return json_error("forbidden", "forbidden", status=403)

    if request.method == "DELETE":
        db.session.delete(inv)
        db.session.commit()
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    if "status" in data:
        inv.status = data["status"]
    if "tax_no" in data:
        inv.tax_no = data["tax_no"]
    if "total" in data:
        try:
            inv.total = float(data["total"])
        except Exception:
            return json_error("bad_request", "invalid total", status=400)

    db.session.commit()
    return jsonify({"success": True})
