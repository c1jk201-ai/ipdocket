from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import (
    AnnuityItem,
    Matter,
    LegacyExpense,
    LegacyExpensePayment,
    LegacyInvoice,
    LegacyInvoicePayment,
)
from app.services.annuity.annuity_management import is_annuity_management_disabled_for_matter
from app.services.annuity.annuity_service import (
    revive_soft_deleted_annuity_item,
    soft_delete_annuity_item,
)
from app.services.workflow.sync_requests import (
    enqueue_annuity_sync_for_item,
    enqueue_annuity_sync_for_matter,
)
from app.utils.permissions import matter_action, require_matter_access

# Import unified invoice services
try:
    from app.models.invoice import get_unified_invoice
    from app.services.billing.invoice_services import (
        InvoiceLinkService,
        InvoiceService,
        PaymentService,
    )

    _UNIFIED_SERVICES = True
except ImportError:
    _UNIFIED_SERVICES = False

_MONEY_QUANT = Decimal("0.01")


def _require_invoice_access(case_id: str) -> None:
    require_matter_access(str(case_id), action="invoice")


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        cleaned = str(value).replace(",", "").strip()
        return Decimal(cleaned or "0")
    except Exception:
        return Decimal("0")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _clamp_money(value: Decimal) -> Decimal:
    if abs(value) < Decimal("0.005"):
        return Decimal("0")
    return value


def _money_to_float(value: Decimal) -> float:
    return float(_clamp_money(_quantize_money(value)))


def _parse_date_input(raw: str | None, label: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    flash(f"{label}   . (YYYY-MM-DD)", "warning")
    return None


def _active_filter(model):
    return or_(model.is_deleted == False, model.is_deleted.is_(None))  # noqa: E712


@bp.route("/<case_id>/cost/invoice/add", methods=["POST"])
@matter_action("invoice")
@login_required
def invoice_add(case_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    inv = LegacyInvoice(matter_id=str(case_id))
    inv.bill_date = _parse_date_input(request.form.get("bill_date"), "Billing")
    inv.due_date = _parse_date_input(request.form.get("due_date"), "Payment deadline")
    inv.tax_issued_date = _parse_date_input(
        request.form.get("tax_issued_date"), "Tax documentation date"
    )
    inv.tax_no = (request.form.get("tax_no") or "").strip() or None
    inv.fee_ref = (request.form.get("fee_ref") or "").strip() or None
    inv.integrated_fee_ref = (request.form.get("integrated_fee_ref") or "").strip() or None
    inv.currency = (request.form.get("currency") or "").strip() or "USD"
    inv.description = (request.form.get("description") or "").strip() or None

    total_amount = _quantize_money(_to_decimal(request.form.get("total_amount")))
    gov_fee = _quantize_money(_to_decimal(request.form.get("gov_fee")))
    service_fee = _quantize_money(_to_decimal(request.form.get("service_fee")))
    vat_amount = _quantize_money(_to_decimal(request.form.get("vat_amount")))
    received_total = _quantize_money(_to_decimal(request.form.get("received_total")))

    inv.total_amount = _money_to_float(total_amount)
    inv.gov_fee = _money_to_float(gov_fee)
    inv.service_fee = _money_to_float(service_fee)
    inv.vat_amount = _money_to_float(vat_amount)
    inv.received_total = _money_to_float(received_total)
    outstanding = _clamp_money(_quantize_money(total_amount - received_total))
    if outstanding < 0:
        outstanding = Decimal("0")
    inv.outstanding_amount = _money_to_float(outstanding)
    inv.status = (request.form.get("status") or "").strip() or (
        "outstanding" if inv.outstanding_amount else "DepositDone"
    )
    inv.status_changed_date = datetime.utcnow().date().isoformat()

    db.session.add(inv)
    db.session.commit()
    flash("Billing Add.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/cost/invoice/<invoice_id>/delete", methods=["POST"])
@matter_action("invoice")
@login_required
def invoice_delete(case_id: str, invoice_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    inv = LegacyInvoice.query.get_or_404(invoice_id)
    if inv.matter_id != str(case_id):
        abort(404)
    if inv.is_deleted:
        flash(" Delete Billing.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))
    inv.is_deleted = True
    inv.deleted_at = datetime.utcnow()
    inv.deleted_by = getattr(current_user, "id", None)
    inv.delete_reason = (request.form.get("delete_reason") or "").strip() or None
    db.session.query(LegacyInvoicePayment).filter(
        LegacyInvoicePayment.invoice_id == inv.invoice_id
    ).update(
        {
            LegacyInvoicePayment.is_deleted: True,
            LegacyInvoicePayment.deleted_at: inv.deleted_at,
            LegacyInvoicePayment.deleted_by: inv.deleted_by,
            LegacyInvoicePayment.delete_reason: inv.delete_reason,
        },
        synchronize_session=False,
    )
    db.session.commit()
    flash("Billing Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/cost/invoice/<invoice_id>/pay", methods=["POST"])
@matter_action("invoice")
@login_required
def invoice_pay(case_id: str, invoice_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    inv = LegacyInvoice.query.get_or_404(invoice_id)
    if inv.matter_id != str(case_id):
        abort(404)

    if inv.is_deleted:
        flash("Delete Billing  Add  none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    amount = _quantize_money(_to_decimal(request.form.get("paid_amount")))
    if amount <= 0:
        flash("  .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    max_no = (
        db.session.query(func.max(LegacyInvoicePayment.installment_no))
        .filter(LegacyInvoicePayment.invoice_id == inv.invoice_id)
        .filter(_active_filter(LegacyInvoicePayment))
        .scalar()
        or 0
    )
    pay = LegacyInvoicePayment(
        invoice_id=inv.invoice_id,
        installment_no=int(max_no) + 1,
        paid_date=_parse_date_input(request.form.get("paid_date"), ""),
        paid_amount=float(amount),
        method=(request.form.get("method") or "").strip() or None,
        payer_name=(request.form.get("payer_name") or "").strip() or None,
        fx_rate=float(_to_decimal(request.form.get("fx_rate"))),
    )
    db.session.add(pay)

    received_total = _quantize_money(_to_decimal(inv.received_total) + amount)
    inv.received_total = _money_to_float(received_total)
    outstanding = _clamp_money(_quantize_money(_to_decimal(inv.total_amount) - received_total))
    if outstanding < 0:
        outstanding = Decimal("0")
    inv.outstanding_amount = _money_to_float(outstanding)
    inv.status = "DepositDone" if outstanding <= 0 else "outstanding"
    inv.status_changed_date = datetime.utcnow().date().isoformat()

    db.session.commit()
    flash("/  .", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/cost/expense/add", methods=["POST"])
@matter_action("invoice")
@login_required
def expense_add(case_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    exp = LegacyExpense(matter_id=str(case_id))
    exp.dn_date = _parse_date_input(request.form.get("dn_date"), "Notice")
    exp.dn_no = (request.form.get("dn_no") or "").strip() or None
    exp.expense_ref = (request.form.get("expense_ref") or "").strip() or None
    exp.remit_no = (request.form.get("remit_no") or "").strip() or None
    exp.currency = (request.form.get("currency") or "").strip() or "USD"
    exp.description = (request.form.get("description") or "").strip() or None

    requested_total = _quantize_money(_to_decimal(request.form.get("requested_total")))
    remit_total = _quantize_money(_to_decimal(request.form.get("remit_total")))
    exp.requested_total = _money_to_float(requested_total)
    exp.remit_total = _money_to_float(remit_total)
    outstanding = _clamp_money(_quantize_money(requested_total - remit_total))
    if outstanding < 0:
        outstanding = Decimal("0")
    exp.outstanding_amount = _money_to_float(outstanding)
    exp.status = (request.form.get("status") or "").strip() or (
        "" if exp.outstanding_amount else "Done"
    )
    db.session.add(exp)
    db.session.commit()
    flash("Expense Add.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/cost/expense/<expense_id>/delete", methods=["POST"])
@matter_action("invoice")
@login_required
def expense_delete(case_id: str, expense_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    exp = LegacyExpense.query.get_or_404(expense_id)
    if exp.matter_id != str(case_id):
        abort(404)
    if exp.is_deleted:
        flash(" Delete Expense.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))
    exp.is_deleted = True
    exp.deleted_at = datetime.utcnow()
    exp.deleted_by = getattr(current_user, "id", None)
    exp.delete_reason = (request.form.get("delete_reason") or "").strip() or None
    db.session.query(LegacyExpensePayment).filter(
        LegacyExpensePayment.expense_id == exp.expense_id
    ).update(
        {
            LegacyExpensePayment.is_deleted: True,
            LegacyExpensePayment.deleted_at: exp.deleted_at,
            LegacyExpensePayment.deleted_by: exp.deleted_by,
            LegacyExpensePayment.delete_reason: exp.delete_reason,
        },
        synchronize_session=False,
    )
    db.session.commit()
    flash("Expense Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/cost/expense/<expense_id>/pay", methods=["POST"])
@matter_action("invoice")
@login_required
def expense_pay(case_id: str, expense_id: str):
    Matter.query.get_or_404(case_id)
    _require_invoice_access(case_id)
    exp = LegacyExpense.query.get_or_404(expense_id)
    if exp.matter_id != str(case_id):
        abort(404)

    if exp.is_deleted:
        flash("Delete Expense  Add  none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    amount = _quantize_money(_to_decimal(request.form.get("sent_amount")))
    if amount <= 0:
        flash("  .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    max_no = (
        db.session.query(func.max(LegacyExpensePayment.installment_no))
        .filter(LegacyExpensePayment.expense_id == exp.expense_id)
        .filter(_active_filter(LegacyExpensePayment))
        .scalar()
        or 0
    )
    pay = LegacyExpensePayment(
        expense_id=exp.expense_id,
        installment_no=int(max_no) + 1,
        sent_date=_parse_date_input(request.form.get("sent_date"), ""),
        sent_amount=float(amount),
        fx_rate=float(_to_decimal(request.form.get("fx_rate"))),
    )
    db.session.add(pay)
    remit_total = _quantize_money(_to_decimal(exp.remit_total) + amount)
    exp.remit_total = _money_to_float(remit_total)
    outstanding = _clamp_money(_quantize_money(_to_decimal(exp.requested_total) - remit_total))
    if outstanding < 0:
        outstanding = Decimal("0")
    exp.outstanding_amount = _money_to_float(outstanding)
    exp.status = "Done" if outstanding <= 0 else ""
    db.session.commit()
    flash("Expense   .", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/annuity/add", methods=["POST"])
@login_required
def annuity_add(case_id: str):
    Matter.query.get_or_404(case_id)
    # Re-check edit permission; the UI can be stale.
    require_matter_access(str(case_id), action="edit_case")
    if is_annuity_management_disabled_for_matter(str(case_id)):
        flash("Annuity management is disabled for this matter because renewal tracking is linked through the client settings.", "info")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    cycle_no_raw = (request.form.get("cycle_no") or "").strip()
    due_date_raw = (request.form.get("due_date") or "").strip()
    extended_due_date_raw = (request.form.get("extended_due_date") or "").strip()
    internal_due_date_raw = (request.form.get("internal_due_date") or "").strip()
    paid_date_raw = (request.form.get("paid_date") or "").strip()
    paid_amount_raw = (request.form.get("paid_amount") or "").strip()
    annuity_status = (request.form.get("annuity_status") or "").strip()
    official_fee_raw = (request.form.get("official_fee") or "").strip()
    discount_rate_raw = (request.form.get("discount_rate") or "").strip()
    memo = (request.form.get("memo") or "").strip()
    overwrite_blanks = (request.form.get("overwrite_blanks") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )

    def _to_int(s: str) -> int | None:
        try:
            return int(s) if s else None
        except Exception:
            return None

    def _parse_float_input(raw: str, label: str) -> tuple[float | None, bool]:
        s = (raw or "").strip()
        if not s:
            return None, True
        try:
            return float(s.replace(",", "")), True
        except Exception:
            flash(f"{label}   . ()", "warning")
            return None, False

    cycle_no = _to_int(cycle_no_raw)
    if not cycle_no:
        flash(" Input .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    # Normalize dates to canonical YYYY-MM-DD. If the user typed an invalid date,
    # abort without mutating the existing record (better than silently storing None).
    due_date = _parse_date_input(due_date_raw, "Due date")
    if due_date_raw and due_date is None:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))
    extended_due_date = _parse_date_input(extended_due_date_raw, "Due date")
    if extended_due_date_raw and extended_due_date is None:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))
    internal_due_date = _parse_date_input(internal_due_date_raw, "TaskDue date")
    if internal_due_date_raw and internal_due_date is None:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))
    paid_date = _parse_date_input(paid_date_raw, "Payment")
    if paid_date_raw and paid_date is None:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    paid_amount, ok_paid_amount = _parse_float_input(paid_amount_raw, "Payment")
    if not ok_paid_amount:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))
    official_fee, ok_official_fee = _parse_float_input(official_fee_raw, "Registration")
    if not ok_official_fee:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))
    discount_rate, ok_discount_rate = _parse_float_input(discount_rate_raw, "(%)")
    if not ok_discount_rate:
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    existing = None
    existing = AnnuityItem.query.filter_by(
        matter_id=str(case_id),
        cycle_no=cycle_no,
    ).first()

    item = existing or AnnuityItem(matter_id=str(case_id))
    is_existing = existing is not None
    if existing is not None:
        revive_soft_deleted_annuity_item(existing)

    item.cycle_no = cycle_no
    if due_date or overwrite_blanks or not is_existing:
        item.due_date = due_date or None
    if extended_due_date or overwrite_blanks or not is_existing:
        item.extended_due_date = extended_due_date or None
    if internal_due_date or overwrite_blanks or not is_existing:
        item.internal_due_date = internal_due_date or None
    if paid_date or overwrite_blanks or not is_existing:
        item.paid_date = paid_date or None
    if not annuity_status and paid_date:
        annuity_status = "paid"
    # For new rows, keep a canonical value in DB (avoid NULL status).
    if is_existing:
        if annuity_status:
            item.annuity_status = annuity_status
        elif overwrite_blanks:
            item.annuity_status = "pending"
    else:
        item.annuity_status = annuity_status or "pending"
    if official_fee_raw or overwrite_blanks or not is_existing:
        item.official_fee = official_fee
    if discount_rate_raw or overwrite_blanks or not is_existing:
        item.discount_rate = discount_rate
    if paid_amount_raw or overwrite_blanks or not is_existing:
        item.paid_amount = paid_amount
    if memo or overwrite_blanks or not is_existing:
        item.memo = memo or None

    db.session.add(item)
    enqueue_annuity_sync_for_item(item)  #  COMMIT 
    db.session.commit()
    flash(
        "Renewal item ." if is_existing else "Renewal item Add.",
        "success",
    )
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))


@bp.route("/<case_id>/annuity/delete", methods=["POST"])
@login_required
def annuity_delete(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    ids = request.form.getlist("annuity_ids")
    ids = [i for i in ids if (i or "").strip()]
    if not ids:
        flash("Delete Renewal Item Select .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    items = (
        AnnuityItem.query.filter(AnnuityItem.matter_id == str(case_id))
        .filter(AnnuityItem.annuity_id.in_(ids))
        .filter(or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None)))
        .all()
    )
    if not items:
        flash("Delete Renewal Item   none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    enqueue_annuity_sync_for_matter(str(case_id))  #  COMMIT 
    actor_id = getattr(current_user, "id", None)
    for item in items:
        soft_delete_annuity_item(
            item,
            reason="case_annuity_delete",
            deleted_by=actor_id,
        )
    db.session.commit()
    flash("Renewal item Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))


@bp.route("/<case_id>/annuity/ensure", methods=["POST"])
@login_required
def annuity_ensure(case_id: str):
    """Regenerate/refresh annuity schedule for this matter from the configured schedule."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    if is_annuity_management_disabled_for_matter(str(case_id)):
        flash("Annuity management is disabled for this matter because renewal tracking is linked through the client settings.", "info")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    from app.services.annuity.annuity_service import ensure_annuities_for_matter
    from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

    try:
        changed = ensure_annuities_for_matter(
            str(case_id),
            refresh_registration_date=True,
            commit=False,
        )
        # Ensure immediate consistency in case view + renewal list + calendar pruning.
        sync_annuity_workflows_for_matter(str(case_id))
        db.session.commit()
        if changed:
            flash(f"Renewal rows created or updated: {changed} change(s).", "success")
        else:
            flash("Renewal rows were already up to date.", "info")
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            current_app.logger.warning(
                "annuity_autogen rollback failed for case_id=%s: %s",
                case_id,
                rollback_exc,
                exc_info=True,
            )
        flash(f"Renewal create/update failed: {exc}", "error")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))


@bp.route("/<case_id>/annuity/workflow-sync", methods=["POST"])
@login_required
def annuity_workflow_sync(case_id: str):
    """Force a rebuild of annuity workflows for this matter (applies "next annuity" + N window)."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    if is_annuity_management_disabled_for_matter(str(case_id)):
        flash("Annuity management is disabled for this matter because renewal tracking is linked through the client settings.", "info")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))

    from app.services.workflow.task_sync import sync_annuity_workflows_for_matter

    try:
        sync_annuity_workflows_for_matter(str(case_id))
        db.session.commit()
        flash("Renewal Task  Done", "success")
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            current_app.logger.warning(
                "annuity_workflow_sync rollback failed for case_id=%s: %s",
                case_id,
                rollback_exc,
                exc_info=True,
            )
        flash(f"Renewal Task  Failed: {exc}", "error")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))


@bp.route("/<case_id>/annuity/csv-sync", methods=["POST"])
@login_required
def annuity_csv_sync(case_id: str):
    """Deprecated: annuity CSV sync is disabled."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    flash("Renewal CSV   Current disabled.", "warning")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-annuity"))


# ---------------------------------------------------------------------------
# Unified Invoice View (Integrated billing_invoices data)
# ---------------------------------------------------------------------------


@bp.route("/invoice-unified/<int:invoice_id>")
@login_required
def unified_invoice_view(invoice_id: int):
    """
    Display unified invoice data from the canonical billing_invoices database.
    Shows invoice details, line items, payments, case links, and integrations.
    """
    if not _UNIFIED_SERVICES:
        flash(" Invoice Service   none.", "error")
        return redirect(url_for("case_work.list_cases"))

    data = get_unified_invoice(invoice_id)
    if not data:
        abort(404)

    debug_enabled = (current_app.config.get("ENABLE_INVOICE_DEBUG") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    show_debug = bool(current_app.debug or debug_enabled) and (
        (getattr(current_user, "role", "") or "").strip().lower() == "admin"
    )

    return render_template(
        "case/unified_invoice_view.html",
        data=data,
        invoice=data["invoice"],
        line_items=data["line_items"],
        payments=data["payments"],
        case_links=data["case_links"],
        integrations=data["integrations"],
        totals=data["totals"],
        show_debug=show_debug,
    )


@bp.route("/invoice-unified/<int:invoice_id>/json")
@login_required
def unified_invoice_json(invoice_id: int):
    """
    API endpoint returning unified invoice data as JSON.
    """
    if not _UNIFIED_SERVICES:
        return jsonify({"error": "Service unavailable"}), 503

    data = get_unified_invoice(invoice_id)
    if not data:
        return jsonify({"error": "Invoice not found"}), 404

    return jsonify(data)
