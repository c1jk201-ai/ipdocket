from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user

from app.services.billing.tax_issue_types import (
  FINAL_TAX_BILLING_STATUSES,
  normalize_tax_issue_type,
)
from app.services.ops.background import BackgroundService
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_invoice_manager
from app.utils.url_helpers import safe_referrer_path

from ..auth import get_current_user, log_audit, role_required
from ..db import (
  _get_column_names,
  build_client_deposit_audit_meta,
  cancel_uncanceled_deposit_applies_for_invoice,
  get_db,
  row_get,
  row_to_dict,
)
from ..services.invoice_bulk_status_service import (
  BulkInvoiceStatusHooks,
  apply_bulk_invoice_status_update,
)
from ..services.invoice_deletion_service import (
  InvoiceDeleteBlockedError,
  InvoiceDeleteExecutionError,
  InvoiceDeleteHooks,
  delete_invoices,
  log_invoice_delete_cancel_audits,
  record_bulk_invoice_delete_operation,
)
from .invoices import (
  _BILLING_TRANSITIONS,
  _PAYMENT_TRANSITIONS,
  _STATUS_TRANSITIONS,
  _compute_billing_payment_from_status,
  _derive_legacy_status_from_split,
  _is_payment_effectively_complete,
  _reject_transition,
  _resolve_billing_status,
  _resolve_payment_status,
  _sync_legacy_status,
  _stored_outgoing_mode,
  _transition_allowed,
  bp,
)


class TaxInvoiceUploadError(Exception):
  """Compatibility error for the disabled tax-document import path."""


@bp.route("/<int:invoice_id>/mark_tax_issued", methods=["POST"])
def mark_tax_issued(invoice_id):
  """Deprecated manual status flip. Redirect to the tax-documentation queue."""
  if not is_invoice_manager(current_user):
    abort(403, "You do not have permission to record tax documentation for invoices.")

  # Preserve language/outgoing params for fallback view redirect.
  lang = (request.form.get("lang") or request.args.get("lang") or "").strip().lower() or None
  if lang not in (None, "en"):
    lang = None
  outgoing = (request.form.get("outgoing") or request.args.get("outgoing") or "").strip() or None

  flash(
    "Use the tax documentation queue to record tax status changes.",
    "warning",
  )
  try:
    return redirect(url_for("billing_invoices.invoices.tax_issue"))
  except Exception:
    return redirect(
      url_for(
        "billing_invoices.invoices.view_invoice",
        invoice_id=invoice_id,
        lang=lang,
        outgoing=outgoing,
      )
    )


@bp.route("/<int:invoice_id>/mark_paid", methods=["POST"])
def mark_paid(invoice_id):
  """Non-USD quick mark paid (legacy). For USD, prefer verify_payment."""
  conn = get_db()
  invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not invoice:
    conn.close()
    abort(404)
  # For USD, redirect to verification flow instead of blindly marking paid
  if (invoice["currency"] or "").upper() == "USD":
    conn.close()
    flash("Use payment verification before marking this invoice paid.", "error")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
  current_payment = _resolve_payment_status(invoice)
  if not _transition_allowed(current_payment, "paid", _PAYMENT_TRANSITIONS, "unpaid"):
    conn.close()
    return _reject_transition(
      "Payment status transition is not allowed.",
      invoice_id=invoice_id,
      status_code=409,
    )
  # Decoupled: only set payment fields for non-USD quick paid
  conn.execute(
    "UPDATE invoices SET payment_verified=1, payment_status='paid' WHERE id=?",
    (invoice_id,),
  )
  _sync_legacy_status(conn, invoice_id)
  conn.commit()
  conn.close()
  from ..auth import log_audit

  log_audit("invoice.mark_paid", "invoice", invoice_id)
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/mark_settlement_done", methods=["POST"])
@role_required("admin", "staff")
def mark_settlement_done(invoice_id):
  conn = get_db()
  row = conn.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  now_str = datetime.now().isoformat()
  conn.execute(
    "UPDATE invoices SET internal_settlement_status=?, internal_settlement_at=? WHERE id=?",
    ("done", now_str, invoice_id),
  )
  conn.commit()
  conn.close()
  log_audit("invoice.internal_settlement.done", "invoice", invoice_id)
  flash("Internal settlement marked complete.", "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/reset_settlement_status", methods=["POST"])
@role_required("admin", "staff")
def reset_settlement_status(invoice_id):
  conn = get_db()
  row = conn.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  conn.execute(
    "UPDATE invoices SET internal_settlement_status=NULL, internal_settlement_at=NULL WHERE id=?",
    (invoice_id,),
  )
  conn.commit()
  conn.close()
  log_audit("invoice.internal_settlement.reset", "invoice", invoice_id)
  flash("Internal Settlement Status Reset.", "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


def _ensure_not_downgrade_tax_issued(conn, invoice_id, new_status):
  # Policy note:
  # Tax-issued downgrade is intentionally allowed in the current split-status model.
  # Guard is kept as an explicit no-op hook to preserve call-site compatibility.
  return None


def _sync_bank_activity_for_invoice_tax_downgrade(conn, invoice_number: str | None):
  if not invoice_number:
    return
  inv_no = str(invoice_number).strip()
  if not inv_no:
    return
  try:
    conn.execute(
      "UPDATE bank_transactions SET tax_invoice_override=NULL, tax_invoice_issued=0, tax_invoice_issued_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE memo LIKE ?",
      (f"%INV:{inv_no}%",),
    )
  except Exception:
    return


def _spawn_bank_activity_tax_downgrade(_app, invoice_numbers: list[str]):
  try:
    nums = [str(n).strip() for n in (invoice_numbers or []) if str(n).strip()]
  except Exception:
    nums = []
  if not nums:
    return

  def _worker(numbers: list[str]):
    try:
      conn = get_db()
      for n in numbers:
        _sync_bank_activity_for_invoice_tax_downgrade(conn, n)
      try:
        conn.commit()
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices_status._spawn_bank_activity_tax_downgrade.commit",
          log_key="billing_invoices.invoices_status._spawn_bank_activity_tax_downgrade.commit",
          log_window_seconds=300,
        )
      try:
        conn.close()
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices_status._spawn_bank_activity_tax_downgrade.close",
          log_key="billing_invoices.invoices_status._spawn_bank_activity_tax_downgrade.close",
          log_window_seconds=300,
        )
    except Exception:
      return

  try:
    BackgroundService.run_async(_worker, nums)
  except Exception:
    return


def _tax_issue_source_from_form(default: str) -> str:
  source = (request.form.get("tax_issue_source") or "").strip().lower()
  if source in {
    "manual_status",
    "bulk_update",
    "manual_detail",
    "tax_issue_page",
    "tax_invoice_import",
    "bank_activity",
  }:
    return source
  return default


@bp.route("/<int:invoice_id>/update_status", methods=["POST"])
def update_invoice_status(invoice_id):
  new_status = request.form.get("status", "draft")
  if new_status not in {
    "draft",
    "sent",
    "paid",
    "tax_issued",
    "void",
    "cash_issued",
    "processed",
    "payment_pending",
    "pre_overdue",
  }:
    abort(400, " Statusvalue.")

  conn = get_db()
  bank_activity_to_sync = []

  # Previous Status times
  _row = conn.execute(
    "SELECT number, status, billing_status, payment_status, currency, payment_verified FROM invoices WHERE id=?",
    (invoice_id,),
  ).fetchone()
  invoice = row_to_dict(_row)
  old_status = invoice.get("status") if invoice else "unknown"
  invoice_number = invoice.get("number")
  payment_complete = _is_payment_effectively_complete(invoice)
  old_status_norm = (old_status or "").strip().lower()
  old_billing_norm = (
    (invoice.get("billing_status") or old_status or "").strip().lower() if invoice else ""
  )

  if not _transition_allowed(old_status_norm, new_status, _STATUS_TRANSITIONS, "draft"):
    conn.close()
    flash(" Invoice status .", "error")
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))

  # NOTE: Backup removed from status update - only needed for delete operations.
  # Backup creation was causing significant performance degradation for simple status changes.

  # USD  paid  → status change 
  if (
    invoice
    and new_status == "paid"
    and (invoice["currency"] or "").upper() == "USD"
    and not payment_complete
  ):
    conn.close()
    flash(
      "Payment verification is available from the invoice details page.",
      "error",
    )
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))

  canceled_deposit_entries = []
  if new_status == "void":
    # Void should not leave applied deposits behind.
    user = None
    try:
      user = get_current_user()
    except Exception:
      user = None
    try:
      if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    except Exception as begin_exc:
      # Best-effort: if BEGIN fails, proceed; insert helper will still enforce invariants.
      report_swallowed_exception(
        begin_exc,
        context="billing_invoices.invoices_status.update_invoice_status.void.begin_immediate",
        log_key="billing_invoices.invoices_status.update_invoice_status.void.begin_immediate",
        log_window_seconds=300,
      )
    try:
      canceled_deposit_entries = cancel_uncanceled_deposit_applies_for_invoice(
        conn,
        int(invoice_id),
        memo="auto_cancel_on_void",
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
    except Exception as exc:
      try:
        conn.rollback()
      except Exception as rollback_exc:
        report_swallowed_exception(
          rollback_exc,
          context="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit.rollback",
          log_key="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit.rollback",
          log_window_seconds=300,
        )
      conn.close()
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit",
        log_key="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit",
        log_window_seconds=300,
      )
      flash(
        "Void Process Retainer applicationCancel failed. Retainer applicationCancel retry.",
        "error",
      )
      return redirect(
        safe_referrer_path()
        or url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id)
      )

  # Status  Apply
  bs, ps = _compute_billing_payment_from_status(
    new_status, (invoice["payment_verified"] if invoice else 0)
  )
  legacy_status = _derive_legacy_status_from_split(bs, ps)
  if new_status == "tax_issued":
    tax_issue_type = normalize_tax_issue_type(request.form.get("tax_issue_type"), "tax_issued")
    if not tax_issue_type:
      tax_issue_type = "tax_invoice"
    tax_issue_source = _tax_issue_source_from_form("manual_status")
    tax_issue_note = (request.form.get("tax_issue_note") or "").strip() or None
    if not payment_complete:
      confirm = request.form.get("confirm_unverified_tax", "").strip().lower()
      if confirm != "yes":
        conn.close()
        flash(
          "Payment has not been verified. Confirm before marking tax documentation recorded.",
          "error",
        )
        return redirect(
          url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id)
          + "?show_tax_warning=1"
        )

    now_kst = _current_tax_issued_at()
    conn.execute(
      """
      UPDATE invoices
      SET status=?, tax_issued_at=?, billing_status=?,
        tax_issue_type=?, tax_issue_source=?, tax_issue_note=?
      WHERE id=?
      """,
      (
        legacy_status,
        now_kst,
        bs,
        tax_issue_type,
        tax_issue_source,
        tax_issue_note,
        invoice_id,
      ),
    )
  elif new_status == "void":
    conn.execute(
      "UPDATE invoices SET status=?, billing_status=?, payment_status=?, payment_verified=0, payment_meta=NULL WHERE id=?",
      (legacy_status, bs, ps, invoice_id),
    )
  else:
    conn.execute(
      "UPDATE invoices SET status=?, billing_status=?, payment_status=? WHERE id=?",
      (legacy_status, bs, ps, invoice_id),
    )
  if old_billing_norm in FINAL_TAX_BILLING_STATUSES and new_status != "tax_issued":
    try:
      conn.execute(
        "UPDATE invoices SET tax_issued_at=NULL, tax_issue_type=NULL, tax_issue_source=NULL, tax_issue_note=NULL WHERE id=?",
        (invoice_id,),
      )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_status.update_invoice_status.clear_tax_issued_at",
        log_key="billing_invoices.invoices_status.update_invoice_status.clear_tax_issued_at",
        log_window_seconds=300,
      )
    if invoice_number:
      bank_activity_to_sync.append(invoice_number)
  conn.commit()
  conn.close()

  # Deposit auto-cancel audit logs (best-effort; status update should not fail due to logging).
  if canceled_deposit_entries:
    for e in canceled_deposit_entries:
      try:
        audit_meta = build_client_deposit_audit_meta(
          entry_id=e.get("cancel_entry_id"),
          business_profile_id=e.get("business_profile_id"),
          client_id=e.get("client_id"),
          currency=e.get("currency"),
          amount_minor=e.get("amount_minor"),
          entry_type="cancel_apply",
          memo=e.get("memo"),
          related_invoice_id=int(invoice_id),
          related_entry_id=e.get("apply_entry_id"),
          balance_before_minor=e.get("balance_before_minor"),
          balance_after_minor=e.get("balance_after_minor"),
        )
        log_audit("invoice.deposit.cancel_apply", "invoice", int(invoice_id), audit_meta)
      except Exception as log_exc:
        report_swallowed_exception(
          log_exc,
          context="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit.audit",
          log_key="billing_invoices.invoices_status.update_invoice_status.void.cancel_deposit.audit",
          log_window_seconds=300,
        )

  try:
    if bank_activity_to_sync:
      _spawn_bank_activity_tax_downgrade(current_app._get_current_object(), bank_activity_to_sync)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_status.update_invoice_status.spawn_bank_activity",
      log_key="billing_invoices.invoices_status.update_invoice_status.spawn_bank_activity",
      log_window_seconds=300,
    )

  #  (Status Change — days Status topfrom )
  from ..auth import log_audit

  audit_meta = {
    "number": invoice_number,
    "old_status": old_status,
    "new_status": new_status,
  }
  if new_status == "tax_issued":
    audit_meta["tax_issue_type"] = (
      normalize_tax_issue_type(request.form.get("tax_issue_type"), "tax_issued")
      or "tax_invoice"
    )
    audit_meta["tax_issue_source"] = _tax_issue_source_from_form("manual_status")
  log_audit(
    "invoice.status_change",
    "invoice",
    invoice_id,
    json.dumps(audit_meta, ensure_ascii=False),
  )

  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))


def _decimal_amount(value, default: Decimal = Decimal("0")) -> Decimal:
  try:
    return Decimal(str(value or 0))
  except (InvalidOperation, ValueError, TypeError):
    return default


def _amounts_match(lhs: Decimal, rhs: Decimal) -> bool:
  tolerance = Decimal("0.01")
  try:
    if lhs == lhs.to_integral_value() and rhs == rhs.to_integral_value():
      tolerance = Decimal("1")
  except Exception:
    tolerance = Decimal("0.01")
  return abs(lhs - rhs) <= tolerance


def _load_taxable_invoice_rows(conn, invoice_numbers: list[str]) -> dict[str, dict]:
  numbers = [str(num).strip() for num in (invoice_numbers or []) if str(num).strip()]
  if not numbers:
    return {}

  placeholders = ",".join(["?"] * len(numbers))
  rows = conn.execute(
    f"""
    SELECT
      invoices.id,
      invoices.number,
      invoices.status,
      invoices.billing_status,
      invoices.payment_status,
      invoices.payment_verified,
      invoices.currency,
      invoices.tax_issued_at,
      (
        SELECT COALESCE(
          SUM(
            line_items.qty * line_items.unit_price
            * (1 - COALESCE(line_items.discount, 0) / 100.0)
          ),
          0
        )
        FROM line_items
        WHERE line_items.invoice_id = invoices.id
         AND line_items.item_type = 'service'
         AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
      ) AS service_total,
      (
        SELECT COALESCE(
          SUM(
            CASE
              WHEN COALESCE(line_items.fx_rate_used, 0) > 0 THEN
                (COALESCE(line_items.fx_fee, 0) + COALESCE(line_items.fx_gov, 0))
                * COALESCE(line_items.fx_rate_used, 0)
                * (1 + COALESCE(line_items.fx_markup, 0) / 100.0)
              ELSE
                line_items.qty * line_items.unit_price
                * (1 - COALESCE(line_items.discount, 0) / 100.0)
            END
          ),
          0
        )
        FROM line_items
        WHERE line_items.invoice_id = invoices.id
         AND line_items.item_type = 'foreign'
         AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
         AND COALESCE(line_items.is_taxable, 0) = 1
      ) AS foreign_taxable_total
    FROM invoices
    WHERE invoices.number IN ({placeholders})
    """,
    numbers,
  ).fetchall()
  return {str(row["number"]): row_to_dict(row) for row in rows}


def _invoice_taxable_supply_amount(row: dict) -> Decimal:
  return _decimal_amount(row.get("service_total")) + _decimal_amount(
    row.get("foreign_taxable_total")
  )


def _current_tax_issued_at() -> str:
  try:
    return datetime.now(ZoneInfo(current_app.config.get("TIMEZONE", "America/New_York"))).isoformat(
      timespec="seconds"
    )
  except Exception:
    return datetime.now().isoformat()


def _apply_tax_issued_status(conn, invoice_row: dict) -> str:
  invoice_id = int(invoice_row["id"])
  current_billing = _resolve_billing_status(invoice_row)
  if current_billing == "tax_issued":
    return "already"
  if not _transition_allowed(current_billing, "tax_issued", _BILLING_TRANSITIONS, "draft"):
    return "blocked"
  now_kst = _current_tax_issued_at()
  conn.execute(
    """
    UPDATE invoices
    SET tax_issued_at=?, billing_status=?,
      tax_issue_type='tax_invoice', tax_issue_source='tax_invoice_import'
    WHERE id=?
    """,
    (now_kst, "tax_issued", invoice_id),
  )
  _sync_legacy_status(
    conn,
    invoice_id,
    billing_status="tax_issued",
    payment_status=_resolve_payment_status(invoice_row),
  )
  invoice_row["billing_status"] = "tax_issued"
  invoice_row["status"] = "tax_issued"
  invoice_row["tax_issued_at"] = now_kst
  return "updated"


@bp.route("/import_tax_invoice_status_file", methods=["POST"])
def import_tax_invoice_status_file():
  flash("Tax documentation status import is not available.", "error")
  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))

  upload = request.files.get("tax_invoice_file")
  if not upload or not (upload.filename or "").strip():
    flash("Select a tax documentation file.", "error")
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))

  try:
    payload = upload.read()
    import_rows = []
  except TaxInvoiceUploadError as exc:
    flash(str(exc), "error")
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_status.import_tax_invoice_status_file.parse",
      log_key="billing_invoices.invoices_status.import_tax_invoice_status_file.parse",
      log_window_seconds=300,
    )
    flash("Could not analyze the tax documentation file.", "error")
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))

  invoice_numbers = sorted(
    {
      invoice_number
      for import_row in import_rows
      for invoice_number in import_row.invoice_numbers
      if invoice_number
    }
  )
  conn = get_db()
  invoice_map = _load_taxable_invoice_rows(conn, invoice_numbers)

  updated_numbers: list[str] = []
  updated_invoice_ids: list[int] = []
  already_numbers: list[str] = []
  skipped_details: list[str] = []
  matched_rows = 0

  for import_row in import_rows:
    row_label = f"{import_row.sheet_name} {import_row.excel_row_number}row"
    matched_invoices = []
    missing_numbers = []
    for invoice_number in import_row.invoice_numbers:
      invoice_row = invoice_map.get(invoice_number)
      if invoice_row is None:
        missing_numbers.append(invoice_number)
      else:
        matched_invoices.append(invoice_row)

    if missing_numbers:
      skipped_details.append(f"{row_label}: Invoice ({', '.join(missing_numbers[:5])})")
      continue

    expected_supply = import_row.supply_amount
    actual_supply = sum(
      (_invoice_taxable_supply_amount(invoice_row) for invoice_row in matched_invoices),
      Decimal("0"),
    )
    if not _amounts_match(actual_supply, expected_supply):
      skipped_details.append(
        f"{row_label}: Taxable amount match ({', '.join(import_row.invoice_numbers)} / File {expected_supply:,} / Invoice {actual_supply:,})"
      )
      continue

    blocked_numbers = [
      invoice_row["number"]
      for invoice_row in matched_invoices
      if _resolve_billing_status(invoice_row) != "tax_issued"
      and not _transition_allowed(
        _resolve_billing_status(invoice_row),
        "tax_issued",
        _BILLING_TRANSITIONS,
        "draft",
      )
    ]
    if blocked_numbers:
      skipped_details.append(
        f"{row_label}: status change ({', '.join(blocked_numbers[:5])})"
      )
      continue

    matched_rows += 1
    for invoice_row in matched_invoices:
      result = _apply_tax_issued_status(conn, invoice_row)
      if result == "updated" and str(invoice_row["number"]) not in updated_numbers:
        updated_numbers.append(str(invoice_row["number"]))
        try:
          updated_invoice_ids.append(int(invoice_row["id"]))
        except (TypeError, ValueError):
          current_app.logger.warning(
            "Failed to coerce updated invoice id=%r to int",
            invoice_row.get("id"),
            exc_info=True,
          )
      elif result == "already" and str(invoice_row["number"]) not in already_numbers:
        already_numbers.append(str(invoice_row["number"]))

  conn.commit()
  conn.close()

  audit_meta = {
    "filename": upload.filename,
    "processed_rows": len(import_rows),
    "matched_rows": matched_rows,
    "updated_count": len(updated_numbers),
    "already_count": len(already_numbers),
    "skipped_count": len(skipped_details),
    "updated_numbers": updated_numbers[:100],
    "already_numbers": already_numbers[:100],
    "skipped_preview": skipped_details[:20],
  }
  log_audit(
    "invoice.tax_invoice_file_import",
    "invoice",
    None,
    json.dumps(audit_meta, ensure_ascii=False),
  )
  if updated_invoice_ids:
    try:
      from app.services.billing.invoice_manager_followup_service import (
        maybe_notify_manager_followup_for_invoice,
      )

      for updated_invoice_id in updated_invoice_ids:
        maybe_notify_manager_followup_for_invoice(
          action="invoice.tax_issued",
          invoice_id=int(updated_invoice_id),
        )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_status.import_tax_invoice_status_file.followup_notice",
        log_key="billing_invoices.invoices_status.import_tax_invoice_status_file.followup_notice",
        log_window_seconds=300,
      )

  if updated_numbers:
    flash(
      f"Marked {len(updated_numbers)} invoice(s) as tax recorded. "
      f"(Uploaded rows: {len(import_rows)}, matched rows: {matched_rows})",
      "success",
    )
  else:
    flash(
      "No new invoices were marked tax recorded from the uploaded file.",
      "warning",
    )

  if already_numbers:
    flash(
      f"{len(already_numbers)} invoice(s) were already tax recorded.",
      "warning",
    )
  if skipped_details:
    preview = " / ".join(skipped_details[:5])
    suffix = " / ..." if len(skipped_details) > 5 else ""
    flash(f"Skipped {len(skipped_details)} item(s): {preview}{suffix}", "warning")

  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))


@bp.route("/bulk_update_status", methods=["POST"])
def bulk_update_status():
  mode = (request.form.get("mode") or "billing").strip().lower()
  invoice_ids = request.form.getlist("invoice_ids[]")
  new_status = (request.form.get("new_status") or "").strip().lower()
  if not invoice_ids or not new_status:
    abort(400, "Invoice Status Select .")
  billing_allowed = {
    "draft",
    "sent",
    "tax_issued",
    "cash_issued",
    "processed",
    "void",
    "pre_overdue",
  }
  payment_allowed = {"unpaid", "pending", "paid"}
  if (mode == "billing" and new_status not in billing_allowed) or (
    mode == "payment" and new_status not in payment_allowed
  ):
    abort(400, " Statusvalue.")
  tax_issue_type = None
  if mode == "billing" and new_status == "tax_issued":
    tax_issue_type = normalize_tax_issue_type(request.form.get("tax_issue_type"), "tax_issued")
    if not tax_issue_type:
      abort(400, "Tax-record type required.")
    tax_issue_source = _tax_issue_source_from_form("bulk_update")
  else:
    tax_issue_source = None

  conn = get_db()
  user = None
  try:
    user = get_current_user()
  except Exception:
    user = None
  hooks = BulkInvoiceStatusHooks(
    transition_allowed=_transition_allowed,
    resolve_billing_status=_resolve_billing_status,
    resolve_payment_status=_resolve_payment_status,
    compute_billing_payment_from_status=_compute_billing_payment_from_status,
    derive_legacy_status_from_split=_derive_legacy_status_from_split,
    sync_legacy_status=_sync_legacy_status,
    billing_transitions=_BILLING_TRANSITIONS,
    payment_transitions=_PAYMENT_TRANSITIONS,
  )
  for invoice_id in invoice_ids:
    _ensure_not_downgrade_tax_issued(conn, int(invoice_id), new_status)

  result = apply_bulk_invoice_status_update(
    conn,
    invoice_ids,
    mode=mode,
    new_status=new_status,
    hooks=hooks,
    created_by_user_id=(user["id"] if user else None),
    timezone_name=current_app.config.get("TIMEZONE", "America/New_York"),
    tax_issue_type=tax_issue_type,
    tax_issue_source=tax_issue_source,
  )

  conn.commit()
  conn.close()

  try:
    if result.bank_activity_to_sync:
      _spawn_bank_activity_tax_downgrade(current_app._get_current_object(), result.bank_activity_to_sync)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_status.bulk_update_status.spawn_bank_activity",
      log_key="billing_invoices.invoices_status.bulk_update_status.spawn_bank_activity",
      log_window_seconds=300,
    )

  #  
  from ..auth import log_audit

  log_audit(
    "invoice.bulk_status_change",
    "invoice",
    None,
    json.dumps(
      {
        "count": len(result.updated_invoices),
        "invalid_count": len(result.invalid_invoices),
        "mode": mode,
        "new_status": new_status,
        "tax_issue_type": tax_issue_type,
        "invoice_ids": result.updated_invoice_ids,
        "invoice_numbers": result.updated_invoices,
        "invalid_numbers": result.invalid_invoices,
      },
      ensure_ascii=False,
    ),
  )
  if mode == "billing" and new_status == "tax_issued":
    try:
      from app.services.billing.invoice_manager_followup_service import (
        maybe_notify_manager_followup_for_invoice,
      )

      for invoice_id in invoice_ids:
        try:
          maybe_notify_manager_followup_for_invoice(
            action="invoice.tax_issued",
            invoice_id=int(invoice_id),
          )
        except Exception:
          continue
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_status.bulk_update_status.followup_notice",
        log_key="billing_invoices.invoices_status.bulk_update_status.followup_notice",
        log_window_seconds=300,
      )

  flash(f"✅ {len(result.updated_invoices)}items Invoice status Change.", "success")
  if result.invalid_invoices:
    preview = ", ".join(result.invalid_invoices[:10])
    suffix = "..." if len(result.invalid_invoices) > 10 else ""
    flash(
      f"days Invoice  Status  Required to Change : {preview}{suffix}",
      "warning",
    )

  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))


@bp.route("/bulk_delete", methods=["POST"])
@role_required("admin", "staff")
def bulk_delete():
  invoice_ids = request.form.getlist("invoice_ids[]")
  if not invoice_ids:
    abort(400, "Delete Invoice Select .")

  conn = get_db()
  user = None
  try:
    user = get_current_user()
  except Exception:
    user = None
  hooks = InvoiceDeleteHooks(
    resolve_billing_status=_resolve_billing_status,
    resolve_payment_status=_resolve_payment_status,
  )
  try:
    result = delete_invoices(
      conn,
      invoice_ids,
      hooks,
      created_by_user_id=(user["id"] if user else None),
      skip_missing=True,
      error_context="billing_invoices.invoices_status.bulk_delete",
    )
  except InvoiceDeleteBlockedError as exc:
    conn.close()
    abort(403, exc.message)
  except InvoiceDeleteExecutionError as exc:
    conn.close()
    flash(exc.message, "error")
    return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))
  conn.close()

  log_invoice_delete_cancel_audits(
    result.canceled_deposit_entries,
    error_context="billing_invoices.invoices_status.bulk_delete.cancel_deposit_before_delete",
  )
  record_bulk_invoice_delete_operation(result.snapshots, result.deleted_numbers)

  log_audit(
    "invoice.bulk_delete",
    "invoice",
    None,
    f'{{"count": {len(result.deleted_invoice_ids)}, "invoice_numbers": {result.deleted_numbers}}}',
  )

  flash(f"🗑️ {len(result.deleted_invoice_ids)}items Invoice Delete.", "success")

  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))


@bp.route("/<int:invoice_id>/publish_and_print", methods=["POST"])
def publish_and_print(invoice_id):
  conn = get_db()
  row = conn.execute(
    "SELECT number, status, billing_status, payment_status, payment_verified, is_outgoing FROM invoices WHERE id=?",
    (invoice_id,),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)
  # Preserve language params if provided; outgoing layout follows the saved invoice type.
  lang = (request.form.get("lang") or request.args.get("lang") or "").strip() or None
  outgoing = "1" if _stored_outgoing_mode(row) else "0"
  fit = (request.form.get("fit") or request.args.get("fit") or "").strip() or None
  if fit != "1":
    fit = None

  # Only "publish" when the invoice is still in draft. Otherwise, do not mutate state here.
  legacy_status = str(row_get(row, "status", default="") or "").strip().lower()
  current_billing = _resolve_billing_status(row)
  if legacy_status != "draft" or current_billing != "draft":
    conn.close()
    return redirect(
      url_for(
        ".view_invoice",
        invoice_id=invoice_id,
        print=1,
        lang=lang,
        outgoing=outgoing,
        fit=fit,
      )
    )

  if not _transition_allowed(current_billing, "sent", _BILLING_TRANSITIONS, "draft"):
    conn.close()
    return _reject_transition(
      " Billing status .",
      invoice_id=invoice_id,
      status_code=409,
    )

  try:
    invoice_cols = _get_column_names(conn, "invoices")
  except Exception:
    invoice_cols = set()
  if lang in {"en"} and "language" in invoice_cols:
    conn.execute(
      "UPDATE invoices SET billing_status='sent', language=? WHERE id=?",
      (lang, invoice_id),
    )
  else:
    conn.execute("UPDATE invoices SET billing_status='sent' WHERE id=?", (invoice_id,))
  _sync_legacy_status(
    conn,
    invoice_id,
    billing_status="sent",
    payment_status=_resolve_payment_status(row),
  )
  conn.commit()
  conn.close()
  #  (Issued Process, Change )
  from ..auth import log_audit

  log_audit("invoice.publish", "invoice", invoice_id, '{"to_status":"sent"}')
  return redirect(
    url_for(
      ".view_invoice",
      invoice_id=invoice_id,
      print=1,
      lang=lang,
      outgoing=outgoing,
      fit=fit,
    )
  )
