from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal

from flask import abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from app.services.billing.utils import compute_totals, compute_totals_minor, from_minor, to_minor
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_invoice_manager

from ..auth import get_current_user, log_audit, role_required
from ..db import (
  build_client_deposit_audit_meta,
  get_client_deposit_balance_minor,
  get_db,
  insert_client_deposit_ledger_entry,
  row_get,
  row_to_dict,
  safe_json_parse,
)
from .invoices import (
  _PAYMENT_TRANSITIONS,
  _parse_amount_to_minor,
  _parse_int_amount_usd,
  _reject_transition,
  _resolve_billing_status,
  _resolve_payment_status,
  _sync_legacy_status,
  _transition_allowed,
  bp,
)


@bp.route("/<int:invoice_id>/deposit/apply", methods=["POST"])
@role_required("admin", "staff")
def apply_deposit(invoice_id: int):
  conn = get_db()
  invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not invoice:
    conn.close()
    abort(404)
  current_billing = _resolve_billing_status(invoice)
  auto_publish = current_billing == "draft"
  invoice = row_to_dict(invoice)
  if current_billing == "void":
    conn.close()
    abort(400, "Retainer deposits are not available for void invoices.")
  payment_verified_raw = invoice.get("payment_verified", 0)
  try:
    payment_verified = int(payment_verified_raw or 0)
  except (TypeError, ValueError):
    payment_verified = 0
  if payment_verified == 1:
    conn.close()
    abort(400, "Payment verification is already complete for this invoice.")

  client_id = int(invoice["client_id"])
  business_profile_id = int(invoice["business_profile_id"] or 1)
  currency = (invoice["currency"] or "USD").strip().upper()
  user = get_current_user()

  try:
    total_minor_val = row_get(invoice, "total_minor", default=None)
    total_minor = (
      total_minor_val
      if total_minor_val is not None
      else _parse_amount_to_minor(invoice["total"], currency)
    )
    total_minor = int(total_minor)
  except Exception:
    total_minor = 0

  try:
    s = conn.execute(
      """
      SELECT COALESCE(SUM(amount_minor), 0) AS s
      FROM client_deposit_ledger
      WHERE related_invoice_id=?
       AND entry_type IN ('apply','cancel_apply')
      """,
      (invoice_id,),
    ).fetchone()
    sum_minor = int(row_get(s, "s", 0, 0) or 0)
    already_applied = max(0, -sum_minor)
  except Exception:
    already_applied = 0
  outstanding = max(0, int(total_minor) - int(already_applied))
  if outstanding <= 0:
    conn.close()
    abort(400, " Paid Invoice.")

  amount_raw = (request.form.get("amount") or "").strip()
  memo = (request.form.get("memo") or "").strip() or None
  if not memo:
    try:
      memo = f"invoice:{invoice['number']}"
    except Exception:
      memo = f"invoice:{invoice_id}"

  try:
    if amount_raw:
      req_minor = abs(int(_parse_amount_to_minor(amount_raw, currency)))
    else:
      req_minor = int(outstanding)
  except Exception as e:
    # SECURITY: do not leak internal parsing errors to users
    try:
      report_swallowed_exception(
        e,
        context="billing_invoices.invoices_payments.apply_deposit.parse_amount",
        log_key="billing_invoices.invoices_payments.apply_deposit.parse_amount",
        log_window_seconds=300,
      )
    except Exception as log_exc:
      # best-effort: do not fail the request due to logging/reporting issues
      try:
        current_app.logger.debug(
          "apply_deposit: report_swallowed_exception failed: %s",
          str(log_exc),
        )
      except Exception:
        log_exc = None
    conn.close()
    abort(400, "Amount is invalid.")

  if req_minor <= 0:
    conn.close()
    abort(400, "Amount is invalid.")
  if req_minor > outstanding:
    req_minor = int(outstanding)

  bal_bp = get_client_deposit_balance_minor(conn, business_profile_id, client_id, currency)
  bal_global = get_client_deposit_balance_minor(conn, None, client_id, currency)
  available = int(bal_bp) + int(bal_global)
  if not amount_raw:
    req_minor = min(int(req_minor), int(available))
  elif int(req_minor) > int(available):
    conn.close()
    abort(400, "Retainer balance .")
  if int(req_minor) <= 0:
    conn.close()
    abort(400, "Retainer balance .")

  use_bp = min(int(req_minor), int(bal_bp))
  use_global = int(req_minor) - int(use_bp)
  if int(use_global) > int(bal_global):
    conn.close()
    abort(400, "Retainer balance .")

  new_outstanding = int(outstanding) - int(req_minor)
  new_payment_status = "paid" if new_outstanding <= 0 else "pending"
  new_payment_verified = 1 if new_outstanding <= 0 else 0

  try:
    meta = safe_json_parse(invoice.get("payment_meta"), {})
  except Exception:
    meta = {}
  if not isinstance(meta, dict):
    meta = {}
  if new_payment_verified == 1:
    meta["verified_by_user_id"] = user["id"] if user else None
    meta["verified_by_username"] = user["username"] if user else None
    meta["verified_at"] = datetime.now().isoformat(timespec="seconds")
    meta["verified_via"] = "deposit"
  else:
    if meta.get("verified_via") == "deposit":
      meta.pop("verified_by_user_id", None)
      meta.pop("verified_by_username", None)
      meta.pop("verified_at", None)
      meta.pop("verified_via", None)

  inserted_entries = []
  try:
    if not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")
    if use_bp > 0:
      res = insert_client_deposit_ledger_entry(
        conn,
        business_profile_id,
        client_id,
        currency,
        -int(use_bp),
        "apply",
        memo=memo,
        related_invoice_id=invoice_id,
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      inserted_entries.append(
        {
          "entry_id": res.get("entry_id"),
          "business_profile_id": int(business_profile_id),
          "amount_minor": -int(use_bp),
          "balance_before_minor": res.get("balance_before_minor"),
          "balance_after_minor": res.get("balance_after_minor"),
        }
      )

    if use_global > 0:
      res = insert_client_deposit_ledger_entry(
        conn,
        None,
        client_id,
        currency,
        -int(use_global),
        "apply",
        memo=memo,
        related_invoice_id=invoice_id,
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      inserted_entries.append(
        {
          "entry_id": res.get("entry_id"),
          "business_profile_id": None,
          "amount_minor": -int(use_global),
          "balance_before_minor": res.get("balance_before_minor"),
          "balance_after_minor": res.get("balance_after_minor"),
        }
      )

    if auto_publish:
      conn.execute(
        "UPDATE invoices SET billing_status='sent', payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
        (
          new_payment_status,
          new_payment_verified,
          json.dumps(meta, ensure_ascii=False),
          invoice_id,
        ),
      )
      _sync_legacy_status(
        conn, invoice_id, billing_status="sent", payment_status=new_payment_status
      )
    else:
      conn.execute(
        "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
        (
          new_payment_status,
          new_payment_verified,
          json.dumps(meta, ensure_ascii=False),
          invoice_id,
        ),
      )
      _sync_legacy_status(conn, invoice_id)
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoices_payments.apply_deposit.rollback",
        log_key="billing_invoices.invoices_payments.apply_deposit.rollback",
        log_window_seconds=300,
      )
    conn.close()
    if isinstance(exc, HTTPException):
      raise
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.apply_deposit",
      log_key="billing_invoices.invoices_payments.apply_deposit",
      log_window_seconds=300,
    )
    abort(500, "Retainer application Process Error .")

  for e in inserted_entries:
    audit_meta = build_client_deposit_audit_meta(
      entry_id=e.get("entry_id"),
      business_profile_id=e.get("business_profile_id"),
      client_id=client_id,
      currency=currency,
      amount_minor=e.get("amount_minor"),
      entry_type="apply",
      memo=memo,
      related_invoice_id=invoice_id,
      balance_before_minor=e.get("balance_before_minor"),
      balance_after_minor=e.get("balance_after_minor"),
    )
    log_audit("invoice.deposit.apply", "invoice", invoice_id, audit_meta)
  conn.close()
  if auto_publish:
    try:
      log_audit(
        "invoice.publish", "invoice", invoice_id, '{"to_status":"sent","via":"deposit"}'
      )
    except Exception as log_exc:
      # Best-effort; deposit apply should not fail due to logging.
      report_swallowed_exception(
        log_exc,
        context="billing_invoices.invoices_payments.apply_deposit.auto_publish.audit",
        log_key="billing_invoices.invoices_payments.apply_deposit.auto_publish.audit",
        log_window_seconds=300,
      )
    flash("Retainer application Status 'Issued'to Auto Change.", "warning")
  flash("Retainer .", "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/deposit/cancel_apply", methods=["POST"])
@role_required("admin", "staff")
def cancel_deposit_apply(invoice_id: int):
  conn = get_db()
  invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not invoice:
    conn.close()
    abort(404)
  invoice = row_to_dict(invoice)

  client_id = int(invoice["client_id"])
  currency = (invoice["currency"] or "USD").strip().upper()
  user = get_current_user()

  entry_id_raw = (request.form.get("entry_id") or "").strip()
  if not entry_id_raw.isdigit():
    conn.close()
    abort(400, "Cancel target required.")
  target_entry_id = int(entry_id_raw)

  apply_row = conn.execute(
    """
    SELECT *
    FROM client_deposit_ledger
    WHERE id=? AND entry_type='apply' AND related_invoice_id=?
    """,
    (target_entry_id, invoice_id),
  ).fetchone()
  if not apply_row:
    conn.close()
    abort(400, "Cancel target not found.")

  try:
    apply_bp_id = (
      int(apply_row["business_profile_id"])
      if apply_row["business_profile_id"] is not None
      else None
    )
  except Exception:
    apply_bp_id = None

  try:
    apply_amt = int(apply_row["amount_minor"])
  except Exception:
    conn.close()
    abort(400, "Cancel target Amount is invalid.")
  cancel_amt = -apply_amt
  if cancel_amt <= 0:
    conn.close()
    abort(400, "Cancel target Amount is invalid.")

  memo = (request.form.get("memo") or "").strip() or None
  if not memo:
    memo = "cancel_apply"

  try:
    total_minor_val = row_get(invoice, "total_minor", default=None)
    total_minor = (
      total_minor_val
      if total_minor_val is not None
      else _parse_amount_to_minor(invoice["total"], currency)
    )
    total_minor = int(total_minor)
  except Exception:
    total_minor = 0
  try:
    if not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")
    res = insert_client_deposit_ledger_entry(
      conn,
      apply_bp_id,
      client_id,
      currency,
      int(cancel_amt),
      "cancel_apply",
      memo=memo,
      related_invoice_id=invoice_id,
      related_entry_id=target_entry_id,
      created_by=(user["id"] if user else None),
      begin_immediate=False,
      commit_if_started=False,
    )

    try:
      s = conn.execute(
        """
        SELECT COALESCE(SUM(amount_minor), 0) AS s
        FROM client_deposit_ledger
        WHERE related_invoice_id=?
         AND entry_type IN ('apply','cancel_apply')
        """,
        (invoice_id,),
      ).fetchone()
      sum_minor = int(row_get(s, "s", 0, 0) or 0)
      now_applied = max(0, -sum_minor)
    except Exception:
      now_applied = 0
    new_outstanding = max(0, int(total_minor) - int(now_applied))
    new_payment_status = "paid" if new_outstanding <= 0 and int(total_minor) > 0 else "pending"
    new_payment_verified = 1 if new_payment_status == "paid" and int(now_applied) > 0 else 0

    try:
      meta = json.loads(invoice["payment_meta"]) if invoice["payment_meta"] else {}
    except Exception:
      meta = {}
    if not isinstance(meta, dict):
      meta = {}
    if new_payment_verified == 1:
      meta["verified_by_user_id"] = user["id"] if user else None
      meta["verified_by_username"] = user["username"] if user else None
      meta["verified_at"] = datetime.now().isoformat(timespec="seconds")
      meta["verified_via"] = "deposit"
    else:
      if meta.get("verified_via") == "deposit":
        meta.pop("verified_by_user_id", None)
        meta.pop("verified_by_username", None)
        meta.pop("verified_at", None)
        meta.pop("verified_via", None)

    conn.execute(
      "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        new_payment_status,
        new_payment_verified,
        json.dumps(meta, ensure_ascii=False),
        invoice_id,
      ),
    )
    _sync_legacy_status(conn, invoice_id)
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoices_payments.cancel_deposit_apply.rollback",
        log_key="billing_invoices.invoices_payments.cancel_deposit_apply.rollback",
        log_window_seconds=300,
      )
    conn.close()
    if isinstance(exc, HTTPException):
      raise
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.cancel_deposit_apply",
      log_key="billing_invoices.invoices_payments.cancel_deposit_apply",
      log_window_seconds=300,
    )
    abort(500, "Retainer applicationCancel Process Error .")

  audit_meta = build_client_deposit_audit_meta(
    entry_id=res.get("entry_id"),
    business_profile_id=apply_bp_id,
    client_id=client_id,
    currency=currency,
    amount_minor=int(cancel_amt),
    entry_type="cancel_apply",
    memo=memo,
    related_invoice_id=invoice_id,
    related_entry_id=target_entry_id,
    balance_before_minor=res.get("balance_before_minor"),
    balance_after_minor=res.get("balance_after_minor"),
  )
  log_audit("invoice.deposit.cancel_apply", "invoice", invoice_id, audit_meta)
  conn.close()
  flash("Retainer application Cancel.", "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


def _verify_local_payment_meta(invoice_row, meta: dict) -> tuple[bool, str]:
  """Check if payment meta fully matches a USD/local-currency invoice."""
  if not meta:
    return False, "Payment No data."
  inv_currency = (invoice_row["currency"] or "USD").upper()

  if not meta.get("currency"):
    meta["currency"] = inv_currency
  meta_currency = (meta.get("currency") or inv_currency).upper()
  meta["currency"] = meta_currency
  if meta_currency != inv_currency:
    return (
      False,
      f"Currency mismatch. (Payment {meta_currency} / Invoice {inv_currency})",
    )

  try:
    deposit_minor = _payment_deposit_minor(meta.get("deposit"), inv_currency)
  except Exception:
    return False, "Deposit amount is invalid."

  inv_total_minor = 0
  total_minor_val = row_get(invoice_row, "total_minor", default=None)
  if total_minor_val is not None:
    inv_total_minor = int(total_minor_val or 0)
  else:
    inv_total_minor = int(
      to_minor(Decimal(str(row_get(invoice_row, "total", default=0) or 0)), inv_currency)
      or 0
    )

  if deposit_minor != inv_total_minor:
    return (
      False,
      f"Deposit amount does not match invoice total. (Deposit {deposit_minor:,} / Invoice {inv_total_minor:,} in minor units)",
    )

  required_keys = ["date", "account_alias", "deposit", "summary"]
  for k in required_keys:
    val = meta.get(k)
    if val is None or not str(val).strip():
      return False, f"Required field: {k}"
  return True, "OK"


def _verify_fx_payment_meta(invoice_row, meta: dict) -> tuple[bool, str]:
  if not meta:
    return False, "Payment No data."
  inv_currency = (invoice_row["currency"] or "").upper()
  if not inv_currency or inv_currency == "USD":
    return False, "FX Invoice ."

  meta_currency = (meta.get("currency") or inv_currency).upper()
  meta["currency"] = meta_currency
  if meta_currency != inv_currency:
    return (
      False,
      f"Currency . (Payment {meta_currency} / Invoice {inv_currency})",
    )

  try:
    deposit_minor = _parse_amount_to_minor(meta.get("deposit"), inv_currency)
  except Exception:
    return False, "Deposit amount is invalid."

  inv_total_minor = 0
  try:
    total_minor_val = row_get(invoice_row, "total_minor", default=None)
    if total_minor_val is not None:
      inv_total_minor = int(total_minor_val or 0)
    else:
      inv_total_minor = int(
        to_minor(Decimal(str(invoice_row["total"] or 0)), inv_currency) or 0
      )
  except Exception:
    inv_total_minor = 0

  if deposit_minor != inv_total_minor:
    return (
      False,
      f"Deposit amount Total . (Deposit {deposit_minor:,} / Billing {inv_total_minor:,} in minor units)",
    )

  required_keys = ["date", "deposit"]
  for k in required_keys:
    val = meta.get(k)
    if val is None or not str(val).strip():
      return False, f"Required : {k}"
  return True, "OK"


def _payment_meta_currency(currency: str | None) -> str:
  return (currency or "USD").strip().upper()


def _not_deleted_sql(column: str = "is_deleted") -> str:
  return (
    f"COALESCE(LOWER(CAST({column} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def _payment_deposit_minor(value, currency: str | None) -> int:
  cur = _payment_meta_currency(currency)
  raw = str(value or "0").replace(",", "").strip()
  if not raw:
    return 0
  try:
    return int(to_minor(Decimal(raw), cur) or 0)
  except Exception:
    return int(_parse_amount_to_minor(value, cur) or 0)


def _payment_deposit_value_from_minor(amount_minor: int, currency: str | None):
  cur = _payment_meta_currency(currency)
  amount = from_minor(int(amount_minor or 0), cur)
  if amount == amount.to_integral_value():
    return int(amount)
  return float(amount)


def _normalize_payment_deposit_value(value, currency: str | None):
  return _payment_deposit_value_from_minor(_payment_deposit_minor(value, currency), currency)


def _sum_payment_deposit_values(deposits: list[dict], currency: str | None):
  total_minor = 0
  for item in deposits or []:
    if not isinstance(item, dict):
      continue
    total_minor += _payment_deposit_minor(item.get("deposit"), currency)
  return _payment_deposit_value_from_minor(total_minor, currency)


def _payment_meta_records(meta: dict | None) -> list[dict]:
  if not isinstance(meta, dict):
    return []
  deposits = meta.get("deposits")
  if isinstance(deposits, list):
    return [item for item in deposits if isinstance(item, dict)]
  if any(meta.get(key) not in (None, "") for key in ("tid", "summary", "deposit")):
    return [meta]
  return []


def _bank_activity_memo_invoice_numbers(memo: str | None) -> list[str]:
  numbers: list[str] = []
  for match in re.findall(r"INV:([^\s|]+)", str(memo or "")):
    inv_no = str(match or "").strip()
    if inv_no and inv_no not in numbers:
      numbers.append(inv_no)
  return numbers


def _sum_tid_allocations_minor(
  conn,
  tid: str,
  currency: str | None,
  *,
  exclude_invoice_id: int | None = None,
) -> int:
  key = str(tid or "").strip()
  if not key:
    return 0
  like_a = f'%"tid": "{key}"%'
  like_b = f'%"tid":"{key}"%'
  try:
    rows = conn.execute(
      f"""
      SELECT id, currency, payment_meta
      FROM invoices
      WHERE (payment_meta LIKE ? OR payment_meta LIKE ?)
       AND {_not_deleted_sql()}
       AND COALESCE(billing_status, '') != 'void'
      """,
      (like_a, like_b),
    ).fetchall()
  except Exception:
    return 0

  total = 0
  for row in rows or []:
    try:
      invoice_id = int(row_get(row, "id", 0, 0) or 0)
    except Exception:
      try:
        invoice_id = int(row[0] or 0)
      except Exception:
        invoice_id = 0
    if exclude_invoice_id is not None and invoice_id == int(exclude_invoice_id):
      continue
    try:
      row_currency = row_get(row, "currency", 1, currency) or currency
      raw_meta = row_get(row, "payment_meta", 2, None)
    except Exception:
      row_currency = currency
      raw_meta = None
    try:
      parsed = json.loads(raw_meta) if raw_meta else None
    except Exception:
      parsed = None
    for record in _payment_meta_records(parsed):
      if str(record.get("tid") or "").strip() != key:
        continue
      try:
        total += _payment_deposit_minor(record.get("deposit"), row_currency or currency)
      except Exception:
        continue
  return int(total)


def _bank_activity_tid_amount_minor(conn, tid: str, currency: str | None) -> int:
  key = str(tid or "").strip()
  if not key:
    return 0
  try:
    row = conn.execute("SELECT acc_in FROM bank_transactions WHERE tid=?", (key,)).fetchone()
  except Exception:
    return 0
  if not row:
    return 0
  try:
    value = row_get(row, "acc_in", 0, 0)
  except Exception:
    try:
      value = row[0]
    except Exception:
      value = 0
  try:
    return int(value or 0)
  except Exception:
    return 0


@bp.route("/<int:invoice_id>/save_payment_meta", methods=["POST"])
def save_payment_meta(invoice_id):
  """Save bank transaction fields into invoices.payment_meta (JSON).
  Editable in draft/sent/payment_pending states.
  """
  from flask import jsonify

  conn = get_db()
  invoice = conn.execute(
    "SELECT id, number, status, billing_status, currency, total, total_minor, vat_rate, payment_verified, payment_meta, client_id FROM invoices WHERE id=?",
    (invoice_id,),
  ).fetchone()
  if not invoice:
    conn.close()
    abort(404)

  # Edit Status: Draft, Issued, Payment pending, Tax recorded
  # 'paid' Status  (Manual Payment Process ) Edit 
  allowed_statuses = (
    "draft",
    "sent",
    "payment_pending",
    "pre_overdue",
    "tax_issued",
    "cash_issued",
    "processed",
  )
  is_paid_but_unverified = invoice["status"] == "paid" and not invoice["payment_verified"]

  if invoice["status"] not in allowed_statuses and not is_paid_but_unverified:
    conn.close()
    abort(
      403,
      "Payment verification is already complete and cannot be edited.",
    )
  # Accept JSON or form
  payload = None
  if request.is_json:
    payload = request.get_json(silent=True) or {}
    meta = payload.get("payment_meta") or payload
  else:
    f = request.form
    meta = {
      "account_alias": f.get("account_alias", "").strip(),
      "date": f.get("date", "").strip(),
      "time": f.get("time", "").strip(),
      "deposit": f.get("deposit", "").strip(),
      "currency": f.get("currency", "USD").strip(),
      "summary": f.get("summary", "").strip(),
      "channel": f.get("channel", "").strip(),
      "cms_code": f.get("cms_code", "").strip(),
    }

  # Currency Auto ( column Invoice Currency)
  try:
    default_currency = (invoice["currency"] or "USD").strip().upper()
  except Exception:
    default_currency = "USD"
  if not meta.get("currency") or not str(meta.get("currency") or "").strip():
    meta["currency"] = default_currency

  try:
    invoice_number = str(invoice["number"] or "").strip()
  except Exception:
    invoice_number = ""

  inv_total_minor = 0
  try:
    inv_total_minor = int(invoice["total_minor"] or 0)
  except Exception:
    inv_total_minor = 0
  if inv_total_minor <= 0:
    try:
      inv_total_minor = int(
        to_minor(
          Decimal(str(invoice["total"] or 0)),
          (invoice["currency"] or default_currency).strip().upper(),
        )
        or 0
      )
    except Exception:
      inv_total_minor = 0

  meta_to_save = meta
  tid = ""
  allow_multi_invoice_tid = False
  if request.is_json and isinstance(payload, dict):
    tid = str(payload.get("tid") or "").strip()
    allow_multi_invoice_tid = bool(
      payload.get("allow_multi_invoice")
      or payload.get("allow_n_to_one")
      or payload.get("split_match")
    )
  if tid:
    try:
      tx = conn.execute(
        "SELECT memo FROM bank_transactions WHERE tid=?",
        (tid,),
      ).fetchone()
      tx_memo = ""
      if tx:
        try:
          tx_memo = row_get(tx, "memo", 0, "") or ""
        except Exception:
          try:
            tx_memo = (tx[0] if tx else "") or ""
          except Exception:
            tx_memo = ""
      memo_invoice_numbers = _bank_activity_memo_invoice_numbers(tx_memo)
      other_invoice_numbers = [
        no for no in memo_invoice_numbers if invoice_number and no != invoice_number
      ]
      if other_invoice_numbers and not allow_multi_invoice_tid:
        already = ", ".join(other_invoice_numbers)
        conn.close()
        return (
          jsonify(
            {
              "success": False,
              "error": (
                f" Invoice({already}) Matching Deposit. "
                " Deposit Invoice Matching N:1 Matchingto Open."
              ),
            }
          ),
          400,
        )
      allocated_elsewhere = _sum_tid_allocations_minor(
        conn,
        tid,
        default_currency,
        exclude_invoice_id=invoice_id,
      )
      if allocated_elsewhere > 0 and not allow_multi_invoice_tid:
        conn.close()
        return (
          jsonify(
            {
              "success": False,
              "error": (
                " Invoice Matching Deposit. "
                " Deposit Invoice Matching N:1 Matchingto Open."
              ),
            }
          ),
          400,
        )
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_payments.save_payment_meta.check_bank_activity_tid",
        log_key="billing_invoices.invoices_payments.save_payment_meta.check_bank_activity_tid",
        log_window_seconds=300,
      )

  if request.is_json and isinstance(payload, dict) and payload.get("append"):
    try:
      existing_raw = invoice.get("payment_meta")
    except Exception:
      try:
        existing_raw = invoice["payment_meta"]
      except Exception:
        existing_raw = None
    try:
      existing = json.loads(existing_raw) if existing_raw else None
    except Exception:
      existing = None
    if not isinstance(existing, dict):
      existing = {}
    deposits = existing.get("deposits")
    if not isinstance(deposits, list):
      deposits = []

    if not deposits and (existing.get("deposit") is not None):
      try:
        deposits.append(
          {
            "currency": existing.get("currency") or default_currency,
            "date": existing.get("date") or "",
            "account_alias": existing.get("account_alias") or "",
            "deposit": _normalize_payment_deposit_value(
              existing.get("deposit"),
              default_currency,
            ),
            "summary": existing.get("summary") or "",
          }
        )
      except Exception:
        deposits = []

    rec = dict(meta or {})
    if tid:
      rec["tid"] = tid
    rec["deposit"] = _normalize_payment_deposit_value(
      rec.get("deposit"),
      default_currency,
    )

    if tid:
      if not any(str(d.get("tid") or "") == tid for d in deposits):
        deposits.append(rec)
    else:
      deposits.append(rec)

    parts = []
    for d in deposits:
      if not isinstance(d, dict):
        continue
      s = str(d.get("summary") or "").strip()
      if s:
        parts.append(s)
    deposit_total = _sum_payment_deposit_values(deposits, default_currency)
    deposit_total_minor = _payment_deposit_minor(deposit_total, default_currency)
    if tid:
      try:
        current_tid_total_minor = sum(
          _payment_deposit_minor(d.get("deposit"), d.get("currency") or default_currency)
          for d in deposits
          if isinstance(d, dict) and str(d.get("tid") or "").strip() == tid
        )
      except Exception:
        current_tid_total_minor = 0
      tx_total_minor = _bank_activity_tid_amount_minor(conn, tid, default_currency)
      if tx_total_minor > 0 and current_tid_total_minor > 0:
        allocated_elsewhere = _sum_tid_allocations_minor(
          conn,
          tid,
          default_currency,
          exclude_invoice_id=invoice_id,
        )
        if int(allocated_elsewhere) + int(current_tid_total_minor) > int(tx_total_minor):
          conn.close()
          remaining_minor = max(0, int(tx_total_minor) - int(allocated_elsewhere))
          remaining_value = _payment_deposit_value_from_minor(
            remaining_minor,
            default_currency,
          )
          return (
            jsonify(
              {
                "success": False,
                "error": (
                  "Deposit allocation exceeds available amount. "
                  f"(Remaining {remaining_value} {default_currency})"
                ),
              }
            ),
            400,
          )

    existing["deposits"] = deposits
    existing["currency"] = meta.get("currency") or existing.get("currency") or default_currency
    existing["date"] = meta.get("date") or existing.get("date") or ""
    existing["account_alias"] = meta.get("account_alias") or existing.get("account_alias") or ""
    existing["deposit"] = deposit_total
    if int(inv_total_minor or 0) > 0 and int(deposit_total_minor or 0) > int(
      inv_total_minor or 0
    ):
      conn.close()
      return (
        jsonify(
          {
            "success": False,
            "error": f" Matching . (Current Total {deposit_total} {default_currency} / Invoice Total minor {int(inv_total_minor or 0):,})",
          }
        ),
        400,
      )
    if parts:
      existing["summary"] = " + ".join(parts)
    meta_to_save = existing

  # Final strict over-match check (applies to both append and replace)
  try:
    check_deposit = _payment_deposit_minor(
      (meta_to_save or {}).get("deposit"),
      default_currency,
    )
  except Exception:
    check_deposit = 0
  if int(inv_total_minor or 0) > 0 and int(check_deposit or 0) > int(inv_total_minor or 0):
    conn.close()
    return (
      jsonify(
        {
          "success": False,
          "error": f" Matching . (Deposit {int(check_deposit or 0):,} / Invoice Total {int(inv_total_minor or 0):,})",
        }
      ),
      400,
    )

  # Save
  conn.execute(
    "UPDATE invoices SET payment_meta=?, payment_verified=0 WHERE id=?",
    (json.dumps(meta_to_save, ensure_ascii=False), invoice_id),
  )
  conn.commit()
  try:
    from .bank_activity import invalidate_client_payer_history_cache

    invalidate_client_payer_history_cache([row_get(invoice, "client_id", default=None)])
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.save_payment_meta.invalidate_cache",
      log_key="billing_invoices.invoices_payments.save_payment_meta.invalidate_cache",
      log_window_seconds=300,
    )
  conn.close()
  # 
  from ..auth import log_audit

  log_audit("invoice.payment_meta.save", "invoice", invoice_id)
  if request.is_json:
    return jsonify({"success": True, "payment_meta": meta_to_save}), 200
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/unmatch_deposit", methods=["POST"])
def unmatch_deposit(invoice_id: int):
  data = request.get_json(silent=True) or {}
  tid = str(data.get("tid") or "").strip()
  if not tid:
    return jsonify({"success": False, "error": "tid is required"}), 400

  conn = get_db()
  invoice = conn.execute(
    "SELECT id, number, status, billing_status, currency, total_minor, payment_meta FROM invoices WHERE id=?",
    (invoice_id,),
  ).fetchone()
  if not invoice:
    conn.close()
    abort(404)

  try:
    invoice_number = str(invoice["number"] or "").strip()
  except Exception:
    invoice_number = ""

  try:
    existing_raw = invoice["payment_meta"]
  except Exception:
    existing_raw = None
  try:
    meta = json.loads(existing_raw) if existing_raw else None
  except Exception:
    meta = None
  if not isinstance(meta, dict):
    meta = {}

  deposits = meta.get("deposits")
  if not isinstance(deposits, list):
    deposits = []

  before_len = len(deposits)
  deposits = [d for d in deposits if str((d or {}).get("tid") or "") != tid]
  if len(deposits) == before_len:
    conn.close()
    return (
      jsonify({"success": False, "error": " Deposit(tid) Matching ."}),
      400,
    )

  meta_to_save = None
  deposit_total = _sum_payment_deposit_values(deposits, invoice["currency"])
  deposit_total_minor = _payment_deposit_minor(deposit_total, invoice["currency"])
  if deposits and int(deposit_total_minor or 0) > 0:
    meta["deposits"] = deposits
    meta["deposit"] = deposit_total
    meta_to_save = meta

  ok = False
  reason = "OK"
  payment_status = "unpaid"
  payment_verified = 0

  try:
    inv_currency = (invoice["currency"] or "USD").strip().upper()
  except Exception:
    inv_currency = "USD"

  if meta_to_save and int(deposit_total_minor or 0) > 0:
    if not meta_to_save.get("currency") or not str(meta_to_save.get("currency") or "").strip():
      meta_to_save["currency"] = inv_currency
    if inv_currency in {"USD", "USD"}:
      ok, reason = _verify_local_payment_meta(invoice, meta_to_save)
    else:
      ok, reason = _verify_fx_payment_meta(invoice, meta_to_save)
    payment_status = "paid" if ok else "pending"
    payment_verified = 1 if ok else 0

    if not ok:
      meta_to_save["verification_error"] = reason
      meta_to_save["verification_failed_at"] = datetime.now().isoformat()
    else:
      meta_to_save.pop("verification_error", None)
      meta_to_save.pop("verification_failed_at", None)
  else:
    reason = "Payment No data."

  try:
    billing_status = row_get(invoice, "billing_status", default=None)
  except Exception:
    billing_status = None

  if billing_status in ("tax_issued", "cash_issued", "processed"):
    conn.execute(
      "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        payment_status,
        payment_verified,
        (json.dumps(meta_to_save, ensure_ascii=False) if meta_to_save else None),
        invoice_id,
      ),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status=billing_status,
      payment_status=payment_status,
    )
  else:
    conn.execute(
      "UPDATE invoices SET billing_status='sent', payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        payment_status,
        payment_verified,
        (json.dumps(meta_to_save, ensure_ascii=False) if meta_to_save else None),
        invoice_id,
      ),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status="sent",
      payment_status=payment_status,
    )

  def _remove_inv_tag(memo: str) -> str:
    try:
      parts = [p.strip() for p in str(memo or "").split("|")]
    except Exception:
      parts = []
    kept = []
    for p in parts:
      if not p:
        continue
      if p == "INV" and not invoice_number:
        continue
      if p.startswith("INV:"):
        tagged_no = p[4:].strip()
        if invoice_number and tagged_no != invoice_number:
          kept.append(p)
          continue
        continue
      kept.append(p)
    return " | ".join(kept)

  try:
    tx = conn.execute(
      "SELECT memo FROM bank_transactions WHERE tid=?",
      (tid,),
    ).fetchone()
    if tx:
      try:
        prev_memo = row_get(tx, "memo", 0, "") or ""
      except Exception:
        prev_memo = (tx[0] if tx else "") or ""
      next_memo = _remove_inv_tag(prev_memo)
      if next_memo != prev_memo:
        conn.execute(
          "UPDATE bank_transactions SET memo=?, tax_invoice_override=NULL, tax_invoice_issued=0, tax_invoice_issued_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE tid=?",
          (next_memo, tid),
        )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.unmatch_deposit.update_bank_activity_memo",
      log_key="billing_invoices.invoices_payments.unmatch_deposit.update_bank_activity_memo",
      log_window_seconds=300,
    )

  conn.commit()
  try:
    from .bank_activity import invalidate_client_payer_history_cache

    invalidate_client_payer_history_cache([row_get(invoice, "client_id", default=None)])
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.unmatch_deposit.invalidate_cache",
      log_key="billing_invoices.invoices_payments.unmatch_deposit.invalidate_cache",
      log_window_seconds=300,
    )
  conn.close()

  try:
    log_audit(
      "invoice.payment_meta.unmatch",
      "invoice",
      invoice_id,
      json.dumps({"tid": tid, "invoiceNumber": invoice_number}, ensure_ascii=False),
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_payments.unmatch_deposit.log_audit",
      log_key="billing_invoices.invoices_payments.unmatch_deposit.log_audit",
      log_window_seconds=300,
    )

  return (
    jsonify(
      {
        "success": True,
        "payment_meta": meta_to_save,
        "ok": ok,
        "reason": reason,
      }
    ),
    200,
  )


@bp.route("/<int:invoice_id>/verify_payment", methods=["POST"])
def verify_payment(invoice_id):
  """Verify payment_meta and set status accordingly.
  - If full match: status -> paid, payment_verified=1
  - Else: status -> payment_pending, payment_verified=0
  """
  from flask import jsonify

  conn = get_db()
  invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not invoice:
    conn.close()
    abort(404)
  # Load meta
  meta = None
  if request.is_json:
    payload = request.get_json(silent=True) or {}
    meta = payload.get("payment_meta") or payload
  if not meta:
    try:
      meta = json.loads(invoice["payment_meta"]) if invoice["payment_meta"] else None
    except Exception:
      meta = None
  # value if not available 
  if not meta or not meta.get("deposit"):
    conn.close()
    if request.is_json:
      return (
        jsonify({"success": False, "error": "Payment  enter."}),
        400,
      )
    flash(
      "Payment  enter. 'LLMto ' Manual Input .",
      "error",
    )
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))

  try:
    total_minor_val = row_get(invoice, "total_minor", default=None)
    stored_total_minor = int(total_minor_val) if total_minor_val is not None else 0
  except Exception:
    stored_total_minor = 0

  needs_recalc = stored_total_minor <= 0
  if not needs_recalc:
    try:
      has_foreign = conn.execute(
        """
        SELECT 1
        FROM line_items
        WHERE invoice_id=?
         AND item_type='foreign'
         AND (is_estimated IS NULL OR is_estimated=0)
        LIMIT 1
        """,
        (invoice_id,),
      ).fetchone()
      if has_foreign:
        needs_recalc = True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices_payments.verify_payment.has_foreign",
        log_key="billing_invoices.invoices_payments.verify_payment.has_foreign",
        log_window_seconds=300,
      )

  if needs_recalc:
    try:
      items_rows = conn.execute(
        "SELECT * FROM line_items WHERE invoice_id=?",
        (invoice_id,),
      ).fetchall()
    except Exception:
      items_rows = []

    items_data = []
    for r in items_rows or []:
      try:
        items_data.append(row_to_dict(r))
      except Exception:
        try:
          items_data.append(
            {
              "qty": r[0],
              "unit_price": r[1],
            }
          )
        except Exception:
          continue

    try:
      currency = invoice["currency"] or "USD"
    except Exception:
      currency = "USD"
    try:
      vat_rate = Decimal(str(invoice["vat_rate"] or 0))
    except Exception:
      vat_rate = Decimal("0")

    try:
      subtotal, tax, total = compute_totals(items_data, vat_rate)
      subtotal_minor, tax_minor, total_minor = compute_totals_minor(
        items_data, vat_rate, currency
      )
    except Exception:
      subtotal = tax = total = None
      subtotal_minor = tax_minor = total_minor = None

    if total_minor is not None and int(total_minor or 0) != int(stored_total_minor or 0):
      try:
        conn.execute(
          "UPDATE invoices SET subtotal=?, tax=?, total=?, subtotal_minor=?, tax_minor=?, total_minor=? WHERE id=?",
          (
            float(subtotal or 0),
            float(tax or 0),
            float(total or 0),
            int(subtotal_minor or 0),
            int(tax_minor or 0),
            int(total_minor or 0),
            invoice_id,
          ),
        )
        conn.commit()
        invoice = conn.execute(
          "SELECT * FROM invoices WHERE id=?",
          (invoice_id,),
        ).fetchone()
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices_payments.verify_payment.recalc_totals",
          log_key="billing_invoices.invoices_payments.verify_payment.recalc_totals",
          log_window_seconds=300,
        )

  try:
    inv_currency = (invoice["currency"] or "USD").upper()
  except Exception:
    inv_currency = "USD"
  if not meta.get("currency") or not str(meta.get("currency") or "").strip():
    meta["currency"] = inv_currency

  if inv_currency in {"USD", "USD"}:
    ok, reason = _verify_local_payment_meta(invoice, meta)
  else:
    ok, reason = _verify_fx_payment_meta(invoice, meta)
  new_status = "paid" if ok else "payment_pending"
  target_payment = "paid" if ok else "pending"
  current_payment = _resolve_payment_status(invoice)
  if not _transition_allowed(current_payment, target_payment, _PAYMENT_TRANSITIONS, "unpaid"):
    conn.close()
    return _reject_transition(
      " Payment status .",
      invoice_id=invoice_id,
      status_code=409,
    )

  #  meta  Save
  if not ok:
    meta["verification_error"] = reason
    meta["verification_failed_at"] = datetime.now().isoformat()
  else:
    # success  
    meta.pop("verification_error", None)
    meta.pop("verification_failed_at", None)

  # Existing billing status check for tax documentation or payment receipt.
  billing_status = row_get(invoice, "billing_status", default=None)

  # Status  quarter
  if billing_status in ("tax_issued", "cash_issued", "processed"):
    # Billing status and payment status change (status, billing_status)
    # payment_verified=1 payment_status='paid' 
    conn.execute(
      "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        "paid" if ok else "pending",
        1 if ok else 0,
        json.dumps(meta, ensure_ascii=False),
        invoice_id,
      ),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status=billing_status,
      payment_status=("paid" if ok else "pending"),
    )
  else:
    #  (Draft/Issued ) Status 'sent' Change (Payment Issued Status )
    # payment_status peopleto Change, billing_status 'sent' 
    conn.execute(
      "UPDATE invoices SET billing_status='sent', payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        "paid" if ok else "pending",
        1 if ok else 0,
        json.dumps(meta, ensure_ascii=False),
        invoice_id,
      ),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status="sent",
      payment_status=("paid" if ok else "pending"),
    )
  conn.commit()
  conn.close()
  # 
  from ..auth import log_audit

  log_audit(
    "invoice.payment.verify",
    "invoice",
    invoice_id,
    f'{{"ok": {str(ok).lower()}, "reason": "{reason}"}}',
  )
  if request.is_json:
    return (
      jsonify({"success": True, "ok": ok, "reason": reason, "status": new_status}),
      200,
    )
  if ok:
    flash("Payment verification complete: invoice marked paid.", "success")
  else:
    flash(f"Payment match failed: {reason}. Invoice remains payment pending.", "error")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/force_paid", methods=["POST"])
def force_paid(invoice_id):
  """Admin/Staff force mark as paid without verification."""
  if not is_invoice_manager(current_user):
    abort(403, "Invoice Permissions User Payment Process exists.")
  conn = get_db()

  # Existing status check for tax documentation or payment receipt.
  row = conn.execute(
    "SELECT billing_status, payment_status FROM invoices WHERE id=?", (invoice_id,)
  ).fetchone()
  if not row:
    conn.close()
    abort(404)

  billing_status = row["billing_status"]
  current_payment = _resolve_payment_status(row)
  if not _transition_allowed(current_payment, "paid", _PAYMENT_TRANSITIONS, "unpaid"):
    conn.close()
    return _reject_transition(
      " Payment status .",
      invoice_id=invoice_id,
      status_code=409,
    )

  if billing_status in ("tax_issued", "cash_issued", "processed"):
    # Billing status and payment status change (status, billing_status)
    conn.execute(
      "UPDATE invoices SET payment_status='paid', payment_verified=0 WHERE id=?",
      (invoice_id,),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status=billing_status,
      payment_status="paid",
    )
  else:
    #  (Draft/Issued ) Status 'paid' Change (Legacy status='paid' billing='sent' )
    # payment_status peopleto Change
    conn.execute(
      "UPDATE invoices SET billing_status='sent', payment_status='paid', payment_verified=0 WHERE id=?",
      (invoice_id,),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status="sent",
      payment_status="paid",
    )

  conn.commit()
  conn.close()
  # 
  from ..auth import log_audit

  log_audit("invoice.payment.force_paid", "invoice", invoice_id)
  wants_json = request.is_json or (
    request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json"
    and request.accept_mimetypes["application/json"] >= request.accept_mimetypes["text/html"]
  )
  if wants_json:
    return (
      jsonify(
        {
          "success": True,
          "status": "paid",
          "payment_status": "paid",
          "payment_verified": 0,
        }
      ),
      200,
    )
  flash("⚠️ Administrator Paid Process.", "warning")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/unverify_payment", methods=["POST"])
def unverify_payment(invoice_id):
  """Reset payment verification status to allow re-editing."""
  if not is_invoice_manager(current_user):
    abort(403, "Invoice Permissions User  exists.")

  conn = get_db()
  invoice = conn.execute(
    "SELECT id, status, billing_status, currency, payment_verified, payment_status FROM invoices WHERE id=?",
    (invoice_id,),
  ).fetchone()
  if not invoice:
    conn.close()
    abort(404)
  current_payment = _resolve_payment_status(invoice)

  # Paid Status Status 
  # status='tax_issued'  payment_status='paid'  
  is_paid = (invoice["status"] == "paid") or (
    (invoice["payment_status"] or "").strip().lower() == "paid"
  )
  is_verified = invoice["payment_verified"] == 1

  # Tax-recorded status requires a paid or verified payment.
  if not (is_paid or is_verified):
    conn.close()
    flash("⚠️ Paid  Status Invoice  exists.", "error")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))

  if not _transition_allowed(current_payment, "pending", _PAYMENT_TRANSITIONS, "unpaid"):
    conn.close()
    return _reject_transition(
      " Payment status .",
      invoice_id=invoice_id,
      status_code=409,
    )

  # Billing status check (tax documentation/payment receipt recorded).
  billing_status = row_get(invoice, "billing_status", default=None)

  if billing_status in ("tax_issued", "cash_issued", "processed"):
    # Billing status and payment status change
    conn.execute(
      "UPDATE invoices SET payment_status='pending', payment_verified=0 WHERE id=?",
      (invoice_id,),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status=billing_status,
      payment_status="pending",
    )
    msg = "Payment verification was reopened while preserving the tax documentation status."
  else:
    # Reset to issued billing status when reopening payment verification.
    conn.execute(
      "UPDATE invoices SET billing_status='sent', payment_status='pending', payment_verified=0 WHERE id=?",
      (invoice_id,),
    )
    _sync_legacy_status(
      conn,
      invoice_id,
      billing_status="sent",
      payment_status="pending",
    )
    msg = "Payment verification was reopened and payment details can be edited."

  conn.commit()
  conn.close()

  # 
  from ..auth import log_audit

  log_audit("invoice.payment.unverify", "invoice", invoice_id)
  flash(msg, "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
