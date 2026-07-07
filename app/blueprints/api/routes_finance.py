from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from flask import current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.blueprints.api import bp
from app.blueprints.api.routes import (
    _active_filter,
    _clamp_money,
    _cleanup_created_invoice,
    _close_conn_safely,
    _invoice_open_url,
    _ledger_status_match,
    _money_to_float,
    _normalize_date_text,
    _payable_status_label,
    _quantize_money,
    _recalc_expense_totals,
    _require_matter_access,
    _reserve_next_installment_no,
    _rollback_conn_safely,
    _safe_float,
    _safe_int,
    _to_decimal,
)
from app.extensions import db
from app.models.client import Client
from app.models.operation import Operation
from app.models.ip_records import CaseExpenseInvoiceMap, Matter, LegacyExpense, LegacyExpensePayment
from app.services.audit.entity_audit import (
    diff_snapshots,
    record_entity_change_audit,
    snapshot_attrs,
)
from app.services.billing.case_finance_service import CaseFinanceService
from app.services.billing.case_invoice_service import fetch_case_invoices
from app.services.billing.invoice_bridge import InvoiceBridgeError, ensure_invoice_client_link
from app.services.billing.invoice_matter_link_usecase import InvoiceMatterLinkUseCase
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter, check_permission, matter_action

_EXPENSE_AUDIT_FIELDS = (
    "expense_id",
    "matter_id",
    "expense_ref",
    "dn_no",
    "dn_date",
    "remit_no",
    "expense_date",
    "due_date",
    "vendor_name",
    "category_code",
    "currency",
    "requested_total",
    "remit_total",
    "outstanding_amount",
    "status",
    "description",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
)
_EXPENSE_PAYMENT_AUDIT_FIELDS = (
    "exp_payment_id",
    "expense_id",
    "installment_no",
    "sent_date",
    "sent_amount",
    "fx_rate",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
)
_EXPENSE_LINK_AUDIT_FIELDS = (
    "id",
    "matter_id",
    "expense_id",
    "billing_invoice_id",
    "billing_line_item_id",
    "amount_minor",
    "currency",
    "created_by",
    "created_at",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
)


def _expense_snapshot(exp: LegacyExpense) -> dict[str, object]:
    return snapshot_attrs(exp, _EXPENSE_AUDIT_FIELDS)


def _expense_payment_snapshot(payment: LegacyExpensePayment) -> dict[str, object]:
    return snapshot_attrs(payment, _EXPENSE_PAYMENT_AUDIT_FIELDS)


def _expense_link_snapshot(link: CaseExpenseInvoiceMap) -> dict[str, object]:
    return snapshot_attrs(link, _EXPENSE_LINK_AUDIT_FIELDS)


def _expense_meta(exp: LegacyExpense, *, source: str) -> dict[str, object]:
    return {
        "expense_id": str(exp.expense_id),
        "matter_id": str(exp.matter_id),
        "source": source,
    }


@bp.route("/cases/<string:matter_id>/invoices")
@matter_action("invoice")
@login_required
def case_invoices(matter_id: str):
    Matter.query.get_or_404(matter_id)
    payload = fetch_case_invoices(str(matter_id))
    return jsonify(payload)


@bp.route("/cases/<string:matter_id>/invoices/create", methods=["POST"])
@matter_action("invoice")
@login_required
def case_invoice_create(matter_id: str):
    if not check_permission("manage_invoice"):
        return jsonify({"error": "forbidden"}), 403
    matter = Matter.query.get_or_404(matter_id)
    payload = request.get_json(silent=True) or {}

    client_id = _safe_int(payload.get("client_id"))
    title = (payload.get("title") or "").strip()
    currency = (payload.get("currency") or "USD").strip().upper()

    if not client_id or not title:
        return jsonify({"error": "client_id and title are required"}), 400

    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "invalid client_id"}), 400

    idem_key = (request.headers.get("Idempotency-Key") or payload.get("request_id") or "").strip()
    actor_id = getattr(current_user, "id", None)
    from app.services.ops.operation_log import namespace_idempotency_key

    op_request_id = namespace_idempotency_key(idem_key or None, actor_id)
    legacy_request_id = idem_key or None
    if op_request_id:
        existing = (
            db.session.query(Operation)
            .filter(Operation.request_id == op_request_id)
            .filter(Operation.action == "case_invoice_create")
            .first()
        )
        if (
            not existing
            and legacy_request_id
            and actor_id is not None
            and legacy_request_id != op_request_id
        ):
            # Backward-compat: previously stored raw idempotency keys in Operation.request_id.
            # Only return a legacy op if it belongs to the same actor to avoid cross-user leaks.
            existing = (
                db.session.query(Operation)
                .filter(Operation.request_id == legacy_request_id)
                .filter(Operation.action == "case_invoice_create")
                .filter(Operation.actor_id == actor_id)
                .first()
            )
        if existing and isinstance(existing.summary_json, dict):
            inv_id = existing.summary_json.get("invoice_id")
            if inv_id:
                return jsonify(
                    {
                        "invoice_id": inv_id,
                        "open_url": _invoice_open_url(inv_id),
                        "matter_id": str(matter.matter_id),
                    }
                )

    try:
        invoice_client_id = ensure_invoice_client_link(client)
    except InvoiceBridgeError as exc:
        return jsonify({"error": str(exc)}), 400

    from app.blueprints.billing_invoices.db import (
        _execute_insert_returning_id,
        get_business_profile,
        get_db,
        next_invoice_number,
        snapshot_of_profile,
    )

    conn = get_db()
    try:
        bp = get_business_profile()
        business_profile_id = int(bp.get("id") or 1)
        issue_date = date.today().isoformat()
        due_date = (date.today() + timedelta(days=30)).isoformat()

        today_str = datetime.now(
            ZoneInfo(current_app.config.get("TIMEZONE", "America/New_York"))
        ).strftime("%Y%m%d")
        prefix = f"INV-{today_str}-"
        number = next_invoice_number(conn, business_profile_id, prefix)

        inv_id = _execute_insert_returning_id(
            conn,
            """
            INSERT INTO invoices
            (client_id, business_profile_id, number, internal_reference, issue_date, due_date,
             status, billing_status, payment_status, notes, subtotal, tax, total,
             subtotal_minor, tax_minor, total_minor, currency, vat_rate, business_snapshot, language,
             ipm_case_id, ipm_case_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(invoice_client_id),
                business_profile_id,
                number,
                title,
                issue_date,
                due_date,
                "draft",
                "draft",
                "unpaid",
                title,
                0.0,
                0.0,
                0.0,
                0,
                0,
                0,
                currency,
                float(bp.get("vat_rate") or 0),
                snapshot_of_profile(bp),
                "en",
                str(matter.matter_id),
                str(matter.our_ref or ""),
            ),
        )
        if not inv_id:
            _rollback_conn_safely(conn, context="api.routes.case_invoice_create")
            return jsonify({"error": "invoice_create_failed"}), 500

        conn.execute(
            """
            INSERT INTO line_items
            (invoice_id, description, qty, unit_price, item_type, discount, is_taxable, qty_minor, unit_price_minor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(inv_id), title, 1, 0.0, "service", 0.0, 1, 0, 0),
        )
        conn.commit()
    except Exception as exc:
        _rollback_conn_safely(conn, context="api.routes.case_invoice_create")
        current_app.logger.exception("Invoice create failed: %s", exc)
        return jsonify({"error": "invoice_create_failed"}), 500
    finally:
        _close_conn_safely(conn, context="api.routes.case_invoice_create")

    try:
        InvoiceMatterLinkUseCase.link(
            matter_id=str(matter.matter_id),
            external_invoice_ref=int(inv_id),
            actor_id=actor_id,
        )
    except InvoiceBridgeError as exc:
        _cleanup_created_invoice(get_db_fn=get_db, invoice_id=int(inv_id))
        return jsonify({"error": str(exc), "invoice_id": inv_id}), 400
    except Exception as exc:
        current_app.logger.exception("Invoice link failed: %s", exc)
        _cleanup_created_invoice(get_db_fn=get_db, invoice_id=int(inv_id))
        return jsonify({"error": "invoice_link_failed", "invoice_id": inv_id}), 500

    if op_request_id:
        try:
            op = Operation(
                request_id=op_request_id,
                actor_id=actor_id,
                action="case_invoice_create",
                risk_level="LOW",
                status="applied",
                summary_json={"invoice_id": inv_id},
                created_at=datetime.utcnow(),
                applied_at=datetime.utcnow(),
            )
            db.session.add(op)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            report_swallowed_exception(
                exc,
                context="api.routes.case_invoice_create.idempotency",
                log_key="api.routes.case_invoice_create.idempotency",
                log_window_seconds=300,
            )

    return (
        jsonify(
            {
                "invoice_id": inv_id,
                "open_url": _invoice_open_url(inv_id),
                "matter_id": str(matter.matter_id),
            }
        ),
        201,
    )


@bp.route("/cases/<string:matter_id>/invoices/link", methods=["POST"])
@matter_action("invoice")
@login_required
def case_invoice_link(matter_id: str):
    matter_id = str(matter_id)
    if not check_permission("manage_invoice"):
        return jsonify({"error": "forbidden"}), 403
    _require_matter_access(matter_id)
    payload = request.get_json(silent=True) or {}
    invoice_id = payload.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id required"}), 400
    try:
        data = InvoiceMatterLinkUseCase.link(
            matter_id=matter_id,
            external_invoice_ref=invoice_id,
            actor_id=getattr(current_user, "id", None),
        )
        return jsonify({"ok": True, "invoice": data})
    except InvoiceBridgeError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"link failed: {exc}"}), 500


@bp.route("/cases/<string:matter_id>/invoices/<int:invoice_id>", methods=["DELETE"])
@matter_action("invoice")
@login_required
def case_invoice_unlink(matter_id: str, invoice_id: int):
    matter_id = str(matter_id)
    if not check_permission("manage_invoice"):
        return jsonify({"error": "forbidden"}), 403
    _require_matter_access(matter_id)
    try:
        InvoiceMatterLinkUseCase.unlink(
            matter_id=matter_id,
            external_invoice_ref=invoice_id,
            actor_id=getattr(current_user, "id", None),
        )
        return jsonify({"ok": True})
    except InvoiceBridgeError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"unlink failed: {exc}"}), 500


@bp.route("/cases/<string:matter_id>/finance/summary")
@matter_action("invoice")
@login_required
def case_finance_summary(matter_id: str):
    Matter.query.get_or_404(matter_id)
    _require_matter_access(matter_id)
    include_ledger = (request.args.get("include_ledger") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    status_filter = request.args.get("status")
    filters = {
        "type": request.args.get("type"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "q": request.args.get("q"),
        "status": status_filter,
    }
    payload = CaseFinanceService.get_summary(
        matter_id,
        filters=filters,
        include_ledger=include_ledger,
    )
    for exp in payload.get("payables", []) or []:
        exp["status"] = _payable_status_label(exp.get("status"))
    ledger_items = payload.get("ledger", []) or []
    if status_filter:
        ledger_items = [item for item in ledger_items if _ledger_status_match(item, status_filter)]
    for item in ledger_items:
        if item.get("type") == "PAYABLE":
            item["status"] = _payable_status_label(item.get("status"))
    payload["ledger"] = ledger_items
    return jsonify(payload)


@bp.route("/cases/<string:matter_id>/finance/ledger")
@matter_action("invoice")
@login_required
def case_finance_ledger(matter_id: str):
    Matter.query.get_or_404(matter_id)
    _require_matter_access(matter_id)
    status_filter = request.args.get("status")
    filters = {
        "type": request.args.get("type"),
        "from": request.args.get("from"),
        "to": request.args.get("to"),
        "q": request.args.get("q"),
        "status": status_filter,
    }
    ledger = CaseFinanceService.list_ledger(matter_id, filters=filters)
    if status_filter:
        ledger = [item for item in ledger if _ledger_status_match(item, status_filter)]
    for item in ledger or []:
        if item.get("type") == "PAYABLE":
            item["status"] = _payable_status_label(item.get("status"))
    return jsonify({"ok": True, "ledger": ledger})


@bp.route("/cases/<string:matter_id>/payables", methods=["POST"])
@matter_action("invoice")
@login_required
def case_payable_create(matter_id: str):
    current_app.logger.info(f"case_payable_create called for {matter_id}")
    if not can_access_matter(current_user, matter_id, action="invoice"):
        current_app.logger.warning(f"Access denied for {matter_id}")
        return jsonify({"error": "forbidden"}), 403

    # Check if matter exists
    m = Matter.query.get(matter_id)
    if not m:
        current_app.logger.warning(f"Matter not found: {matter_id}")
        return jsonify({"error": "matter_not_found"}), 404

    payload = request.get_json(silent=True) or {}
    current_app.logger.info(f"Payload: {payload}")

    exp_id = uuid.uuid4().hex
    existing = LegacyExpense.query.get(exp_id)
    if existing:
        if str(existing.matter_id) != str(matter_id):
            return jsonify({"error": "expense_id_conflict"}), 409
        return jsonify({"ok": True, "expense_id": existing.expense_id, "existing": True}), 200
    requested_total_dec = _to_decimal(payload.get("requested_total"))
    current_app.logger.info(f"Requested total: {requested_total_dec}")

    if requested_total_dec <= 0:
        current_app.logger.warning("Requested total is <= 0")
        return jsonify({"error": "requested_total_required"}), 400

    exp = LegacyExpense(matter_id=str(matter_id))
    exp.expense_id = exp_id
    exp.expense_ref = (payload.get("expense_ref") or "").strip() or None
    exp.dn_no = (payload.get("dn_no") or "").strip() or None
    date_errors: list[str] = []
    exp.dn_date = _normalize_date_text(payload.get("dn_date"), "dn_date", date_errors)
    exp.expense_date = _normalize_date_text(
        payload.get("expense_date"), "expense_date", date_errors
    )
    exp.due_date = _normalize_date_text(payload.get("due_date"), "due_date", date_errors)
    exp.remit_no = (payload.get("remit_no") or "").strip() or None
    exp.vendor_name = (payload.get("vendor_name") or "").strip() or None
    exp.category_code = (payload.get("category_code") or "").strip() or None
    exp.currency = (payload.get("currency") or "USD").strip().upper()
    exp.description = (payload.get("description") or "").strip() or None
    if date_errors:
        return jsonify({"error": "invalid_date_format", "fields": date_errors}), 400

    exp.requested_total = _money_to_float(requested_total_dec)
    exp.remit_total = 0.0
    outstanding = _clamp_money(_quantize_money(requested_total_dec))
    if outstanding < 0:
        outstanding = Decimal("0")
    exp.outstanding_amount = _money_to_float(outstanding)
    exp.status = "unpaid" if outstanding > 0 else "paid"

    try:
        db.session.add(exp)
        db.session.flush()
        record_entity_change_audit(
            action="expense.create",
            target_type="expense",
            actor_id=getattr(current_user, "id", None),
            after=_expense_snapshot(exp),
            meta=_expense_meta(exp, source="api.case_payable_create"),
            title=exp.expense_ref or exp.dn_no or exp.vendor_name or exp.expense_id,
            include_snapshots=True,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = LegacyExpense.query.get(exp_id)
        if existing and str(existing.matter_id) == str(matter_id):
            return jsonify({"ok": True, "expense_id": existing.expense_id, "existing": True}), 200
        return jsonify({"error": "expense_id_conflict"}), 409
    return jsonify({"ok": True, "expense_id": exp.expense_id}), 201


@bp.route("/payables/<string:expense_id>", methods=["PATCH"])
@matter_action("invoice")
@login_required
def payable_patch(expense_id: str):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    date_errors: list[str] = []
    before = _expense_snapshot(exp)

    if "expense_ref" in payload:
        exp.expense_ref = (payload.get("expense_ref") or "").strip() or None
    if "dn_no" in payload:
        exp.dn_no = (payload.get("dn_no") or "").strip() or None
    if "dn_date" in payload:
        exp.dn_date = _normalize_date_text(payload.get("dn_date"), "dn_date", date_errors)
    if "expense_date" in payload:
        exp.expense_date = _normalize_date_text(
            payload.get("expense_date"), "expense_date", date_errors
        )
    if "due_date" in payload:
        exp.due_date = _normalize_date_text(payload.get("due_date"), "due_date", date_errors)
    if "remit_no" in payload:
        exp.remit_no = (payload.get("remit_no") or "").strip() or None
    if "vendor_name" in payload:
        exp.vendor_name = (payload.get("vendor_name") or "").strip() or None
    if "category_code" in payload:
        exp.category_code = (payload.get("category_code") or "").strip() or None
    if "currency" in payload:
        exp.currency = (payload.get("currency") or "USD").strip().upper()
    if "description" in payload:
        exp.description = (payload.get("description") or "").strip() or None
    if "requested_total" in payload:
        exp.requested_total = _money_to_float(_to_decimal(payload.get("requested_total")))

    if date_errors:
        return jsonify({"error": "invalid_date_format", "fields": date_errors}), 400

    _recalc_expense_totals(exp)
    after = _expense_snapshot(exp)
    changes = diff_snapshots(before, after)
    if changes:
        record_entity_change_audit(
            action="expense.update",
            target_type="expense",
            actor_id=getattr(current_user, "id", None),
            changes=changes,
            meta=_expense_meta(exp, source="api.payable_patch"),
            title=exp.expense_ref or exp.dn_no or exp.vendor_name or exp.expense_id,
        )
    db.session.commit()
    return jsonify({"ok": True, "expense_id": exp.expense_id})


@bp.route("/payables/<string:expense_id>", methods=["DELETE"])
@matter_action("invoice")
@login_required
def payable_delete(expense_id: str):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    before = _expense_snapshot(exp)

    exp.is_deleted = True
    exp.deleted_at = datetime.utcnow()
    exp.deleted_by = getattr(current_user, "id", None)
    exp.delete_reason = (
        payload.get("delete_reason") or payload.get("reason") or ""
    ).strip() or None
    record_entity_change_audit(
        action="expense.delete",
        target_type="expense",
        actor_id=getattr(current_user, "id", None),
        before=before,
        after=_expense_snapshot(exp),
        meta=_expense_meta(exp, source="api.payable_delete"),
        title=exp.expense_ref or exp.dn_no or exp.vendor_name or exp.expense_id,
        include_snapshots=True,
    )
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/payables/<string:expense_id>/payments", methods=["POST"])
@matter_action("invoice")
@login_required
def payable_payment_create(expense_id: str):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    expense_before = _expense_snapshot(exp)

    sent_amount_dec = _to_decimal(payload.get("sent_amount"))
    if sent_amount_dec <= 0:
        return jsonify({"error": "invalid sent_amount"}), 400

    payment_id = (payload.get("exp_payment_id") or "").strip() or uuid.uuid4().hex
    installment_no = _reserve_next_installment_no(exp.expense_id)

    date_errors: list[str] = []
    sent_date = _normalize_date_text(payload.get("sent_date"), "sent_date", date_errors)
    if date_errors:
        return jsonify({"error": "invalid_date_format", "fields": date_errors}), 400

    pay = LegacyExpensePayment(
        exp_payment_id=payment_id,
        expense_id=exp.expense_id,
        installment_no=int(installment_no),
        sent_date=sent_date,
        sent_amount=_money_to_float(sent_amount_dec),
        fx_rate=_safe_float(payload.get("fx_rate"), 0.0),
    )
    db.session.add(pay)
    _recalc_expense_totals(exp)
    db.session.flush()
    record_entity_change_audit(
        action="expense.payment.create",
        target_type="expense_payment",
        actor_id=getattr(current_user, "id", None),
        after=_expense_payment_snapshot(pay),
        meta={
            **_expense_meta(exp, source="api.payable_payment_create"),
            "exp_payment_id": pay.exp_payment_id,
        },
        title=f"{exp.expense_ref or exp.expense_id} payment",
        include_snapshots=True,
    )
    expense_changes = diff_snapshots(expense_before, _expense_snapshot(exp))
    if expense_changes:
        record_entity_change_audit(
            action="expense.update",
            target_type="expense",
            actor_id=getattr(current_user, "id", None),
            changes=expense_changes,
            meta={
                **_expense_meta(exp, source="api.payable_payment_create.recalc"),
                "exp_payment_id": pay.exp_payment_id,
            },
            title=exp.expense_ref or exp.dn_no or exp.vendor_name or exp.expense_id,
        )
    db.session.commit()
    return jsonify({"ok": True, "payment_id": pay.exp_payment_id}), 201


@bp.route("/payables/<string:expense_id>/payments/<string:exp_payment_id>", methods=["DELETE"])
@matter_action("invoice")
@login_required
def payable_payment_delete(expense_id: str, exp_payment_id: str):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403

    pay = LegacyExpensePayment.query.get_or_404(exp_payment_id)
    if pay.expense_id != exp.expense_id:
        return jsonify({"error": "payment_mismatch"}), 404

    payload = request.get_json(silent=True) or {}
    expense_before = _expense_snapshot(exp)
    payment_before = _expense_payment_snapshot(pay)
    pay.is_deleted = True
    pay.deleted_at = datetime.utcnow()
    pay.deleted_by = getattr(current_user, "id", None)
    pay.delete_reason = (
        payload.get("delete_reason") or payload.get("reason") or ""
    ).strip() or None

    _recalc_expense_totals(exp)
    record_entity_change_audit(
        action="expense.payment.delete",
        target_type="expense_payment",
        actor_id=getattr(current_user, "id", None),
        before=payment_before,
        after=_expense_payment_snapshot(pay),
        meta={
            **_expense_meta(exp, source="api.payable_payment_delete"),
            "exp_payment_id": pay.exp_payment_id,
        },
        title=f"{exp.expense_ref or exp.expense_id} payment deleted",
        include_snapshots=True,
    )
    expense_changes = diff_snapshots(expense_before, _expense_snapshot(exp))
    if expense_changes:
        record_entity_change_audit(
            action="expense.update",
            target_type="expense",
            actor_id=getattr(current_user, "id", None),
            changes=expense_changes,
            meta={
                **_expense_meta(exp, source="api.payable_payment_delete.recalc"),
                "exp_payment_id": pay.exp_payment_id,
            },
            title=exp.expense_ref or exp.dn_no or exp.vendor_name or exp.expense_id,
        )
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/payables/<string:expense_id>/links/invoice", methods=["POST"])
@matter_action("invoice")
@login_required
def payable_link_invoice(expense_id: str):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}

    billing_invoice_id = _safe_int(payload.get("billing_invoice_id"))
    if not billing_invoice_id:
        return jsonify({"error": "billing_invoice_id is required"}), 400
    billing_line_item_id = _safe_int(payload.get("billing_line_item_id"))
    amount_minor = _safe_int(payload.get("amount_minor"))
    currency = (payload.get("currency") or "").strip().upper() or None

    existing = CaseExpenseInvoiceMap.query.filter(
        CaseExpenseInvoiceMap.expense_id == exp.expense_id,
        CaseExpenseInvoiceMap.billing_invoice_id == billing_invoice_id,
        CaseExpenseInvoiceMap.billing_line_item_id == billing_line_item_id,
    ).first()
    if existing:
        before = _expense_link_snapshot(existing)
        if existing.is_deleted:
            existing.is_deleted = False
            existing.deleted_at = None
            existing.deleted_by = None
            existing.delete_reason = None
        if amount_minor is not None:
            existing.amount_minor = amount_minor
        if currency:
            existing.currency = currency
        changes = diff_snapshots(before, _expense_link_snapshot(existing))
        if changes:
            record_entity_change_audit(
                action=(
                    "expense.invoice_link.create"
                    if before.get("is_deleted")
                    else "expense.invoice_link.update"
                ),
                target_type="expense_invoice_link",
                target_id=existing.id,
                actor_id=getattr(current_user, "id", None),
                changes=changes,
                meta={
                    **_expense_meta(exp, source="api.payable_link_invoice"),
                    "link_id": existing.id,
                    "billing_invoice_id": billing_invoice_id,
                },
                title=f"{exp.expense_ref or exp.expense_id} invoice link",
            )
        db.session.commit()
        return jsonify({"ok": True, "link_id": existing.id, "existing": True})

    link = CaseExpenseInvoiceMap(
        matter_id=str(exp.matter_id),
        expense_id=exp.expense_id,
        billing_invoice_id=int(billing_invoice_id),
        billing_line_item_id=billing_line_item_id,
        amount_minor=amount_minor if amount_minor is not None else None,
        currency=currency,
        created_by=getattr(current_user, "id", None),
    )
    db.session.add(link)
    db.session.flush()
    record_entity_change_audit(
        action="expense.invoice_link.create",
        target_type="expense_invoice_link",
        target_id=link.id,
        actor_id=getattr(current_user, "id", None),
        after=_expense_link_snapshot(link),
        meta={
            **_expense_meta(exp, source="api.payable_link_invoice"),
            "link_id": link.id,
            "billing_invoice_id": billing_invoice_id,
        },
        title=f"{exp.expense_ref or exp.expense_id} invoice link",
        include_snapshots=True,
    )
    db.session.commit()
    return jsonify({"ok": True, "link_id": link.id}), 201


@bp.route(
    "/payables/<string:expense_id>/links/invoice/<int:billing_invoice_id>",
    methods=["DELETE"],
)
@matter_action("invoice")
@login_required
def payable_unlink_invoice(expense_id: str, billing_invoice_id: int):
    exp = LegacyExpense.query.get_or_404(expense_id)
    if not can_access_matter(current_user, exp.matter_id, action="invoice"):
        return jsonify({"error": "forbidden"}), 403

    line_item_id = _safe_int(request.args.get("line_item_id"))
    payload = request.get_json(silent=True) or {}
    delete_reason = (payload.get("delete_reason") or payload.get("reason") or "").strip() or None

    query = CaseExpenseInvoiceMap.query.filter(
        CaseExpenseInvoiceMap.expense_id == exp.expense_id,
        CaseExpenseInvoiceMap.billing_invoice_id == billing_invoice_id,
    )
    if line_item_id:
        query = query.filter(CaseExpenseInvoiceMap.billing_line_item_id == line_item_id)
    rows = query.filter(_active_filter(CaseExpenseInvoiceMap)).all()
    if not rows:
        return jsonify({"error": "link_not_found"}), 404

    now = datetime.utcnow().isoformat()
    before_by_id = {row.id: _expense_link_snapshot(row) for row in rows}
    for row in rows:
        row.is_deleted = True
        row.deleted_at = now
        row.deleted_by = getattr(current_user, "id", None)
        row.delete_reason = delete_reason
        record_entity_change_audit(
            action="expense.invoice_link.delete",
            target_type="expense_invoice_link",
            target_id=row.id,
            actor_id=getattr(current_user, "id", None),
            before=before_by_id.get(row.id),
            after=_expense_link_snapshot(row),
            meta={
                **_expense_meta(exp, source="api.payable_unlink_invoice"),
                "link_id": row.id,
                "billing_invoice_id": billing_invoice_id,
            },
            title=f"{exp.expense_ref or exp.expense_id} invoice link removed",
            include_snapshots=True,
        )

    db.session.commit()
    return jsonify({"ok": True, "deleted": len(rows)})
