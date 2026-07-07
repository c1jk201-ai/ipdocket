from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from flask import abort, flash, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from app.services.billing.utils import compute_totals, compute_totals_minor, to_minor
from app.utils.error_logging import report_swallowed_exception

from ..auth import get_current_user, log_audit
from ..db import (
  _get_column_names,
  build_client_deposit_audit_meta,
  cancel_uncanceled_deposit_applies_for_invoice,
  get_all_business_profiles,
  get_business_profile,
  get_client_deposit_balance_minor,
  insert_client_deposit_ledger_entry,
  row_get,
  row_to_dict,
  snapshot_of_profile,
)
from ..settlement import is_default_settlement_split
from .invoice_creation_service import (
  _allowed_settlement_bp_ids,
  _build_invoice_form_toggle_url,
  _clean_client_name_en,
  _collect_submitted_items,
  _insert_client_record,
  _normalize_items_for_save,
  _stored_outgoing_mode,
  _submitted_outgoing_mode,
  _update_existing_client_fields,
)


@dataclass(frozen=True)
class InvoiceEditHooks:
  table_exists: Callable[..., bool]
  ensure_invoice_revision_for_print: Callable[..., tuple[int, bool, str] | None]
  safe_int: Callable[..., int | None]
  parse_settlement_splits: Callable[..., tuple[list[dict], str | None, str | None]]
  normalize_invoice_date: Callable[[Any], str | None]
  parse_amount_to_minor: Callable[[str, str], int]
  compute_billing_payment_from_status: Callable[[str, int], tuple[str, str]]
  derive_legacy_status_from_split: Callable[[str, str], str]
  sync_legacy_status: Callable[..., str]


@dataclass
class InvoiceEditPageState:
  conn: Any
  invoice_id: int
  invoice: dict[str, Any]
  clients: list[Any]
  all_profiles: list[dict]
  templates: list[Any]
  items: list[Any]
  outgoing_mode: bool
  business_profile: dict | None


def _default_dates() -> tuple[str, str]:
  today = date.today().isoformat()
  next_month = (date.today() + timedelta(days=30)).isoformat()
  return today, next_month


def load_invoice_edit_page_state(conn, invoice_id: int) -> InvoiceEditPageState:
  all_profiles = get_all_business_profiles()
  templates = conn.execute("SELECT * FROM invoice_templates ORDER BY name").fetchall()
  invoice_row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
  if not invoice_row:
    raise LookupError(f"invoice {invoice_id} not found")

  invoice = row_to_dict(invoice_row)
  outgoing_mode = _stored_outgoing_mode(invoice)

  business_profile = get_business_profile(invoice["business_profile_id"])
  raw_client_id = invoice.get("client_id")
  client_id = None
  try:
    if raw_client_id is not None:
      client_id = int(raw_client_id)
  except Exception:
    client_id = None

  if client_id:
    clients = conn.execute(
      "SELECT id, name, address, phone, manager "
      "FROM clients WHERE is_deleted IS NOT TRUE OR id=? ORDER BY name",
      (client_id,),
    ).fetchall()
  else:
    clients = conn.execute(
      "SELECT id, name, address, phone, manager "
      "FROM clients WHERE is_deleted IS NOT TRUE ORDER BY name"
    ).fetchall()

  items = conn.execute("SELECT * FROM line_items WHERE invoice_id=?", (invoice_id,)).fetchall()
  return InvoiceEditPageState(
    conn=conn,
    invoice_id=invoice_id,
    invoice=invoice,
    clients=clients,
    all_profiles=all_profiles,
    templates=templates,
    items=items,
    outgoing_mode=outgoing_mode,
    business_profile=business_profile,
  )


def _settlement_splits_from_invoice(invoice: dict[str, Any]) -> list[dict] | None:
  try:
    raw_meta = row_get(invoice, "settlement_meta", default=None)
  except Exception:
    raw_meta = None
  if not raw_meta:
    return None

  try:
    parsed = json.loads(raw_meta)
  except Exception:
    return None
  if not isinstance(parsed, list):
    return None
  if is_default_settlement_split(parsed, invoice.get("business_profile_id")):
    return None

  out: list[dict] = []
  for record in parsed:
    try:
      business_profile_id = int(record.get("business_profile_id"))
      percent = float(record.get("percent"))
    except Exception:
      continue
    if percent <= 0:
      continue
    out.append({"business_profile_id": business_profile_id, "percent": percent})
  return out or None


def render_invoice_edit_form(state: InvoiceEditPageState):
  today, next_month = _default_dates()
  items_dicts = [row_to_dict(item) for item in state.items]
  return render_template(
    "invoice_form.html",
    clients=state.clients,
    prefill_client_id="",
    all_profiles=state.all_profiles,
    bp=state.business_profile,
    business_profile=state.business_profile,
    today=today,
    next_month=next_month,
    invoice=state.invoice,
    items=items_dicts,
    templates=state.templates,
    outgoing_mode=state.outgoing_mode,
    toggle_mode_url=_build_invoice_form_toggle_url(
      outgoing_mode=state.outgoing_mode,
      invoice_id=state.invoice_id,
    ),
    settlement_splits=_settlement_splits_from_invoice(state.invoice),
  )


def _capture_baseline_revision(
  state: InvoiceEditPageState, hooks: InvoiceEditHooks, *, billing_status: str
) -> None:
  if billing_status == "draft":
    return
  try:
    if hooks.table_exists(state.conn, "invoice_revisions"):
      has_any = state.conn.execute(
        "SELECT 1 FROM invoice_revisions WHERE invoice_id=? LIMIT 1",
        (int(state.invoice_id),),
      ).fetchone()
      if not has_any:
        try:
          hooks.ensure_invoice_revision_for_print(
            state.conn,
            int(state.invoice_id),
            source="baseline_pre_edit",
          )
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.invoice_edit.baseline_revision.ensure",
            log_key="billing_invoices.invoice_edit.baseline_revision.ensure",
            log_window_seconds=300,
          )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_edit.baseline_revision",
      log_key="billing_invoices.invoice_edit.baseline_revision",
      log_window_seconds=300,
    )


def _resolve_or_create_edit_client(
  state: InvoiceEditPageState,
  form,
  *,
  client_id_str: str | None,
  safe_int: Callable[..., int | None],
) -> int:
  if client_id_str:
    client_id = safe_int(form.get("client_id"), None)
    if client_id is None or client_id <= 0:
      abort(400, "Invalid client ID.")
    return client_id

  new_client_name = form.get("new_client_name", "").strip()
  if not new_client_name:
    abort(400, "Select a client or enter a new client name.")
  new_client_email = (form.get("new_client_email") or "").strip()
  new_client_phone = (form.get("new_client_phone") or "").strip()
  new_client_address = (form.get("new_client_address") or "").strip()
  new_client_manager = (form.get("new_client_manager") or "").strip()
  new_client_notes = (form.get("new_client_notes") or "").strip()
  new_client_name_en = _clean_client_name_en(form.get("new_client_name_en"))

  existing = state.conn.execute(
    "SELECT id FROM clients WHERE name=? AND is_deleted IS NOT TRUE",
    (new_client_name,),
  ).fetchone()
  if existing:
    client_id = int(existing["id"])
    _update_existing_client_fields(
      state.conn,
      client_id,
      name=new_client_name,
      email=new_client_email,
      phone=new_client_phone,
      address=new_client_address,
      manager=new_client_manager,
      notes=new_client_notes,
      name_en=new_client_name_en,
    )
    return client_id

  return _insert_client_record(
    state.conn,
    name=new_client_name,
    email=new_client_email,
    phone=new_client_phone,
    address=new_client_address,
    manager=new_client_manager,
    notes=new_client_notes,
    name_en=new_client_name_en,
  )


def _update_invoice_line_items(
  conn, *, invoice_id: int, items_data: list[dict], currency: str
) -> None:
  conn.execute("DELETE FROM line_items WHERE invoice_id=?", (int(invoice_id),))

  try:
    cols = _get_column_names(conn, "line_items")
  except Exception:
    cols = set()
  has_fx_cols = all(
    column in cols
    for column in ("fx_currency", "fx_fee", "fx_gov", "fx_markup", "fx_rate_used")
  )
  has_phase_col = "phase" in cols
  has_estimated_col = "is_estimated" in cols

  for item in items_data:
    qty_minor = to_minor(item["qty"], currency)
    unit_price_minor = to_minor(item["unit_price"], currency)

    if has_phase_col and has_fx_cols and has_estimated_col:
      conn.execute(
        """INSERT INTO line_items
          (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
          item_type, discount, is_taxable, phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used, is_estimated)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
          int(invoice_id),
          item["description"],
          float(item["qty"]),
          float(item["unit_price"]),
          qty_minor,
          unit_price_minor,
          item["item_type"],
          float(item["discount"]),
          item["is_taxable"],
          item.get("phase") or "app",
          item.get("fx_currency"),
          float(item.get("fx_fee") or 0),
          float(item.get("fx_gov") or 0),
          float(item.get("fx_markup") or 0),
          (
            float(item.get("fx_rate_used"))
            if item.get("fx_rate_used") is not None
            else None
          ),
          item.get("is_estimated", 0),
        ),
      )
    elif has_phase_col and has_fx_cols:
      conn.execute(
        """INSERT INTO line_items
          (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
          item_type, discount, is_taxable, phase, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
          int(invoice_id),
          item["description"],
          float(item["qty"]),
          float(item["unit_price"]),
          qty_minor,
          unit_price_minor,
          item["item_type"],
          float(item["discount"]),
          item["is_taxable"],
          item.get("phase") or "app",
          item.get("fx_currency"),
          float(item.get("fx_fee") or 0),
          float(item.get("fx_gov") or 0),
          float(item.get("fx_markup") or 0),
          (
            float(item.get("fx_rate_used"))
            if item.get("fx_rate_used") is not None
            else None
          ),
        ),
      )
    elif has_phase_col and not has_fx_cols:
      conn.execute(
        """INSERT INTO line_items
          (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
          item_type, discount, is_taxable, phase)
          VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
          int(invoice_id),
          item["description"],
          float(item["qty"]),
          float(item["unit_price"]),
          qty_minor,
          unit_price_minor,
          item["item_type"],
          float(item["discount"]),
          item["is_taxable"],
          item.get("phase") or "app",
        ),
      )
    elif has_fx_cols:
      conn.execute(
        """INSERT INTO line_items
          (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
          item_type, discount, is_taxable, fx_currency, fx_fee, fx_gov, fx_markup, fx_rate_used)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
          int(invoice_id),
          item["description"],
          float(item["qty"]),
          float(item["unit_price"]),
          qty_minor,
          unit_price_minor,
          item["item_type"],
          float(item["discount"]),
          item["is_taxable"],
          item.get("fx_currency"),
          float(item.get("fx_fee") or 0),
          float(item.get("fx_gov") or 0),
          float(item.get("fx_markup") or 0),
          (
            float(item.get("fx_rate_used"))
            if item.get("fx_rate_used") is not None
            else None
          ),
        ),
      )
    else:
      conn.execute(
        """INSERT INTO line_items
          (invoice_id, description, qty, unit_price, qty_minor, unit_price_minor,
          item_type, discount, is_taxable)
          VALUES (?,?,?,?,?,?,?,?,?)""",
        (
          int(invoice_id),
          item["description"],
          float(item["qty"]),
          float(item["unit_price"]),
          qty_minor,
          unit_price_minor,
          item["item_type"],
          float(item["discount"]),
          item["is_taxable"],
        ),
      )


def _line_item_structure_key(item: dict[str, Any]) -> tuple[Any, ...]:
  def _text(value: Any) -> str:
    return str(value or "").strip()

  def _flag(value: Any) -> int:
    try:
      return 1 if int(value or 0) else 0
    except Exception:
      return 0

  return (
    _text(item.get("description")),
    _text(item.get("item_type") or "service"),
    _text(item.get("phase") or "app"),
    _text(item.get("fx_currency")).upper(),
    _flag(item.get("is_taxable")),
    _flag(item.get("is_estimated")),
  )


def _deposit_edit_line_structure_error(
  state: InvoiceEditPageState, items_data: list[dict[str, Any]]
) -> str | None:
  if (request.form.get("use_deposit") or "").strip() != "1":
    return None

  existing_items = [row_to_dict(item) for item in state.items or []]
  if len(existing_items) < 2:
    return None

  existing_keys = [_line_item_structure_key(item) for item in existing_items]
  submitted_keys = [_line_item_structure_key(item) for item in items_data or []]
  existing_unique_count = len(set(existing_keys))
  if existing_unique_count < 2:
    return None

  submitted_unique_count = len(set(submitted_keys))
  if len(submitted_keys) != len(existing_keys) or submitted_unique_count < existing_unique_count:
    return (
      "Saving was stopped because retainer application was submitted with a reduced line-item structure. "
      "Save line-item changes first, then apply the retainer from the invoice screen."
    )
  return None


def _apply_edit_deposit(
  state: InvoiceEditPageState,
  *,
  business_profile_id: int,
  client_id: int,
  currency: str,
  total_minor: int,
  deposit_amount_raw: str,
  deposit_memo_raw: str | None,
  number: str,
  billing_status: str,
  parse_amount_to_minor: Callable[[str, str], int],
  sync_legacy_status: Callable[..., str],
) -> None:
  if (request.form.get("use_deposit") or "").strip() != "1" or billing_status == "void":
    return

  applied_already = 0
  try:
    existing_applies = state.conn.execute(
      """
      SELECT a.amount_minor
      FROM client_deposit_ledger a
      LEFT JOIN client_deposit_ledger c ON c.related_entry_id = a.id AND c.entry_type='cancel_apply'
      WHERE a.related_invoice_id=? AND a.entry_type='apply' AND c.id IS NULL
      """,
      (int(state.invoice_id),),
    ).fetchall()
    for row in existing_applies:
      applied_already += abs(int(row["amount_minor"]))
  except Exception:
    applied_already = 0

  outstanding_minor = int(total_minor or 0) - applied_already
  if outstanding_minor <= 0:
    return

  if deposit_amount_raw:
    try:
      req_minor = abs(int(parse_amount_to_minor(deposit_amount_raw, currency)))
    except Exception:
      req_minor = outstanding_minor
  else:
    req_minor = outstanding_minor
  if req_minor > outstanding_minor:
    req_minor = outstanding_minor

  try:
    bal_bp = get_client_deposit_balance_minor(
      state.conn, business_profile_id, client_id, currency
    )
    bal_global = get_client_deposit_balance_minor(state.conn, None, client_id, currency)
    available = int(bal_bp) + int(bal_global)
    req_minor = min(int(req_minor), int(available))
    if req_minor <= 0:
      return

    use_bp = min(int(req_minor), int(bal_bp))
    use_global = int(req_minor) - int(use_bp)
    user = get_current_user()
    memo = deposit_memo_raw or f"invoice_edit:{number}"

    applied_total_this_time = 0
    if use_bp > 0:
      insert_client_deposit_ledger_entry(
        state.conn,
        business_profile_id,
        client_id,
        currency,
        -int(use_bp),
        "apply",
        memo=memo,
        related_invoice_id=int(state.invoice_id),
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      applied_total_this_time += use_bp

    if use_global > 0:
      insert_client_deposit_ledger_entry(
        state.conn,
        None,
        client_id,
        currency,
        -int(use_global),
        "apply",
        memo=memo,
        related_invoice_id=int(state.invoice_id),
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )
      applied_total_this_time += use_global

    total_applied_now = applied_already + applied_total_this_time
    new_outstanding = int(total_minor or 0) - total_applied_now
    new_payment_status = "paid" if new_outstanding <= 0 else "pending"
    new_payment_verified = 1 if new_outstanding <= 0 else 0

    meta = {}
    try:
      old_meta_row = state.conn.execute(
        "SELECT payment_meta FROM invoices WHERE id=?",
        (int(state.invoice_id),),
      ).fetchone()
      if old_meta_row and old_meta_row[0]:
        meta = json.loads(old_meta_row[0])
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoice_edit.load_payment_meta",
        log_key="billing_invoices.invoice_edit.load_payment_meta",
        log_window_seconds=300,
      )

    if new_payment_verified == 1:
      meta["verified_by_user_id"] = user["id"] if user else None
      meta["verified_by_username"] = user["username"] if user else None
      meta["verified_at"] = datetime.now().isoformat(timespec="seconds")
      meta["verified_via"] = "deposit_on_edit"

    state.conn.execute(
      "UPDATE invoices SET payment_status=?, payment_verified=?, payment_meta=? WHERE id=?",
      (
        new_payment_status,
        new_payment_verified,
        json.dumps(meta, ensure_ascii=False),
        int(state.invoice_id),
      ),
    )
    sync_legacy_status(
      state.conn,
      int(state.invoice_id),
      billing_status=billing_status,
      payment_status=new_payment_status,
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_edit.deposit",
      log_key="billing_invoices.invoice_edit.deposit",
      log_window_seconds=300,
    )


def _log_canceled_deposit_audits(invoice_id: int, canceled: list[dict]) -> None:
  for entry in canceled or []:
    try:
      audit_meta = build_client_deposit_audit_meta(
        entry_id=entry.get("cancel_entry_id"),
        business_profile_id=entry.get("business_profile_id"),
        client_id=entry.get("client_id"),
        currency=entry.get("currency"),
        amount_minor=entry.get("amount_minor"),
        entry_type="cancel_apply",
        memo=entry.get("memo"),
        related_invoice_id=int(invoice_id),
        related_entry_id=entry.get("apply_entry_id"),
        balance_before_minor=entry.get("balance_before_minor"),
        balance_after_minor=entry.get("balance_after_minor"),
      )
      log_audit("invoice.deposit.cancel_apply", "invoice", int(invoice_id), audit_meta)
    except Exception as log_exc:
      report_swallowed_exception(
        log_exc,
        context="billing_invoices.invoice_edit.void.cancel_deposit.audit",
        log_key="billing_invoices.invoice_edit.void.cancel_deposit.audit",
        log_window_seconds=300,
      )


def handle_invoice_edit_submission(
  state: InvoiceEditPageState,
  hooks: InvoiceEditHooks,
  *,
  billing_status: str,
) -> Any:
  form = request.form
  state.outgoing_mode = _submitted_outgoing_mode(form, default=state.outgoing_mode)
  _capture_baseline_revision(state, hooks, billing_status=billing_status)

  business_profile_id = hooks.safe_int(
    form.get("business_profile_id", state.invoice["business_profile_id"]),
    hooks.safe_int(state.invoice["business_profile_id"], 1, 1, None) or 1,
    1,
    None,
  )
  client_id_str = form.get("client_id")
  settlement_splits, settlement_meta, settlement_error = hooks.parse_settlement_splits(
    form,
    allowed_bp_ids=_allowed_settlement_bp_ids(state.all_profiles, hooks.safe_int),
  )
  if settlement_error:
    state.conn.close()
    flash(settlement_error, "error")
    return redirect(
      url_for(
        "billing_invoices.invoices.edit_invoice",
        invoice_id=state.invoice_id,
        outgoing="1" if state.outgoing_mode else "0",
      )
    )

  client_id = _resolve_or_create_edit_client(
    state,
    form,
    client_id_str=client_id_str,
    safe_int=hooks.safe_int,
  )
  number = (form.get("number") or "").strip() or state.invoice["number"]
  internal_reference = (form.get("internal_reference") or "").strip()
  issue_date = hooks.normalize_invoice_date(form.get("issue_date"))
  due_date = hooks.normalize_invoice_date(form.get("due_date"))
  status_code = form.get("status") or state.invoice["status"]
  notes = form.get("notes")
  invoice_language = (
    form.get("invoice_language") or row_get(state.invoice, "language", default=None) or "en"
  )
  deposit_amount_raw = (form.get("deposit_amount") or "").strip()
  deposit_memo_raw = (form.get("deposit_memo") or "").strip() or None

  submitted_items, foreign_vat_bases = _collect_submitted_items(form)
  items_data = _normalize_items_for_save(submitted_items, foreign_vat_bases)
  structure_error = _deposit_edit_line_structure_error(state, items_data)
  if structure_error:
    state.conn.close()
    flash(structure_error, "error")
    return redirect(
      url_for(
        "billing_invoices.invoices.edit_invoice",
        invoice_id=state.invoice_id,
        outgoing="1" if state.outgoing_mode else "0",
      )
    )

  bp_row = get_business_profile(business_profile_id)
  currency = bp_row["currency"] or "USD"
  vat_rate = Decimal(bp_row["vat_rate"])
  subtotal, tax, total = compute_totals(items_data, vat_rate)
  subtotal_minor, tax_minor, total_minor = compute_totals_minor(items_data, vat_rate, currency)

  billing_status_next, payment_status_next = hooks.compute_billing_payment_from_status(
    status_code, (state.invoice["payment_verified"] if state.invoice else 0)
  )
  if billing_status_next in {"tax_issued", "cash_issued", "processed"}:
    state.conn.close()
    flash(
      "Tax-recorded status can only be changed from the tax documentation confirmation screen.",
      "error",
    )
    return redirect(url_for("billing_invoices.invoices.tax_issue"))
  legacy_status = hooks.derive_legacy_status_from_split(billing_status_next, payment_status_next)

  canceled = []
  try:
    if not state.conn.in_transaction:
      state.conn.execute("BEGIN IMMEDIATE")

    if billing_status_next == "void":
      user = None
      try:
        user = get_current_user()
      except Exception:
        user = None
      canceled = cancel_uncanceled_deposit_applies_for_invoice(
        state.conn,
        int(state.invoice_id),
        memo="auto_cancel_on_void",
        created_by=(user["id"] if user else None),
        begin_immediate=False,
        commit_if_started=False,
      )

    try:
      invoice_cols = _get_column_names(state.conn, "invoices")
    except Exception:
      invoice_cols = set()

    update_assignments = [
      "client_id=?",
      "business_profile_id=?",
      "number=?",
      "internal_reference=?",
      "issue_date=?",
      "due_date=?",
      "status=?",
      "billing_status=?",
      "payment_status=?",
    ]
    update_params: list[Any] = [
      client_id,
      business_profile_id,
      number,
      internal_reference,
      issue_date,
      due_date,
      legacy_status,
      billing_status_next,
      payment_status_next,
    ]
    if billing_status_next == "void":
      update_assignments.extend(["payment_verified=0", "payment_meta=NULL"])
    current_billing_status = (
      str(
        (state.invoice or {}).get("billing_status")
        or (state.invoice or {}).get("status")
        or ""
      )
      .strip()
      .lower()
    )
    if current_billing_status in {"tax_issued", "cash_issued", "processed"}:
      if "tax_issued_at" in invoice_cols:
        update_assignments.append("tax_issued_at=NULL")
      if "tax_issue_type" in invoice_cols:
        update_assignments.append("tax_issue_type=NULL")
      if "tax_issue_source" in invoice_cols:
        update_assignments.append("tax_issue_source=NULL")
      if "tax_issue_note" in invoice_cols:
        update_assignments.append("tax_issue_note=NULL")
    update_assignments.extend(
      [
        "notes=?",
        "subtotal=?",
        "tax=?",
        "total=?",
        "subtotal_minor=?",
        "tax_minor=?",
        "total_minor=?",
        "settlement_meta=?",
      ]
    )
    update_params.extend(
      [
        notes,
        float(subtotal),
        float(tax),
        float(total),
        subtotal_minor,
        tax_minor,
        total_minor,
        settlement_meta,
      ]
    )
    if "language" in invoice_cols:
      update_assignments.append("language=?")
      update_params.append(invoice_language)
    update_assignments.extend(["business_snapshot=?", "currency=?", "vat_rate=?"])
    update_params.extend(
      [
        snapshot_of_profile(bp_row),
        currency,
        float(vat_rate),
        state.invoice_id,
      ]
    )
    state.conn.execute(
      f"UPDATE invoices SET {', '.join(update_assignments)} WHERE id=?",
      tuple(update_params),
    )

    _update_invoice_line_items(
      state.conn,
      invoice_id=state.invoice_id,
      items_data=items_data,
      currency=currency,
    )

    try:
      invoice_cols = _get_column_names(state.conn, "invoices")
    except Exception:
      invoice_cols = set()
    if "is_outgoing" in invoice_cols:
      state.conn.execute(
        "UPDATE invoices SET is_outgoing=? WHERE id=?",
        (1 if state.outgoing_mode else 0, int(state.invoice_id)),
      )

    _apply_edit_deposit(
      state,
      business_profile_id=business_profile_id,
      client_id=client_id,
      currency=currency,
      total_minor=total_minor,
      deposit_amount_raw=deposit_amount_raw,
      deposit_memo_raw=deposit_memo_raw,
      number=number,
      billing_status=billing_status_next,
      parse_amount_to_minor=hooks.parse_amount_to_minor,
      sync_legacy_status=hooks.sync_legacy_status,
    )

    state.conn.commit()
  except HTTPException:
    try:
      state.conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_edit.rollback.http",
        log_key="billing_invoices.invoice_edit.rollback.http",
        log_window_seconds=300,
      )
    state.conn.close()
    raise
  except Exception as exc:
    try:
      state.conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_edit.rollback",
        log_key="billing_invoices.invoice_edit.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_edit",
      log_key="billing_invoices.invoice_edit",
      log_window_seconds=300,
    )
    state.conn.close()
    abort(400, "An error occurred while saving data.")

  state.conn.close()
  _log_canceled_deposit_audits(state.invoice_id, canceled)
  log_audit(
    "invoice.update",
    "invoice",
    int(state.invoice_id),
    f'{{"number": "{number}", "client_id": {client_id}, "total": {float(total)}, "currency": "{currency}"}}',
  )
  return redirect(
    url_for(
      "billing_invoices.invoices.view_invoice",
      invoice_id=state.invoice_id,
      lang=invoice_language,
      outgoing="1" if state.outgoing_mode else "0",
    )
  )
