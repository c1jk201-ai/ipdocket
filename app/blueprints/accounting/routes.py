from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.accounting import bp
from app.blueprints.billing_invoices.db import get_all_business_profiles, get_db, row_to_dict
from app.extensions import db
from app.models.ip_records import LegacyInvoice
from app.services.billing.invoice_bridge import (
    InvoiceBridgeError,
    link_invoice_to_case,
    resolve_external_invoice_id,
    unlink_invoice_case,
)
from app.services.billing.tax_issue_types import (
    TAX_ISSUE_TYPE_LABELS_EN,
    enrich_invoice_tax_issue_fields,
)
from app.utils.permissions import check_permission
from app.utils.url_helpers import safe_referrer_path


@bp.route("/invoices")
@login_required
def invoices():
    return redirect("/accounting/invoice-system/invoices")


@bp.route("/payments")
@login_required
def payments():
    return redirect(url_for("billing_invoices.invoices.list_invoices", status="paid"))


@bp.route("/expenses")
@login_required
def expenses():
    return redirect(url_for("billing_invoices.expenses.list_expenses"))


@bp.route("/vat-report")
@login_required
def vat_report():
    return redirect(url_for("business.accounting_vat_report"))


def _safe_int(
    value, default: int, min_value: int | None = None, max_value: int | None = None
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if min_value is not None:
        parsed = max(int(min_value), parsed)
    if max_value is not None:
        parsed = min(int(max_value), parsed)
    return parsed


@bp.route("/invoice-tax-ledger")
@bp.route("/tax-ledger")
@login_required
def invoice_tax_ledger():
    if not check_permission("manage_invoice"):
        abort(403)

    issue_type_filter = (request.args.get("tax_issue_type") or "").strip().lower()
    source_filter = (request.args.get("source") or "").strip().lower()
    bp_id = (request.args.get("business_profile_id") or "").strip()
    q = (request.args.get("q") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    page = _safe_int(request.args.get("page", 1), 1, 1, None)
    per_page = _safe_int(request.args.get("per_page", 50), 50, 10, 200)

    where = [
        "(COALESCE(invoices.billing_status, invoices.status, '') IN ('tax_issued','cash_issued','processed') OR invoices.tax_issued_at IS NOT NULL)"
    ]
    params: list = []
    process_date_expr = "COALESCE(substr(invoices.tax_issued_at, 1, 10), invoices.issue_date)"
    if bp_id:
        where.append("invoices.business_profile_id = ?")
        params.append(bp_id)
    if date_from:
        where.append(f"{process_date_expr} >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{process_date_expr} <= ?")
        params.append(date_to)
    if q:
        like = f"%{q}%"
        where.append(
            "("
            "invoices.number LIKE ? OR invoices.internal_reference LIKE ? OR "
            "invoices.ipm_case_ref LIKE ? OR invoices.ipm_case_id LIKE ? OR clients.name LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like])

    where_sql = " AND ".join(where)
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT
                invoices.*,
                clients.name AS client_name,
                business_profile.name AS business_name
            FROM invoices
            JOIN clients ON clients.id = invoices.client_id
            LEFT JOIN business_profile ON business_profile.id = invoices.business_profile_id
            WHERE {where_sql}
            ORDER BY COALESCE(invoices.tax_issued_at, invoices.issue_date) DESC, invoices.id DESC
            LIMIT 5000
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    all_rows = []
    summary_counts = {key: 0 for key in TAX_ISSUE_TYPE_LABELS_EN}
    for row in rows:
        item = row_to_dict(row)
        enrich_invoice_tax_issue_fields(item)
        resolved_type = item.get("tax_issue_type_resolved") or ""
        source = (item.get("tax_issue_source") or "legacy").strip().lower()
        if issue_type_filter and resolved_type != issue_type_filter:
            continue
        if source_filter and source != source_filter:
            continue
        if resolved_type in summary_counts:
            summary_counts[resolved_type] += 1
        item["tax_issue_source_resolved"] = source
        all_rows.append(item)

    total_count = len(all_rows)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    ledger_rows = all_rows[start : start + per_page]

    return render_template(
        "accounting/invoice_tax_ledger.html",
        rows=ledger_rows,
        summary_counts=summary_counts,
        type_labels=TAX_ISSUE_TYPE_LABELS_EN,
        all_profiles=get_all_business_profiles(),
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
    )


@bp.route("/link-external-invoice", methods=["POST"])
@login_required
def link_external_invoice():
    if not check_permission("manage_invoice"):
        flash("You do not have permission to manage invoices.", "error")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    ipm_inv_id = request.form.get("ipm_invoice_id")
    ext_inv_ref = request.form.get("external_invoice_id")

    if not ipm_inv_id or not ext_inv_ref:
        flash("Internal and external invoice IDs are required.")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    ipm_inv = db.session.get(LegacyInvoice, ipm_inv_id)
    if not ipm_inv:
        flash("Internal invoice not found.")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    try:
        external_id = resolve_external_invoice_id(ext_inv_ref)
        link_invoice_to_case(ipm_inv, int(external_id))
        flash("External invoice linked successfully.")
    except InvoiceBridgeError:
        current_app.logger.exception("External invoice link failed")
        flash("Invoice link failed.", "error")

    return redirect(safe_referrer_path() or url_for("costs.invoices"))


@bp.route("/unlink-external-invoice", methods=["POST"])
@login_required
def unlink_external_invoice():
    if not check_permission("manage_invoice"):
        flash("You do not have permission to manage invoices.", "error")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    ipm_inv_id = request.form.get("ipm_invoice_id")

    if not ipm_inv_id:
        flash("Internal Invoice ID required.")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    ipm_inv = db.session.get(LegacyInvoice, ipm_inv_id)
    if not ipm_inv:
        flash("Internal Invoice   none.")
        return redirect(safe_referrer_path() or url_for("costs.invoices"))

    try:
        unlink_invoice_case(ipm_inv)
        flash("External Invoice Link Clear.")
    except InvoiceBridgeError:
        current_app.logger.exception("External invoice unlink failed")
        flash("Clear Failed")

    return redirect(safe_referrer_path() or url_for("costs.invoices"))
