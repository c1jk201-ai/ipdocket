from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from flask import (
  Blueprint,
  abort,
  current_app,
  flash,
  jsonify,
  redirect,
  render_template,
  request,
  url_for,
)

from app.services.billing.tax_issue_types import enrich_invoice_tax_issue_fields
from app.services.billing.utils import (
  d,
  is_compact_query,
  sql_ci_contains_any,
  to_compact,
  to_minor,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.url_helpers import safe_referrer_path

from ..auth import get_current_user, log_audit, role_required
from ..db import (
  _actual_table_name,
  _get_column_names,
  _table_exists,
  get_all_business_profiles,
  get_business_profile,
  get_client_deposit_balance_minor,
  get_db,
  get_fx_rates_cache,
  row_get,
  row_to_dict,
  safe_json_parse,
  set_fx_rates_cache,
)
from ..exchange import fetch_sample_rates, fetch_sample_rates_all
from ..repos.invoice_case_repo import resolve_matter_identifier
from ..settlement import is_default_settlement_split
from ..services.invoice_creation_service import (
  InvoiceCreateHooks,
  handle_invoice_create_submission,
  load_invoice_create_page_state,
  render_invoice_create_form,
  _coerce_outgoing_mode,
  _stored_outgoing_mode,
)
from ..services.invoice_deletion_service import (
  InvoiceDeleteBlockedError,
  InvoiceDeleteExecutionError,
  InvoiceDeleteHooks,
  InvoiceDeleteNotFoundError,
  delete_invoices,
  log_invoice_delete_cancel_audits,
  record_single_invoice_delete_operation,
)
from ..services.invoice_edit_service import (
  InvoiceEditHooks,
  handle_invoice_edit_submission,
  load_invoice_edit_page_state,
  render_invoice_edit_form,
)
from ..services.remittance_proof_service import (
  FOREIGN_REMITTANCE_PROOF_ROLE,
  GENERAL_ATTACHMENT_ROLE,
  ensure_invoice_attachment_role_schema,
  foreign_remittance_required_message,
  invoice_has_foreign_remittance_proof,
  invoice_requires_foreign_remittance_proof,
)

try:
  from client_sync_sqlite import sync_clients_bidirectional
except Exception:
  sync_clients_bidirectional = None

bp = Blueprint("invoices", __name__)


_LATIN_TEXT_RE = re.compile(r"[A-Za-z]")
_NON_ASCII_TEXT_RE = re.compile(r"[^\x00-\x7F]")
_CLIENT_EN_NAME_KEYS = (
  "name_en",
  "english_name",
  "client_name_en",
  "invoice_name_en",
  "billing_name_en",
)
_CLIENT_EN_ADDRESS_KEYS = (
  "address_en",
  "english_address",
  "client_address_en",
  "invoice_address_en",
  "billing_address_en",
  "mailing_address_en",
  "applicant_address_en",
  "tax_address_en",
)
_CLIENT_ADDRESS_KEYS_WITH_POSSIBLE_EN = (
  "address",
  "applicant_address",
  "tax_address",
  "mail_recv_address",
  "other_address",
)


def _not_deleted_sql(column_expr: str) -> str:
  """Return a raw-SQL soft-delete predicate that works for bool/int/text DB values."""
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def _clean_invoice_text(value) -> str:
  if value is None:
    return ""
  return str(value).strip()


def _english_text_candidate(value) -> str:
  text = _clean_invoice_text(value)
  if not text or not _LATIN_TEXT_RE.search(text):
    return ""
  if not _NON_ASCII_TEXT_RE.search(text):
    return text

  # Common storage shape: localized line + English line in one address field.
  latin_lines = [
    line.strip()
    for line in re.split(r"[\r\n]+", text)
    if _LATIN_TEXT_RE.search(line) and not _NON_ASCII_TEXT_RE.search(line)
  ]
  if latin_lines:
    return "\n".join(latin_lines).strip()

  # Common name shape: "people (English Name)" or "people / English Name".
  for match in re.finditer(r"\(([^()]*)\)", text):
    candidate = match.group(1).strip()
    if _LATIN_TEXT_RE.search(candidate) and not _NON_ASCII_TEXT_RE.search(candidate):
      return candidate

  for segment in re.split(r"\s*(?:/|\||;)\s*", text):
    candidate = segment.strip()
    if _LATIN_TEXT_RE.search(candidate) and not _NON_ASCII_TEXT_RE.search(candidate):
      return candidate

  return ""


def _invoice_client_extra(invoice: dict) -> dict:
  raw = invoice.get("client_extra") if isinstance(invoice, dict) else None
  if isinstance(raw, dict):
    return raw
  try:
    parsed = safe_json_parse(raw, {}) or {}
  except Exception:
    parsed = {}
  return parsed if isinstance(parsed, dict) else {}


def _first_extra_text(extra: dict, keys: tuple[str, ...]) -> str:
  for key in keys:
    value = _clean_invoice_text(extra.get(key))
    if value:
      return value
  return ""


def _iter_extra_address_values(extra: dict):
  addresses = extra.get("addresses")
  if not isinstance(addresses, list):
    return
  for entry in addresses:
    label = ""
    value = ""
    if isinstance(entry, dict):
      label = _clean_invoice_text(
        entry.get("type")
        or entry.get("label")
        or entry.get("address_type")
        or entry.get("name")
      )
      value = _clean_invoice_text(
        entry.get("value") or entry.get("address") or entry.get("address_text")
      )
    elif isinstance(entry, (list, tuple)):
      if entry:
        label = _clean_invoice_text(entry[0])
      if len(entry) > 1:
        value = _clean_invoice_text(entry[1])
    if value:
      yield label, value


def _pick_client_name_en(extra: dict, fallback_name: str) -> str:
  explicit = _first_extra_text(extra, _CLIENT_EN_NAME_KEYS)
  return explicit or _english_text_candidate(fallback_name)


def _pick_client_address_en(extra: dict, fallback_address: str) -> str:
  explicit = _first_extra_text(extra, _CLIENT_EN_ADDRESS_KEYS)
  if explicit:
    return _english_text_candidate(explicit) or explicit

  for label, value in _iter_extra_address_values(extra) or ():
    label_lower = label.lower()
    if "" in label or "english" in label_lower or label_lower.endswith("_en"):
      candidate = _english_text_candidate(value) or value
      if candidate:
        return candidate

  for key in _CLIENT_ADDRESS_KEYS_WITH_POSSIBLE_EN:
    candidate = _english_text_candidate(extra.get(key))
    if candidate:
      return candidate

  return _english_text_candidate(fallback_address)


def _augment_invoice_client_language_fields(invoice: dict) -> dict:
  if not isinstance(invoice, dict):
    return invoice

  raw_name = _clean_invoice_text(invoice.get("client_name_source") or invoice.get("client_name"))
  raw_address = _clean_invoice_text(
    invoice.get("client_address_source") or invoice.get("client_address")
  )
  invoice["client_name_source"] = raw_name
  invoice["client_address_source"] = raw_address

  extra = _invoice_client_extra(invoice)
  if not _clean_invoice_text(invoice.get("client_name_en")):
    invoice["client_name_en"] = _pick_client_name_en(extra, raw_name)
  if not _clean_invoice_text(invoice.get("client_address_en")):
    invoice["client_address_en"] = _pick_client_address_en(extra, raw_address)
  return invoice


def _apply_invoice_client_language(invoice: dict, invoice_lang: str | None) -> dict:
  invoice = _augment_invoice_client_language_fields(invoice)
  if not isinstance(invoice, dict):
    return invoice

  lang = (invoice_lang or "").strip().lower()
  if lang == "en":
    invoice["client_name"] = _clean_invoice_text(
      invoice.get("client_name_en")
    ) or _clean_invoice_text(invoice.get("client_name_source"))
    invoice["client_address"] = _clean_invoice_text(
      invoice.get("client_address_en")
    ) or _clean_invoice_text(invoice.get("client_address_source"))
  else:
    invoice["client_name"] = _clean_invoice_text(invoice.get("client_name_source"))
    invoice["client_address"] = _clean_invoice_text(invoice.get("client_address_source"))
  return invoice


def _client_extra_select_expr(conn) -> str:
  try:
    if "extra" in _get_column_names(conn, "clients"):
      return "clients.extra as client_extra"
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.client_extra_select_expr",
      log_key="billing_invoices.invoices.client_extra_select_expr",
      log_window_seconds=300,
    )
  return "NULL as client_extra"


def _parse_invoice_audit_meta(meta_value):
  if meta_value is None:
    return None
  if isinstance(meta_value, dict):
    return meta_value
  if not isinstance(meta_value, str):
    return None
  for parser in (json.loads, ast.literal_eval):
    try:
      parsed = parser(meta_value)
    except Exception:
      continue
    if isinstance(parsed, dict):
      return parsed
  return None


def _meta_as_list(value) -> list[object]:
  if value is None:
    return []
  if isinstance(value, (list, tuple, set)):
    return list(value)
  return [value]


def _matches_bulk_status_change_log(
  meta: dict | None,
  *,
  invoice_id: int | None,
  invoice_number: str | None,
  mode: str | None = None,
) -> bool:
  if not isinstance(meta, dict):
    return False

  if mode:
    meta_mode = str(meta.get("mode") or "").strip().lower()
    if meta_mode != str(mode).strip().lower():
      return False

  if invoice_id is not None:
    for raw_id in _meta_as_list(meta.get("invoice_ids")):
      if _safe_int(raw_id, None) == invoice_id:
        return True
    if _safe_int(meta.get("invoice_id"), None) == invoice_id:
      return True

  normalized_number = str(invoice_number or "").strip()
  if normalized_number:
    for raw_number in _meta_as_list(meta.get("invoice_numbers")):
      if str(raw_number or "").strip() == normalized_number:
        return True
    if str(meta.get("invoice_number") or "").strip() == normalized_number:
      return True

  return False


def _audit_log_sort_key(log_row: dict) -> tuple[str, int]:
  return (str(log_row.get("created_at") or ""), _safe_int(log_row.get("id"), 0) or 0)


def _fetch_invoice_audit_rows(
  conn,
  *,
  invoice_id: int,
  invoice_number: str | None,
  direct_actions: tuple[str, ...],
  bulk_mode: str | None = None,
  limit: int | None = None,
) -> list[dict]:
  logs: list[dict] = []
  actions = tuple(action for action in (direct_actions or ()) if action)

  if actions:
    placeholders = ",".join("?" for _ in actions)
    direct_rows = conn.execute(
      f"""
      SELECT a.*, u.username
      FROM audit_log a
      LEFT JOIN users u ON u.id = a.user_id
      WHERE a.target_type='invoice' AND a.target_id=? AND a.action IN ({placeholders})
      ORDER BY a.created_at DESC, a.id DESC
      """,
      [invoice_id, *actions],
    ).fetchall()
    logs.extend(row_to_dict(row) for row in (direct_rows or []))

  bulk_filters = ["a.action='invoice.bulk_status_change'"]
  bulk_params: list[object] = []
  if bulk_mode:
    bulk_filters.append("a.meta LIKE ?")
    bulk_params.append(f'%"mode": "{bulk_mode}"%')

  bulk_rows = conn.execute(
    f"""
    SELECT a.*, u.username
    FROM audit_log a
    LEFT JOIN users u ON u.id = a.user_id
    WHERE {" AND ".join(bulk_filters)}
    ORDER BY a.created_at DESC, a.id DESC
    """,
    bulk_params,
  ).fetchall()
  for row in bulk_rows or []:
    data = row_to_dict(row)
    meta = _parse_invoice_audit_meta(data.get("meta"))
    if not _matches_bulk_status_change_log(
      meta,
      invoice_id=invoice_id,
      invoice_number=invoice_number,
      mode=bulk_mode,
    ):
      continue
    logs.append(data)

  logs.sort(key=_audit_log_sort_key, reverse=True)
  if limit is not None:
    return logs[:limit]
  return logs


INVOICE_PAYMENT_LOG_ACTIONS = (
  "invoice.payment.verify",
  "invoice.payment_meta.save",
  "invoice.payment.force_paid",
  "invoice.payment.unverify",
  "invoice.mark_paid",
  "invoice.deposit.apply",
  "invoice.deposit.cancel_apply",
)

INVOICE_BILLING_LOG_ACTIONS = (
  "invoice.status_change",
  "invoice.tax_issued",
  "invoice.publish",
  "invoice.create",
)


def _build_invoice_audit_pretty_meta(action: str, meta_str: str) -> str:
  meta = _parse_invoice_audit_meta(meta_str)
  a = (action or "").strip()
  if a == "invoice.status_change":
    if isinstance(meta, dict):
      old_s = meta.get("old_status")
      new_s = meta.get("new_status")
      if old_s or new_s:
        return f"Status: {old_s or '-'} -> {new_s or '-'}"
    return ""
  if a == "invoice.publish":
    if isinstance(meta, dict) and meta.get("to_status"):
      return f"Issued: {meta.get('to_status')}"
    return "Issued"
  if a == "invoice.tax_issued":
    return "Tax documentation recorded"
  if a == "invoice.create":
    if isinstance(meta, dict):
      u = meta.get("created_by_username")
      return "Draft" + (f" - {u}" if u else "")
    return "Draft"
  if a == "invoice.payment.verify":
    if isinstance(meta, dict):
      return " success" if meta.get("ok") else f" : {meta.get('reason', '')}"
    return "Payment verification"
  if a == "invoice.payment_meta.save":
    return "Payment Save"
  if a == "invoice.payment.force_paid":
    return "Administrator Payment"
  if a == "invoice.payment.unverify":
    return " (Payment pending)"
  if a == "invoice.mark_paid":
    return "Payment"
  if a == "invoice.deposit.apply":
    return "Retainer application"
  if a == "invoice.deposit.cancel_apply":
    return "Retainer applicationCancel"
  if a == "invoice.bulk_status_change":
    if isinstance(meta, dict):
      mode = meta.get("mode")
      new_s = meta.get("new_status")
      if mode and new_s:
        label = "Payment" if mode == "payment" else "Billing"
        return f"Bulk change - {label}: {new_s}"
    return "Bulk change"
  return meta_str or ""


def _enrich_invoice_audit_logs(rows: list[dict]) -> list[dict]:
  enriched: list[dict] = []
  for row in rows:
    data = row_to_dict(row)
    data["pretty_meta"] = _build_invoice_audit_pretty_meta(
      data.get("action"),
      data.get("meta"),
    )
    enriched.append(data)
  return enriched


def _outgoing_matter_sql(alias: str) -> str:
  return (
    "("
    f"UPPER(TRIM(COALESCE({alias}.right_group, ''))) "
    "IN ('OUT', 'OUTGOING', 'OUTBOUND', 'FOREIGN') "
    f"OR TRIM(COALESCE({alias}.right_group, '')) "
    "IN ('Foreign', 'Foreign', '', '')"
    ")"
  )


def _outgoing_invoice_filter_sql(invoice_alias: str = "invoices") -> str:
  matter_from_map = _outgoing_matter_sql("m_outgoing_filter")
  matter_from_primary = _outgoing_matter_sql("m_primary_outgoing_filter")
  matter_from_ref = _outgoing_matter_sql("m_ref_outgoing_filter")
  return f"""
    (
     COALESCE({invoice_alias}.is_outgoing, 0) = 1
     OR EXISTS (
      SELECT 1
       FROM external_invoice_case_map eicm_outgoing_filter
       JOIN matter m_outgoing_filter
        ON m_outgoing_filter.matter_id = eicm_outgoing_filter.matter_id
       WHERE eicm_outgoing_filter.external_invoice_id = {invoice_alias}.id
        AND {_not_deleted_sql("eicm_outgoing_filter.is_deleted")}
        AND {matter_from_map}
     )
     OR EXISTS (
      SELECT 1
       FROM matter m_primary_outgoing_filter
       WHERE m_primary_outgoing_filter.matter_id = {invoice_alias}.ipm_case_id
        AND {matter_from_primary}
     )
     OR EXISTS (
      SELECT 1
       FROM matter m_ref_outgoing_filter
       WHERE TRIM(COALESCE({invoice_alias}.ipm_case_ref, '')) <> ''
        AND UPPER(TRIM(COALESCE(m_ref_outgoing_filter.our_ref, '')))
          = UPPER(TRIM(COALESCE({invoice_alias}.ipm_case_ref, '')))
        AND {matter_from_ref}
     )
    )
  """


def _load_invoice_with_client(conn, invoice_id: int) -> dict | None:
  client_extra_expr = _client_extra_select_expr(conn)
  invoice = conn.execute(
    f"""SELECT invoices.*, clients.name as client_name, clients.email as client_email,
         clients.phone as client_phone, clients.address as client_address,
         clients.manager as client_manager, {client_extra_expr}
      FROM invoices JOIN clients ON clients.id=invoices.client_id
      WHERE invoices.id=?""",
    (invoice_id,),
  ).fetchone()
  if not invoice:
    return None
  invoice = row_to_dict(invoice)
  _augment_invoice_client_language_fields(invoice)
  try:
    bs, ps, st = _normalize_invoice_status_fields(invoice)
    invoice["billing_status"] = bs
    invoice["payment_status"] = ps
    invoice["status"] = st
    enrich_invoice_tax_issue_fields(invoice)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.view_invoice.normalize_status_fields",
      log_key="billing_invoices.invoices.view_invoice.normalize_status_fields",
      log_window_seconds=300,
    )
  return invoice


def _load_invoice_items(conn, invoice_id: int) -> list[dict]:
  rows = conn.execute(
    "SELECT * FROM line_items WHERE invoice_id=? ORDER BY id ASC",
    (invoice_id,),
  ).fetchall()
  return [row_to_dict(r) for r in (rows or [])]


def _load_invoice_attachments(conn, invoice_id: int) -> list[dict]:
  ensure_invoice_attachment_role_schema(conn)
  rows = conn.execute(
    """SELECT id, original_name, stored_name, content_type, size, COALESCE(role, 'general') AS role,
         uploaded_at, uploaded_by, first_page_text, analysis_meta
      FROM invoice_attachments
      WHERE invoice_id=?
      ORDER BY uploaded_at DESC""",
    (invoice_id,),
  ).fetchall()
  return [row_to_dict(r) for r in (rows or [])]


def _split_invoice_attachments_by_role(
  attachments: list[dict],
) -> tuple[list[dict], list[dict]]:
  general: list[dict] = []
  foreign_remittance: list[dict] = []
  for att in attachments or []:
    role = str(att.get("role") or GENERAL_ATTACHMENT_ROLE).strip().lower()
    if role == FOREIGN_REMITTANCE_PROOF_ROLE:
      foreign_remittance.append(att)
    else:
      general.append(att)
  return general, foreign_remittance


def _load_invoice_recent_logs(
  conn,
  *,
  invoice_id: int,
  invoice_number: str | None,
) -> tuple[list[dict], list[dict]]:
  payment_logs = _fetch_invoice_audit_rows(
    conn,
    invoice_id=invoice_id,
    invoice_number=invoice_number,
    direct_actions=INVOICE_PAYMENT_LOG_ACTIONS,
    bulk_mode="payment",
    limit=5,
  )
  billing_logs = _fetch_invoice_audit_rows(
    conn,
    invoice_id=invoice_id,
    invoice_number=invoice_number,
    direct_actions=INVOICE_BILLING_LOG_ACTIONS,
    bulk_mode="billing",
    limit=5,
  )
  return _enrich_invoice_audit_logs(payment_logs), _enrich_invoice_audit_logs(billing_logs)


def _build_invoice_settlement_details(invoice: dict) -> list[dict] | None:
  settlement_details = None
  try:
    raw_meta = row_get(invoice, "settlement_meta", default=None)
  except Exception:
    raw_meta = None
  if not raw_meta:
    return settlement_details

  try:
    parsed = safe_json_parse(raw_meta)
  except Exception:
    parsed = None
  if not isinstance(parsed, list):
    return settlement_details
  if is_default_settlement_split(parsed, row_get(invoice, "business_profile_id", default=None)):
    return settlement_details

  details = []
  try:
    total_amount = Decimal(str(invoice["total"] or 0))
  except Exception:
    total_amount = None
  try:
    currency_code = invoice["currency"] or "USD"
  except Exception:
    currency_code = "USD"

  for rec in parsed:
    try:
      bp_id = int(rec.get("business_profile_id"))
      pct_decimal = Decimal(str(rec.get("percent")))
      pct = float(pct_decimal)
    except Exception:
      continue
    if pct <= 0:
      continue
    bp_info = get_business_profile(bp_id)
    if not bp_info:
      continue
    amount = None
    if total_amount is not None:
      amount = total_amount * (pct_decimal / Decimal("100"))
    details.append(
      {
        "business_profile_id": bp_id,
        "name": bp_info.get("name"),
        "percent": pct,
        "amount": amount,
        "currency": currency_code,
        "bank_account": bp_info.get("bank_account"),
      }
    )

  if details:
    settlement_details = details
  return settlement_details


def _get_settlement_share_ratio(row: dict, target_bp: int) -> float:
  """
  Return the selected BP's normalized settlement share for display.

  If settlement_meta is absent or invalid, the issuing BP owns 100%.
  """
  try:
    target_bp = int(target_bp)
  except Exception:
    return 0.0

  meta_s = (
    row.get("settlement_meta") if isinstance(row, dict) else row_get(row, "settlement_meta")
  )
  if meta_s:
    parsed = None
    try:
      parsed = safe_json_parse(meta_s)
    except Exception:
      parsed = None
    if isinstance(parsed, list) and not is_default_settlement_split(
      parsed,
      row.get("business_profile_id") if isinstance(row, dict) else row_get(row, "business_profile_id"),
    ):
      pct_sum = 0.0
      target_pct = 0.0
      for rec in parsed:
        try:
          bpv = int(rec.get("business_profile_id"))
          pctv = float(rec.get("percent"))
        except Exception:
          continue
        if pctv <= 0:
          continue
        pct_sum += pctv
        if bpv == target_bp:
          target_pct += pctv
      if pct_sum > 0:
        return max(0.0, min(1.0, target_pct / pct_sum))

  try:
    issuing_bp = int(row.get("business_profile_id") or 0)
  except Exception:
    issuing_bp = 0
  return 1.0 if issuing_bp == target_bp else 0.0


def _build_invoice_deposit_context(conn, invoice_id: int, invoice: dict) -> dict[str, object]:
  deposit_balance_minor = None
  deposit_applied_minor = 0
  deposit_outstanding_minor = None
  apply_rows: list[dict] = []

  try:
    inv_client_id = int(invoice["client_id"])
  except Exception:
    inv_client_id = None
  try:
    inv_bp_id = int(invoice["business_profile_id"] or 1)
  except Exception:
    inv_bp_id = 1
  try:
    inv_currency = (invoice["currency"] or "USD").upper()
  except Exception:
    inv_currency = "USD"

  try:
    if inv_client_id is not None:
      bal_bp = get_client_deposit_balance_minor(conn, inv_bp_id, inv_client_id, inv_currency)
      bal_global = get_client_deposit_balance_minor(conn, None, inv_client_id, inv_currency)
      deposit_balance_minor = int(bal_bp) + int(bal_global)
  except Exception:
    deposit_balance_minor = None

  deposit_entries = []
  try:
    rows = conn.execute(
      """
      SELECT *
      FROM client_deposit_ledger
      WHERE related_invoice_id=?
       AND entry_type IN ('apply','cancel_apply')
      ORDER BY created_at DESC, id DESC
      """,
      (invoice_id,),
    ).fetchall()
    deposit_entries = [row_to_dict(r) for r in (rows or [])]
  except Exception:
    deposit_entries = []

  apply_map = {}
  canceled_apply_ids = set()
  for entry in deposit_entries:
    try:
      entry_type = (entry.get("entry_type") or "").strip().lower()
    except Exception:
      entry_type = ""
    if entry_type == "apply":
      entry_id = _safe_int(entry.get("id"), None)
      if entry_id is not None:
        apply_map[int(entry_id)] = entry
    if entry_type == "cancel_apply":
      related_id = _safe_int(entry.get("related_entry_id"), None)
      if related_id is not None:
        canceled_apply_ids.add(int(related_id))

  for apply_id, apply_row in apply_map.items():
    data = row_to_dict(apply_row)
    data["canceled"] = apply_id in canceled_apply_ids
    apply_rows.append(data)
  apply_rows.sort(key=lambda row: (row.get("created_at") or "", row.get("id") or 0), reverse=True)

  try:
    total_row = conn.execute(
      """
      SELECT COALESCE(SUM(amount_minor), 0) AS s
      FROM client_deposit_ledger
      WHERE related_invoice_id=?
       AND entry_type IN ('apply','cancel_apply')
      """,
      (invoice_id,),
    ).fetchone()
    sum_minor = int(row_get(total_row, "s", 0, 0) or 0)
    deposit_applied_minor = max(0, -sum_minor)
  except Exception:
    deposit_applied_minor = 0

  try:
    total_minor_val = row_get(invoice, "total_minor", default=None)
    inv_total_minor = (
      total_minor_val
      if total_minor_val is not None
      else to_minor(Decimal(str(invoice["total"] or 0)), inv_currency)
    )
    inv_total_minor = int(inv_total_minor)
    deposit_outstanding_minor = max(0, inv_total_minor - int(deposit_applied_minor or 0))
  except Exception:
    deposit_outstanding_minor = None

  return {
    "deposit_balance_minor": deposit_balance_minor,
    "deposit_apply_rows": apply_rows,
    "deposit_applied_minor": deposit_applied_minor,
    "deposit_outstanding_minor": deposit_outstanding_minor,
  }


def _is_blank_invoice_profile_value(value) -> bool:
  if value is None:
    return True
  if isinstance(value, str):
    return value.strip() == ""
  return False


def _resolve_invoice_business_profile(invoice: dict) -> dict:
  bp_row = None
  if invoice.get("business_snapshot"):
    try:
      bp_row = safe_json_parse(invoice["business_snapshot"])
    except Exception:
      bp_row = None
  if not isinstance(bp_row, dict):
    bp_row = None

  if not bp_row or _is_blank_invoice_profile_value(bp_row.get("name")):
    live_bp = get_business_profile(invoice["business_profile_id"])
    if bp_row and live_bp:
      for key in (
        "name",
        "address",
        "email",
        "phone",
        "tax_id",
        "bank_account",
        "logo_path",
      ):
        if _is_blank_invoice_profile_value(bp_row.get(key)):
          bp_row[key] = live_bp.get(key)
    else:
      bp_row = live_bp

  return row_to_dict(bp_row) if bp_row else {}


def _resolve_invoice_render_options(invoice: dict) -> tuple[str, bool]:
  saved_lang = None
  try:
    saved_lang = row_get(invoice, "language", default=None)
  except Exception:
    saved_lang = None
  if not saved_lang:
    saved_lang = None
  invoice_lang = str(request.args.get("lang") or saved_lang or "en").strip().lower()
  if invoice_lang in {"english", "eng"}:
    invoice_lang = "en"
  if invoice_lang != "en":
    invoice_lang = "en"

  outgoing_mode = _stored_outgoing_mode(invoice)
  return invoice_lang, outgoing_mode


def _load_invoice_case_links(conn, invoice_id: int, invoice: dict) -> list[dict]:
  case_links = []
  try:
    rows = conn.execute(
      f"""
      SELECT l.matter_id,
          COALESCE(m.our_ref, l.our_ref) AS our_ref,
          COALESCE(m.right_name, '') AS right_name
      FROM external_invoice_case_map l
      LEFT JOIN matter m ON m.matter_id = l.matter_id
      WHERE l.external_invoice_id=?
       AND {_not_deleted_sql("l.is_deleted")}
      ORDER BY COALESCE(m.our_ref, l.our_ref, l.matter_id) DESC, l.id ASC
      """,
      (invoice_id,),
    ).fetchall()
    case_links = [
      {
        "matter_id": row[0],
        "our_ref": row[1],
        "right_name": row[2],
      }
      for row in (rows or [])
    ]
  except Exception:
    case_links = []

  return case_links


def _load_invoice_internal_reference_suggestions(
  conn,
  *,
  invoice: dict,
  case_links: list[dict],
) -> list[dict[str, object]]:
  linked_matter_ids = {
    str((link or {}).get("matter_id") or "").strip() for link in (case_links or []) if link
  }
  raw_value = (
    invoice.get("internal_reference")
    or invoice.get("ipm_case_ref")
    or invoice.get("ipm_case_id")
  )
  try:
    return _build_internal_reference_case_suggestions(
      conn,
      raw_value=raw_value,
      linked_matter_ids=linked_matter_ids,
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.view_invoice.internal_reference_suggestions",
      log_key="billing_invoices.invoices.view_invoice.internal_reference_suggestions",
      log_window_seconds=300,
    )
  return []


def _load_invoice_revisions(conn, invoice_id: int) -> list[dict]:
  invoice_revisions = []
  try:
    if _table_exists(conn, "invoice_revisions"):
      rows = conn.execute(
        """
        SELECT r.revision_no, r.file_name, r.created_at, r.created_by, r.source,
            r.render_lang, r.render_outgoing, u.username AS created_by_username
        FROM invoice_revisions r
        LEFT JOIN users u ON u.id = r.created_by
        WHERE r.invoice_id=?
        ORDER BY r.revision_no DESC
        """,
        (invoice_id,),
      ).fetchall()
      invoice_revisions = [row_to_dict(r) for r in (rows or [])]
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.view_invoice.invoice_revisions",
      log_key="billing_invoices.invoices.view_invoice.invoice_revisions",
      log_window_seconds=300,
    )
    invoice_revisions = []
  return invoice_revisions


def _compute_billing_payment_from_status(status_code, payment_verified=0):
  """Map legacy status + payment_verified -> (billing_status, payment_status).
  Conservative mapping that does NOT assume tax_issued == paid.
  """
  try:
    s = (status_code or "draft").strip().lower()
  except Exception:
    s = "draft"
  # billing_status
  if s in ("draft", "sent", "void", "tax_issued", "cash_issued", "processed", "pre_overdue"):
    billing = s
  elif s == "payment_pending":
    billing = "sent"
  elif s == "paid":
    billing = "sent"
  else:
    billing = "draft"

  # payment_status
  if s == "paid":
    payment = "paid"
  elif s == "payment_pending" or s == "pre_overdue":
    payment = "pending"
  elif s == "void":
    payment = "none"
  else:
    payment = "unpaid"
  # If already verified, elevate to paid
  try:
    verified = int(payment_verified or 0)
  except (TypeError, ValueError):
    verified = 0
  if verified == 1 and billing != "void":
    payment = "paid"
  return billing, payment


_STATUS_TRANSITIONS = {
  "draft": {"draft", "sent", "void"},
  "sent": {
    "sent",
    "draft",
    "payment_pending",
    "pre_overdue",
    "paid",
    "tax_issued",
    "cash_issued",
    "processed",
    "void",
  },
  "payment_pending": {
    "payment_pending",
    "sent",
    "pre_overdue",
    "paid",
    "tax_issued",
    "cash_issued",
    "processed",
    "void",
  },
  "pre_overdue": {
    "pre_overdue",
    "sent",
    "payment_pending",
    "paid",
    "tax_issued",
    "cash_issued",
    "processed",
    "void",
  },
  "paid": {
    "paid",
    "sent",
    "payment_pending",
    "pre_overdue",
    "tax_issued",
    "cash_issued",
    "processed",
  },
  "tax_issued": {"tax_issued", "sent", "paid"},
  "cash_issued": {"cash_issued", "sent", "paid"},
  "processed": {"processed", "sent", "paid"},
  "void": {"void", "draft", "sent"},
}

_BILLING_TRANSITIONS = {
  "draft": {"draft", "sent", "void"},
  "sent": {"sent", "draft", "tax_issued", "cash_issued", "processed", "void", "pre_overdue"},
  # pre_overdue is a sent-like billing state; tax/cash/processed should still be reachable.
  "pre_overdue": {"pre_overdue", "sent", "tax_issued", "cash_issued", "processed", "void"},
  "tax_issued": {"tax_issued", "sent"},
  "cash_issued": {"cash_issued", "sent"},
  "processed": {"processed", "sent"},
  "void": {"void", "draft", "sent"},
}

_PAYMENT_TRANSITIONS = {
  "unpaid": {"unpaid", "pending", "paid"},
  "pending": {"pending", "unpaid", "paid"},
  "paid": {"paid", "pending", "unpaid"},
  "none": {"none", "unpaid", "pending"},
}


def _normalize_status(value, default: str) -> str:
  try:
    s = (value or "").strip().lower()
  except Exception:
    s = ""
  return s or default


def _is_payment_effectively_complete(row) -> bool:
  """Treat verified payments and manager-forced paid rows as complete for gates."""
  if not row:
    return False
  try:
    if int(row_get(row, "payment_verified", default=0) or 0) == 1:
      return True
  except (TypeError, ValueError):
    pass
  payment_status = _normalize_status(row_get(row, "payment_status", default=""), "")
  legacy_status = _normalize_status(row_get(row, "status", default=""), "")
  return payment_status in {"paid", "overpaid"} or legacy_status == "paid"


def _transition_allowed(old_status, new_status, transitions, default_old: str) -> bool:
  old_norm = _normalize_status(old_status, default_old)
  new_norm = _normalize_status(new_status, "")
  if not new_norm:
    return False
  return new_norm in transitions.get(old_norm, set())


def _reject_transition(message: str, *, invoice_id: int | None = None, status_code: int = 400):
  if request.is_json:
    return jsonify({"success": False, "error": message}), status_code
  flash(message, "error")
  if invoice_id is not None:
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
  return redirect(safe_referrer_path() or url_for("billing_invoices.invoices.list_invoices"))


def _coerce_split_status(billing: str, payment: str) -> tuple[str, str]:
  """
  Normalize and enforce minimal invariants for split status columns.
  - billing='void' <-> payment='none'
  - unknown values fall back conservatively
  """
  b = _normalize_status(billing, "draft")
  p = _normalize_status(payment, "unpaid")

  allowed_b = {"draft", "sent", "void", "tax_issued", "cash_issued", "processed", "pre_overdue"}
  allowed_p = {"unpaid", "pending", "paid", "none"}

  if b not in allowed_b:
    b = "draft"
  if p not in allowed_p:
    p = "unpaid"

  # void/not available coupling
  if b == "void" or p == "none":
    return "void", "none"

  return b, p


def _derive_legacy_status_from_split(billing: str, payment: str) -> str:
  """
  Backward-compatible legacy status string derived from split axes.
  This is only for UI / legacy code that still expects invoices.status.
  """
  b, p = _coerce_split_status(billing, payment)

  # These are billing-terminal or billing-specific legacy states
  if b in ("draft", "void", "tax_issued", "cash_issued", "processed", "pre_overdue"):
    return b

  # b == 'sent'
  if p == "paid":
    return "paid"
  if p == "pending":
    return "payment_pending"
  return "sent"


def _normalize_invoice_status_fields(row: dict) -> tuple[str, str, str]:
  """
  Resolve split statuses (fallback from legacy if missing), enforce invariants,
  and return (billing_status, payment_status, legacy_status).
  """
  bs = _resolve_billing_status(row)
  ps = _resolve_payment_status(row)

  # If payment_verified is set, prefer paid (except void)
  verified_raw = row_get(row, "payment_verified", default=0) or 0
  try:
    verified = int(verified_raw)
  except (TypeError, ValueError):
    verified = 0
  if verified == 1 and bs != "void":
    ps = "paid"

  bs, ps = _coerce_split_status(bs, ps)
  st = _derive_legacy_status_from_split(bs, ps)
  return bs, ps, st


def _sync_legacy_status(conn, invoice_id: int, *, billing_status=None, payment_status=None) -> str:
  """Persist legacy status derived from split statuses."""
  bs = billing_status
  ps = payment_status
  if bs is None or ps is None:
    row = conn.execute(
      "SELECT billing_status, payment_status FROM invoices WHERE id=?",
      (int(invoice_id),),
    ).fetchone()
    if row:
      bs = row_get(row, "billing_status", 0, default=bs)
      ps = row_get(row, "payment_status", 1, default=ps)
  legacy_status = _derive_legacy_status_from_split(bs or "", ps or "")
  conn.execute(
    "UPDATE invoices SET status=? WHERE id=?",
    (legacy_status, int(invoice_id)),
  )
  return legacy_status


def _resolve_billing_status(row) -> str:
  billing_status = _normalize_status(row_get(row, "billing_status", default=""), "")
  if billing_status:
    return billing_status
  legacy = _normalize_status(row_get(row, "status", default="draft"), "draft")
  verified = row_get(row, "payment_verified", default=0)
  return _compute_billing_payment_from_status(legacy, verified)[0]


def _resolve_payment_status(row) -> str:
  payment_status = _normalize_status(row_get(row, "payment_status", default=""), "")
  if payment_status:
    return payment_status
  legacy = _normalize_status(row_get(row, "status", default="draft"), "draft")
  verified = row_get(row, "payment_verified", default=0)
  return _compute_billing_payment_from_status(legacy, verified)[1]


_EXTERNAL_CASE_MAP_TRIGGERS_READY = False
_EXTERNAL_CASE_MAP_BACKFILL_READY = False


def _ensure_external_invoice_case_map(conn) -> None:
  """Ensure external_invoice_case_map exists and (for SQLite) keep it in sync at create-time.

  Goal: when invoices.ipm_case_id/ipm_case_ref is filled at invoice creation, automatically
  insert a row into external_invoice_case_map (N:N mapping) at the same moment.
  """
  global _EXTERNAL_CASE_MAP_TRIGGERS_READY

  if not _table_exists(conn, "external_invoice_case_map"):
    current_app.logger.error(
      "external_invoice_case_map table missing; apply migrations before linking invoices"
    )
    abort(500, "external_invoice_case_map table missing")

  # Best-effort: for SQLite, create triggers that auto-populate external_invoice_case_map
  # on invoice INSERT/UPDATE. Guarded to run once per process.
  if not _EXTERNAL_CASE_MAP_TRIGGERS_READY:
    try:
      _ensure_external_invoice_case_map_triggers(conn)
    except Exception as exc:
      # Don't break invoice module if trigger creation fails (e.g., non-sqlite dialect)
      try:
        current_app.logger.warning(
          "External invoice case map triggers not created: %s",
          exc,
          exc_info=True,
        )
      except Exception as log_exc:
        report_swallowed_exception(
          log_exc,
          context="billing_invoices.invoices._ensure_external_invoice_case_map.log_warning",
          log_key="billing_invoices.invoices._ensure_external_invoice_case_map.log_warning",
          log_window_seconds=300,
        )
    _EXTERNAL_CASE_MAP_TRIGGERS_READY = True

  global _EXTERNAL_CASE_MAP_BACKFILL_READY
  if not _EXTERNAL_CASE_MAP_BACKFILL_READY:
    try:
      _backfill_external_invoice_case_map(conn)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices._ensure_external_invoice_case_map.backfill",
        log_key="billing_invoices.invoices._ensure_external_invoice_case_map.backfill",
        log_window_seconds=300,
      )
    _EXTERNAL_CASE_MAP_BACKFILL_READY = True


def _backfill_external_invoice_case_map(conn) -> None:
  """Backfill N:N case links from invoice primary case fields on any DB dialect."""
  inv_table = _actual_table_name("invoices")
  if not inv_table or not _table_exists(conn, "invoices"):
    return

  quoted_inv = '"' + str(inv_table).replace('"', '""') + '"'
  conn.execute(
    f"""
    INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
    SELECT m.matter_id, COALESCE(m.our_ref, i.ipm_case_ref), i.id
     FROM {quoted_inv} i
     JOIN matter m ON m.matter_id = i.ipm_case_id
     WHERE {_not_deleted_sql("i.is_deleted")}
      AND i.ipm_case_id IS NOT NULL
      AND TRIM(i.ipm_case_id) <> ''
    """
  )
  conn.execute(
    f"""
    UPDATE external_invoice_case_map
      SET is_deleted=FALSE,
        deleted_at=NULL,
        deleted_by=NULL,
        delete_reason=NULL,
        deleted_op_id=NULL
     WHERE EXISTS (
      SELECT 1
       FROM {quoted_inv} i
       JOIN matter m ON m.matter_id = i.ipm_case_id
       WHERE {_not_deleted_sql("i.is_deleted")}
        AND i.ipm_case_id IS NOT NULL
        AND TRIM(i.ipm_case_id) <> ''
        AND external_invoice_case_map.external_invoice_id = i.id
        AND external_invoice_case_map.matter_id = m.matter_id
     )
    """
  )
  conn.execute(
    f"""
    INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
    SELECT m.matter_id, m.our_ref, i.id
     FROM {quoted_inv} i
     JOIN matter m ON m.our_ref = i.ipm_case_ref
     WHERE {_not_deleted_sql("i.is_deleted")}
      AND (i.ipm_case_id IS NULL OR TRIM(i.ipm_case_id) = '')
      AND i.ipm_case_ref IS NOT NULL
      AND TRIM(i.ipm_case_ref) <> ''
    """
  )
  conn.execute(
    f"""
    UPDATE external_invoice_case_map
      SET is_deleted=FALSE,
        deleted_at=NULL,
        deleted_by=NULL,
        delete_reason=NULL,
        deleted_op_id=NULL
     WHERE EXISTS (
      SELECT 1
       FROM {quoted_inv} i
       JOIN matter m ON m.our_ref = i.ipm_case_ref
       WHERE {_not_deleted_sql("i.is_deleted")}
        AND (i.ipm_case_id IS NULL OR TRIM(i.ipm_case_id) = '')
        AND i.ipm_case_ref IS NOT NULL
        AND TRIM(i.ipm_case_ref) <> ''
        AND external_invoice_case_map.external_invoice_id = i.id
        AND external_invoice_case_map.matter_id = m.matter_id
     )
    """
  )
  try:
    conn.commit()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices._backfill_external_invoice_case_map.commit",
      log_key="billing_invoices.invoices._backfill_external_invoice_case_map.commit",
      log_window_seconds=300,
    )


def _ensure_external_invoice_case_map_triggers(conn) -> None:
  """SQLite-only: create triggers to auto-fill external_invoice_case_map at invoice create-time.

  NOTE: billing_invoices SQL rewriter does NOT rewrite CREATE TRIGGER statements.
  Therefore we must bind triggers to the *actual* invoices table name (prefixed in integrated mode).
  """
  # Detect dialect (PrefixedConnection exposes dialect_name; fallback probe for SQLite).
  dialect = str(getattr(conn, "dialect_name", "") or "").lower()
  if not dialect:
    try:
      conn.execute("SELECT sqlite_version()").fetchone()
      dialect = "sqlite"
    except Exception:
      dialect = ""
  if not dialect.startswith("sqlite"):
    return

  # Resolve actual invoices table name (handles integrated prefix, e.g. billing_invoices).
  inv_table = None
  try:
    inv_table = _actual_table_name("invoices")
  except Exception:
    inv_table = None

  candidates = []
  if inv_table:
    candidates.append(inv_table)
  candidates += ["invoices", "billing_invoices"]

  chosen = None
  for t in candidates:
    try:
      if t and _table_exists(conn, t):
        chosen = t
        break
    except Exception:
      continue

  # Last resort: discover any table ending with "invoices"
  if not chosen:
    try:
      rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
        ("%invoices",),
      ).fetchall()
      if rows:
        chosen = rows[0][0]
    except Exception:
      chosen = None

  if not chosen:
    return

  # Trigger names are fixed and safe because we use IF NOT EXISTS.
  trg_ai = "trg_ext_case_map_ai"
  trg_au = "trg_ext_case_map_au"

  # AFTER INSERT: create mapping from invoices.ipm_case_id/ref -> external_invoice_case_map
  conn.execute(
    f"""
    CREATE TRIGGER IF NOT EXISTS {trg_ai}
    AFTER INSERT ON "{chosen}"
    BEGIN
     -- 1) Prefer ipm_case_id if it matches a real matter_id
     INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
     SELECT m.matter_id, COALESCE(m.our_ref, NEW.ipm_case_ref), NEW.id
      FROM matter m
      WHERE NEW.ipm_case_id IS NOT NULL
       AND TRIM(NEW.ipm_case_id) <> ''
       AND m.matter_id = NEW.ipm_case_id;

     UPDATE external_invoice_case_map
       SET is_deleted = 0,
         deleted_at = NULL,
         deleted_by = NULL,
         delete_reason = NULL,
         deleted_op_id = NULL
      WHERE external_invoice_id = NEW.id
       AND matter_id = NEW.ipm_case_id;

     -- 2) Fallback: ipm_case_ref (our_ref) -> resolve a matter_id
     INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
     SELECT m.matter_id, m.our_ref, NEW.id
      FROM matter m
      WHERE (NEW.ipm_case_id IS NULL OR TRIM(NEW.ipm_case_id) = '')
       AND NEW.ipm_case_ref IS NOT NULL
       AND TRIM(NEW.ipm_case_ref) <> ''
       AND m.our_ref = NEW.ipm_case_ref
      ORDER BY m.matter_id DESC
      LIMIT 1;

     UPDATE external_invoice_case_map
       SET is_deleted = 0,
         deleted_at = NULL,
         deleted_by = NULL,
         delete_reason = NULL,
         deleted_op_id = NULL
      WHERE external_invoice_id = NEW.id
       AND matter_id = (
        SELECT matter_id
         FROM matter
         WHERE our_ref = NEW.ipm_case_ref
         ORDER BY matter_id DESC
         LIMIT 1
       );
    END;
    """
  )

  # AFTER UPDATE (case fields): also keep map in sync when ipm_case_id/ref gets written later
  conn.execute(
    f"""
    CREATE TRIGGER IF NOT EXISTS {trg_au}
    AFTER UPDATE OF ipm_case_id, ipm_case_ref ON "{chosen}"
    BEGIN
     INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
     SELECT m.matter_id, COALESCE(m.our_ref, NEW.ipm_case_ref), NEW.id
      FROM matter m
      WHERE NEW.ipm_case_id IS NOT NULL
       AND TRIM(NEW.ipm_case_id) <> ''
       AND m.matter_id = NEW.ipm_case_id;

     UPDATE external_invoice_case_map
       SET is_deleted = 0,
         deleted_at = NULL,
         deleted_by = NULL,
         delete_reason = NULL,
         deleted_op_id = NULL
      WHERE external_invoice_id = NEW.id
       AND matter_id = NEW.ipm_case_id;

     INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
     SELECT m.matter_id, m.our_ref, NEW.id
      FROM matter m
      WHERE (NEW.ipm_case_id IS NULL OR TRIM(NEW.ipm_case_id) = '')
       AND NEW.ipm_case_ref IS NOT NULL
       AND TRIM(NEW.ipm_case_ref) <> ''
       AND m.our_ref = NEW.ipm_case_ref
      ORDER BY m.matter_id DESC
      LIMIT 1;

     UPDATE external_invoice_case_map
       SET is_deleted = 0,
         deleted_at = NULL,
         deleted_by = NULL,
         delete_reason = NULL,
         deleted_op_id = NULL
      WHERE external_invoice_id = NEW.id
       AND matter_id = (
        SELECT matter_id
         FROM matter
         WHERE our_ref = NEW.ipm_case_ref
         ORDER BY matter_id DESC
         LIMIT 1
       );
    END;
    """
  )

  # One-time best-effort backfill (cheap due to OR IGNORE) so old invoices also become visible via N:N map.
  try:
    conn.execute(
      f"""
      INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
      SELECT m.matter_id, COALESCE(m.our_ref, i.ipm_case_ref), i.id
       FROM "{chosen}" i
       JOIN matter m ON m.matter_id = i.ipm_case_id
       WHERE i.ipm_case_id IS NOT NULL AND TRIM(i.ipm_case_id) <> '';
      """
    )
    conn.execute(
      f"""
      INSERT OR IGNORE INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
      SELECT m.matter_id, m.our_ref, i.id
       FROM "{chosen}" i
       JOIN matter m ON m.our_ref = i.ipm_case_ref
       WHERE (i.ipm_case_id IS NULL OR TRIM(i.ipm_case_id) = '')
        AND i.ipm_case_ref IS NOT NULL
        AND TRIM(i.ipm_case_ref) <> '';
      """
    )
    try:
      conn.commit()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices._ensure_external_invoice_case_map.backfill.commit",
        log_key="billing_invoices.invoices._ensure_external_invoice_case_map.backfill.commit",
        log_window_seconds=300,
      )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices._ensure_external_invoice_case_map.backfill",
      log_key="billing_invoices.invoices._ensure_external_invoice_case_map.backfill",
      log_window_seconds=300,
    )


def _resolve_invoice_case_target(
  conn,
  *,
  ipm_case_id: str | None,
  ipm_case_ref: str | None,
  internal_reference: str | None,
) -> dict:
  raw_case_id = str(ipm_case_id or "").strip()
  raw_case_ref = str(ipm_case_ref or "").strip()
  raw_internal_ref = str(internal_reference or "").strip()
  explicit_input = bool(raw_case_id or raw_case_ref)

  if raw_case_id:
    try:
      row = conn.execute(
        "SELECT matter_id, COALESCE(our_ref, '') FROM matter WHERE matter_id=?",
        (raw_case_id,),
      ).fetchone()
    except Exception:
      row = None
    if row:
      return {
        "status": "ok",
        "matter_id": str(row[0] or "").strip(),
        "our_ref": str(row[1] or "").strip(),
        "source": "ipm_case_id",
        "input": raw_case_id,
        "explicit_input": explicit_input,
        "matches": [
          {"matter_id": str(row[0] or "").strip(), "our_ref": str(row[1] or "").strip()}
        ],
      }
    if not raw_case_ref:
      return {
        "status": "not_found",
        "matter_id": None,
        "our_ref": None,
        "source": "ipm_case_id",
        "input": raw_case_id,
        "explicit_input": explicit_input,
        "matches": [],
      }

  if raw_case_ref:
    resolved = resolve_matter_identifier(conn, raw_case_ref)
    resolved["source"] = "ipm_case_ref"
    resolved["input"] = raw_case_ref
    resolved["explicit_input"] = explicit_input
    return resolved

  if raw_internal_ref:
    resolved = resolve_matter_identifier(conn, raw_internal_ref)
    resolved["source"] = "internal_reference"
    resolved["input"] = raw_internal_ref
    resolved["explicit_input"] = explicit_input
    return resolved

  return {
    "status": "empty",
    "matter_id": None,
    "our_ref": None,
    "source": None,
    "input": "",
    "explicit_input": explicit_input,
    "matches": [],
  }


def _set_invoice_primary_case(conn, invoice_id: int) -> None:
  _ensure_external_invoice_case_map(conn)
  row = None
  try:
    row = conn.execute(
      f"""
      SELECT m.matter_id, COALESCE(m.our_ref, l.our_ref) AS our_ref
      FROM external_invoice_case_map l
      LEFT JOIN matter m ON m.matter_id = l.matter_id
      WHERE l.external_invoice_id=?
       AND {_not_deleted_sql("l.is_deleted")}
      ORDER BY l.id DESC
      LIMIT 1
      """,
      (int(invoice_id),),
    ).fetchone()
  except Exception:
    row = None

  if row:
    try:
      conn.execute(
        "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
        (row[0], row[1], int(invoice_id)),
      )
      conn.commit()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices._set_invoice_primary_case.update_invoice",
        log_key="billing_invoices.invoices._set_invoice_primary_case.update_invoice",
        log_window_seconds=300,
      )
    return

  try:
    conn.execute(
      "UPDATE invoices SET ipm_case_id=NULL, ipm_case_ref=NULL WHERE id=?",
      (int(invoice_id),),
    )
    conn.commit()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices._set_invoice_primary_case.clear_invoice",
      log_key="billing_invoices.invoices._set_invoice_primary_case.clear_invoice",
      log_window_seconds=300,
    )


_INVOICE_OUR_REF_SUFFIX_RE = r"(?:[A-Z]{0,2}|PCT)"
_INVOICE_OUR_REF_CORE_RE = (
  rf"(?:\d{{2}}[A-Z]{{1,3}}\d{{3,4}}{_INVOICE_OUR_REF_SUFFIX_RE}|"
  rf"[A-Z]{{1,2}}\d{{2}}\d{{3,4}}{_INVOICE_OUR_REF_SUFFIX_RE})"
)
_MULTI_OUR_REF_TOKEN_RE = re.compile(rf"\b({_INVOICE_OUR_REF_CORE_RE})\b", re.IGNORECASE)
_MULTI_OUR_REF_RANGE_RE = re.compile(
  rf"\b({_INVOICE_OUR_REF_CORE_RE})\b\s*[-~]\s*\b({_INVOICE_OUR_REF_CORE_RE})\b",
  re.IGNORECASE,
)
_INVOICE_OUR_REF_PARTS_RE = re.compile(
  rf"^(\d{{2}})([A-Z]{{1,3}})(\d{{3,4}})({_INVOICE_OUR_REF_SUFFIX_RE})$"
)
_INVOICE_OUR_REF_PARTS_PREFIX_RE = re.compile(
  rf"^([A-Z]{{1,2}})(\d{{2}})(\d{{3,4}})({_INVOICE_OUR_REF_SUFFIX_RE})$"
)
_INVOICE_OUR_REF_SHORT_SEQ_RE = re.compile(
  rf"^(\d{{3,4}})({_INVOICE_OUR_REF_SUFFIX_RE})$", re.IGNORECASE
)
_INVOICE_CASE_REF_SPLIT_RE = re.compile(r"[\s,;/|]+")
_MAX_INVOICE_MULTI_REF_RANGE_SIZE = 200


def _normalize_invoice_case_ref_token(raw: str | None) -> str:
  return str(raw or "").strip().upper()


def _parse_invoice_our_ref_parts(raw: str) -> tuple[str, str, str, str, str] | None:
  token = _normalize_invoice_case_ref_token(raw)
  match = _INVOICE_OUR_REF_PARTS_RE.match(token)
  if match:
    year, code, seq, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
    return year, code, seq, suffix, "year_first"
  match = _INVOICE_OUR_REF_PARTS_PREFIX_RE.match(token)
  if not match:
    return None
  code, year, seq, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
  return year, code, seq, suffix, "code_first"


def _build_invoice_our_ref(year: str, code: str, seq: str, suffix: str, style: str) -> str:
  if style == "code_first":
    return f"{code}{year}{seq}{suffix}"
  return f"{year}{code}{seq}{suffix}"


def _parse_invoice_case_ref_shorthand(raw: str) -> tuple[str, str] | None:
  token = _normalize_invoice_case_ref_token(raw)
  match = _INVOICE_OUR_REF_SHORT_SEQ_RE.match(token)
  if not match:
    return None
  return str(match.group(1) or "").strip(), str(match.group(2) or "").strip()


def _extract_contextual_invoice_case_refs(
  text: str,
  *,
  max_tokens: int,
  seen: set[str],
) -> list[str]:
  parts = []
  for raw_part in _INVOICE_CASE_REF_SPLIT_RE.split(text):
    token = _normalize_invoice_case_ref_token(raw_part.strip("()[]{}<>"))
    if not token:
      continue
    if any(sep in token for sep in ("-", "~")):
      continue
    parts.append(token)

  parsed_items: list[dict[str, str]] = []
  last_context: tuple[str, str, str] | None = None
  for token in parts:
    parsed = _parse_invoice_our_ref_parts(token)
    if parsed:
      year, code, seq, suffix, style = parsed
      parsed_items.append(
        {
          "year": year,
          "code": code,
          "seq": seq,
          "suffix": suffix,
          "style": style,
        }
      )
      last_context = (year, code, style)
      continue

    shorthand = _parse_invoice_case_ref_shorthand(token)
    if shorthand and last_context:
      seq, suffix = shorthand
      year, code, style = last_context
      parsed_items.append(
        {
          "year": year,
          "code": code,
          "seq": seq,
          "suffix": suffix,
          "style": style,
        }
      )

  if not parsed_items:
    return []

  suffix_map: dict[tuple[str, str, str], str] = {}
  for item in parsed_items:
    suffix = str(item.get("suffix") or "").strip()
    if not suffix:
      continue
    suffix_map[(item["year"], item["code"], item["style"])] = suffix

  refs: list[str] = []
  for item in parsed_items:
    key = (item["year"], item["code"], item["style"])
    suffix = str(item.get("suffix") or "").strip() or suffix_map.get(key, "")
    ref = _build_invoice_our_ref(
      item["year"],
      item["code"],
      item["seq"],
      suffix,
      item["style"],
    )
    if not ref or ref in seen:
      continue
    seen.add(ref)
    refs.append(ref)
    if len(refs) >= int(max_tokens):
      break
  return refs


def _expand_invoice_our_ref_range(a: str, b: str, *, max_size: int) -> list[str]:
  start = _parse_invoice_our_ref_parts(a)
  end = _parse_invoice_our_ref_parts(b)
  if not start or not end:
    return []
  y1, c1, s1, suf1, style1 = start
  y2, c2, s2, suf2, style2 = end
  if (y1, c1, suf1, style1) != (y2, c2, suf2, style2):
    return []
  if len(s1) != len(s2):
    return []
  try:
    n1 = int(s1)
    n2 = int(s2)
  except Exception:
    return []
  if n2 < n1:
    return []
  if (n2 - n1) > max_size:
    return []
  width = len(s1)
  refs: list[str] = []
  for num in range(n1, n2 + 1):
    seq = str(num).zfill(width)
    refs.append(_build_invoice_our_ref(y1, c1, seq, suf1, style1))
  return refs


def _extract_invoice_case_ref_inputs(raw: str | None, *, max_tokens: int = 12) -> list[str]:
  text = str(raw or "").strip()
  if not text:
    return []

  tokens: list[str] = []
  seen: set[str] = set()
  for match in _MULTI_OUR_REF_RANGE_RE.finditer(text):
    start_ref = str(match.group(1) or "").strip()
    end_ref = str(match.group(2) or "").strip()
    expanded = _expand_invoice_our_ref_range(
      start_ref,
      end_ref,
      max_size=_MAX_INVOICE_MULTI_REF_RANGE_SIZE,
    )
    refs = expanded or [start_ref, end_ref]
    for ref in refs:
      token = _normalize_invoice_case_ref_token(ref)
      if not token or token in seen:
        continue
      seen.add(token)
      tokens.append(token)
      if len(tokens) >= int(max_tokens):
        return tokens

  remaining = max(0, int(max_tokens) - len(tokens))
  if remaining:
    tokens.extend(
      _extract_contextual_invoice_case_refs(
        text,
        max_tokens=remaining,
        seen=seen,
      )
    )

  if tokens:
    return tokens
  return [text]


def _fetch_matter_preview_map(conn, matter_ids: list[str]) -> dict[str, dict[str, str]]:
  mids = [str(mid or "").strip() for mid in (matter_ids or []) if str(mid or "").strip()]
  if not mids:
    return {}

  placeholders = ",".join(["?"] * len(mids))
  rows = conn.execute(
    f"""
    SELECT matter_id, COALESCE(our_ref, '') AS our_ref, COALESCE(right_name, '') AS right_name
    FROM matter
    WHERE matter_id IN ({placeholders})
    """,
    mids,
  ).fetchall()
  out: dict[str, dict[str, str]] = {}
  for row in rows or []:
    matter_id = str(row[0] or "").strip()
    if not matter_id:
      continue
    out[matter_id] = {
      "matter_id": matter_id,
      "our_ref": str(row[1] or "").strip(),
      "right_name": str(row[2] or "").strip(),
    }
  return out


def _build_internal_reference_case_suggestions(
  conn,
  *,
  raw_value: str | None,
  linked_matter_ids: set[str] | None = None,
) -> list[dict[str, object]]:
  ref_inputs = _extract_invoice_case_ref_inputs(raw_value)
  if not ref_inputs:
    return []

  linked_ids = {
    str(matter_id or "").strip()
    for matter_id in (linked_matter_ids or set())
    if str(matter_id or "").strip()
  }
  resolved_items: list[tuple[str, dict]] = []
  matter_ids: list[str] = []
  matter_seen: set[str] = set()

  for ref_input in ref_inputs:
    resolved = resolve_matter_identifier(conn, ref_input)
    resolved_items.append((ref_input, resolved))
    if str(resolved.get("status") or "").strip() == "ok":
      matter_id = str(resolved.get("matter_id") or "").strip()
      if matter_id and matter_id not in matter_seen:
        matter_seen.add(matter_id)
        matter_ids.append(matter_id)
      continue
    for match in resolved.get("matches") or []:
      matter_id = str((match or {}).get("matter_id") or "").strip()
      if not matter_id or matter_id in matter_seen:
        continue
      matter_seen.add(matter_id)
      matter_ids.append(matter_id)

  matter_preview = _fetch_matter_preview_map(conn, matter_ids)
  suggestions: list[dict[str, object]] = []
  for ref_input, resolved in resolved_items:
    status = str(resolved.get("status") or "").strip() or "not_found"
    if status == "ok":
      matter_id = str(resolved.get("matter_id") or "").strip()
      preview = matter_preview.get(matter_id) or {}
      suggestions.append(
        {
          "input": ref_input,
          "status": "ok",
          "matter_id": matter_id,
          "our_ref": preview.get("our_ref")
          or str(resolved.get("our_ref") or "").strip()
          or matter_id,
          "right_name": preview.get("right_name") or "",
          "already_linked": matter_id in linked_ids,
        }
      )
      continue
    if status == "ambiguous":
      matches: list[dict[str, object]] = []
      for match in resolved.get("matches") or []:
        matter_id = str((match or {}).get("matter_id") or "").strip()
        preview = matter_preview.get(matter_id) or {}
        matches.append(
          {
            "matter_id": matter_id,
            "our_ref": preview.get("our_ref")
            or str((match or {}).get("our_ref") or "").strip()
            or matter_id,
            "right_name": preview.get("right_name") or "",
            "already_linked": matter_id in linked_ids,
          }
        )
      suggestions.append({"input": ref_input, "status": "ambiguous", "matches": matches})
      continue
    suggestions.append({"input": ref_input, "status": "not_found", "matches": []})

  return suggestions


def _load_existing_invoice_case_link_ids(conn, invoice_id: int) -> set[str]:
  rows = conn.execute(
    f"""
    SELECT matter_id
    FROM external_invoice_case_map
    WHERE external_invoice_id=?
     AND {_not_deleted_sql("is_deleted")}
    """,
    (int(invoice_id),),
  ).fetchall()
  out: set[str] = set()
  for row in rows or []:
    matter_id = str(row[0] or "").strip()
    if matter_id:
      out.add(matter_id)
  return out


def _attach_invoice_case_links_for_list(conn, invoices: list[dict]) -> None:
  if not invoices:
    return

  invoice_map: dict[int, dict] = {}
  invoice_ids: list[int] = []
  for inv in invoices:
    try:
      invoice_id = int(inv.get("id") or 0)
    except Exception:
      invoice_id = 0
    inv["matched_case_links"] = []
    inv["matched_case_count"] = 0
    if invoice_id <= 0:
      continue
    invoice_map[invoice_id] = inv
    invoice_ids.append(invoice_id)

  if not invoice_ids:
    return

  placeholders = ",".join(["?"] * len(invoice_ids))
  rows = conn.execute(
    f"""
    SELECT l.external_invoice_id AS invoice_id,
        l.matter_id,
        COALESCE(m.our_ref, l.our_ref, l.matter_id) AS our_ref,
        COALESCE(m.right_name, '') AS right_name
    FROM external_invoice_case_map l
    LEFT JOIN matter m ON m.matter_id = l.matter_id
    WHERE l.external_invoice_id IN ({placeholders})
     AND {_not_deleted_sql("l.is_deleted")}
    ORDER BY l.external_invoice_id,
         COALESCE(m.our_ref, l.our_ref, l.matter_id) DESC,
         l.id ASC
    """,
    invoice_ids,
  ).fetchall()

  for row in rows or []:
    try:
      invoice_id = int(row[0] or 0)
    except Exception:
      invoice_id = 0
    if invoice_id <= 0:
      continue
    inv = invoice_map.get(invoice_id)
    if not inv:
      continue
    links = inv.setdefault("matched_case_links", [])
    links.append(
      {
        "matter_id": str(row[1] or "").strip(),
        "our_ref": str(row[2] or "").strip(),
        "right_name": str(row[3] or "").strip(),
      }
    )
    inv["matched_case_count"] = len(links)

  fallback_matter_ids: list[str] = []
  for inv in invoices:
    if inv.get("matched_case_links"):
      continue
    matter_id = str(inv.get("ipm_case_id") or "").strip()
    if matter_id:
      fallback_matter_ids.append(matter_id)

  if not fallback_matter_ids:
    return

  fallback_placeholders = ",".join(["?"] * len(fallback_matter_ids))
  matter_rows = conn.execute(
    f"""
    SELECT matter_id,
        COALESCE(our_ref, '') AS our_ref,
        COALESCE(right_name, '') AS right_name
    FROM matter
    WHERE matter_id IN ({fallback_placeholders})
    """,
    fallback_matter_ids,
  ).fetchall()
  matter_map = {
    str(row[0] or "").strip(): {
      "our_ref": str(row[1] or "").strip(),
      "right_name": str(row[2] or "").strip(),
    }
    for row in (matter_rows or [])
  }

  for inv in invoices:
    if inv.get("matched_case_links"):
      continue
    matter_id = str(inv.get("ipm_case_id") or "").strip()
    if not matter_id:
      continue
    matter_info = matter_map.get(matter_id) or {}
    our_ref = (
      str(matter_info.get("our_ref") or "").strip()
      or str(inv.get("ipm_case_ref") or "").strip()
      or matter_id
    )
    inv["matched_case_links"] = [
      {
        "matter_id": matter_id,
        "our_ref": our_ref,
        "right_name": str(matter_info.get("right_name") or "").strip(),
      }
    ]
    inv["matched_case_count"] = 1


def _safe_int(v, default=None, min_=None, max_=None):
  try:
    if v is None:
      return default
    s = str(v).strip()
    if not s:
      return default
    x = int(s)
  except Exception:
    return default
  if min_ is not None:
    x = max(min_, x)
  if max_ is not None:
    x = min(max_, x)
  return x


def _safe_float(v, default=None):
  try:
    if v is None:
      return default
    s = str(v).strip()
    if not s:
      return default
    return float(s)
  except Exception:
    return default


def _safe_same_host_referrer(ref: str | None, default_url: str, disallow_path: str | None = None):
  next_url = default_url
  if not ref:
    return next_url
  try:
    parsed = urlparse(ref)
    if parsed.netloc not in ("", request.host):
      return next_url
    if disallow_path and parsed.path == disallow_path:
      return next_url
    return ref
  except Exception:
    return next_url


def _parse_settlement_splits(
  form, *, allowed_bp_ids: set[int] | None = None
) -> tuple[list[dict], str | None, str | None]:
  settle_bp_ids = form.getlist("settle_bp_id[]")
  settle_percents = form.getlist("settle_percent[]")
  max_len = max(len(settle_bp_ids), len(settle_percents))

  splits: list[dict] = []
  seen_bp_ids: set[int] = set()
  total_percent = Decimal("0")

  for idx in range(max_len):
    bp_raw = (settle_bp_ids[idx] if idx < len(settle_bp_ids) else "") or ""
    pct_raw = (settle_percents[idx] if idx < len(settle_percents) else "") or ""
    bp_raw = str(bp_raw).strip()
    pct_raw = str(pct_raw).strip()
    row_no = idx + 1

    if not bp_raw and not pct_raw:
      continue
    if not bp_raw or not pct_raw:
      return splits, None, f"Settlement {row_no}row enter."

    bp_val = _safe_int(bp_raw, None)
    if bp_val is None or bp_val <= 0:
      return splits, None, f"Settlement business profile confirm. ({row_no}row)"
    if allowed_bp_ids is not None and bp_val not in allowed_bp_ids:
      return splits, None, f" Settlement business profile. ({row_no}row)"
    if bp_val in seen_bp_ids:
      return (
        splits,
        None,
        " Settlement business profile Duplicate. Business profile  enter.",
      )

    try:
      pct_val = d(pct_raw)
    except Exception:
      return splits, None, f"Settlement  enter. ({row_no}row)"
    if pct_val <= 0:
      return splits, None, f"Settlement 0 . ({row_no}row)"
    if pct_val > Decimal("100"):
      return splits, None, f"Settlement 100  not available. ({row_no}row)"

    seen_bp_ids.add(int(bp_val))
    total_percent += pct_val
    splits.append({"business_profile_id": int(bp_val), "percent": float(pct_val)})

  if not splits:
    return [], None, None

  if abs(total_percent - Decimal("100")) > Decimal("0.01"):
    total_str = f"{total_percent:.2f}".rstrip("0").rstrip(".")
    return splits, None, f"Settlement Total 100% . (Current {total_str}%)"

  if is_default_settlement_split(splits, form.get("business_profile_id")):
    return [], None, None

  try:
    meta = json.dumps(splits, ensure_ascii=False)
  except Exception:
    return [], None, "Settlement  Save not available."

  return splits, meta, None


def _stable_json_dumps(obj) -> str:
  # Stable JSON for hashing snapshots (ensures deterministic revision hashes).
  return json.dumps(
    obj,
    sort_keys=True,
    ensure_ascii=False,
    separators=(",", ":"),
    default=str,
  )


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename_stem(value: str, *, fallback: str = "invoice") -> str:
  s = (value or "").strip()
  if not s:
    s = fallback
  # Keep filename-safe ASCII only.
  s = _FILENAME_SAFE_RE.sub("_", s)
  s = re.sub(r"_+", "_", s).strip("._-")
  return s or fallback


def _build_invoice_revision_snapshot(conn, invoice_id: int) -> dict | None:
  """Build a snapshot for printing/PDF revisioning (invoice + items + biz_profile)."""
  client_extra_expr = _client_extra_select_expr(conn)
  row = conn.execute(
    f"""SELECT invoices.*, clients.name as client_name, clients.email as client_email,
         clients.phone as client_phone, clients.address as client_address,
         clients.manager as client_manager, {client_extra_expr}
      FROM invoices JOIN clients ON clients.id=invoices.client_id
      WHERE invoices.id=?""",
    (int(invoice_id),),
  ).fetchone()
  if not row:
    return None

  inv = row_to_dict(row)
  _augment_invoice_client_language_fields(inv)
  inv.pop("client_extra", None)
  # Normalize split statuses for rendering stability (older rows might rely on legacy status).
  try:
    bs, ps, st = _normalize_invoice_status_fields(inv)
    inv["billing_status"] = bs
    inv["payment_status"] = ps
    inv["status"] = st
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices._build_invoice_revision_snapshot.normalize_status",
      log_key="billing_invoices.invoices._build_invoice_revision_snapshot.normalize_status",
      log_window_seconds=300,
    )

  items_rows = conn.execute(
    "SELECT * FROM line_items WHERE invoice_id=? ORDER BY id ASC",
    (int(invoice_id),),
  ).fetchall()
  items = [row_to_dict(r) for r in (items_rows or [])]

  # Prefer stored business_snapshot for historical accuracy.
  biz_profile = None
  try:
    if inv.get("business_snapshot"):
      biz_profile = safe_json_parse(inv.get("business_snapshot"))
  except Exception:
    biz_profile = None
  if not isinstance(biz_profile, dict):
    biz_profile = None

  def _is_blank(v):
    if v is None:
      return True
    if isinstance(v, str):
      return v.strip() == ""
    return False

  if not biz_profile or _is_blank(biz_profile.get("name")):
    live_bp = get_business_profile(inv.get("business_profile_id") or 1)
    if biz_profile and isinstance(live_bp, dict):
      for key in (
        "name",
        "address",
        "email",
        "phone",
        "tax_id",
        "bank_account",
        "logo_path",
        "currency",
        "vat_rate",
      ):
        if _is_blank(biz_profile.get(key)):
          biz_profile[key] = live_bp.get(key)
    else:
      biz_profile = live_bp if isinstance(live_bp, dict) else {}

  return {"invoice": inv, "items": items, "biz_profile": biz_profile or {}}


def _invoice_revision_hash_payload(snapshot: dict) -> dict:
  inv = (snapshot or {}).get("invoice") or {}
  items = (snapshot or {}).get("items") or []
  bp = (snapshot or {}).get("biz_profile") or {}

  # Only hash fields that affect the rendered invoice document.
  inv_keys = (
    "number",
    "internal_reference",
    "issue_date",
    "due_date",
    "billing_status",
    "notes",
    "currency",
    "vat_rate",
    "subtotal",
    "tax",
    "total",
    "subtotal_minor",
    "tax_minor",
    "total_minor",
    "client_name",
    "client_email",
    "client_phone",
    "client_address",
    "client_manager",
  )
  inv_h = {k: inv.get(k) for k in inv_keys}
  for k in ("client_name_en", "client_address_en"):
    value = _clean_invoice_text(inv.get(k))
    if value:
      inv_h[k] = value

  item_keys = (
    "description",
    "qty",
    "unit_price",
    "discount",
    "item_type",
    "is_taxable",
    "phase",
    "fx_currency",
    "fx_fee",
    "fx_gov",
    "fx_markup",
    "fx_rate_used",
    "is_estimated",
  )
  items_h = [{k: it.get(k) for k in item_keys} for it in (items or [])]

  bp_keys = (
    "name",
    "address",
    "email",
    "phone",
    "tax_id",
    "bank_account",
    "logo_path",
    "currency",
    "vat_rate",
  )
  bp_h = {k: (bp or {}).get(k) for k in bp_keys}

  return {"invoice": inv_h, "items": items_h, "biz_profile": bp_h}


def _compute_invoice_revision_hash(snapshot: dict) -> str:
  payload = _invoice_revision_hash_payload(snapshot)
  raw = _stable_json_dumps(payload)
  return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_invoice_revision_for_print(
  conn,
  invoice_id: int,
  *,
  render_lang: str | None = None,
  render_outgoing: bool | None = None,
  source: str = "print",
):
  """Ensure there's a stored revision for the current invoice content.

  Returns: (revision_no, created_bool, file_stem)
  """
  if not _table_exists(conn, "invoice_revisions"):
    raise RuntimeError("invoice_revisions table missing")

  snap = _build_invoice_revision_snapshot(conn, invoice_id)
  if not snap:
    raise LookupError("invoice not found")

  inv = (snap.get("invoice") or {}) if isinstance(snap, dict) else {}
  inv_number = str(inv.get("number") or "").strip() or f"invoice_{int(invoice_id)}"

  content_hash = _compute_invoice_revision_hash(snap)

  # Read latest revision
  latest = conn.execute(
    """
    SELECT revision_no, content_hash
    FROM invoice_revisions
    WHERE invoice_id=?
    ORDER BY revision_no DESC
    LIMIT 1
    """,
    (int(invoice_id),),
  ).fetchone()
  if latest:
    try:
      latest_hash = row_get(latest, "content_hash", 1, default=None)
    except Exception:
      latest_hash = None
    if latest_hash and str(latest_hash) == str(content_hash):
      rev_no = row_get(latest, "revision_no", 0, default=0)
      try:
        rev_no = int(rev_no or 0)
      except Exception:
        rev_no = 0
      stem = _sanitize_filename_stem(inv_number, fallback=f"invoice_{int(invoice_id)}")
      if rev_no > 0:
        stem = f"{stem}_rev{rev_no}"
      return rev_no, False, stem

  # Need a new revision
  latest_no = None
  if latest:
    latest_no = row_get(latest, "revision_no", 0, default=None)
  try:
    latest_no_i = int(latest_no) if latest_no is not None else None
  except Exception:
    latest_no_i = None
  new_rev_no = 0 if latest_no_i is None else int(latest_no_i) + 1

  stem = _sanitize_filename_stem(inv_number, fallback=f"invoice_{int(invoice_id)}")
  file_stem = stem if new_rev_no <= 0 else f"{stem}_rev{new_rev_no}"

  # Persist revision (best-effort concurrency safety via OR IGNORE + retry)
  user = get_current_user()
  try:
    created_by = int((user or {}).get("id")) if user else None
  except Exception:
    created_by = None

  # Normalize render flags (store as metadata; not part of hash)
  rl = (render_lang or "").strip().lower() or None
  if rl not in (None, "en"):
    rl = None
  ro = 1 if (render_outgoing is True) else 0 if (render_outgoing is False) else None

  snap_json = _stable_json_dumps(snap)

  inserted = False
  for _ in range(5):
    try:
      conn.execute(
        """
        INSERT OR IGNORE INTO invoice_revisions
         (invoice_id, revision_no, content_hash, file_name, render_lang, render_outgoing, source, snapshot_json, created_by)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
          int(invoice_id),
          int(new_rev_no),
          str(content_hash),
          str(file_stem),
          rl,
          ro,
          str(source or "print"),
          snap_json,
          created_by,
        ),
      )
      conn.commit()
    except Exception:
      try:
        conn.rollback()
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices._ensure_invoice_revision_for_print.rollback",
          log_key="billing_invoices.invoices._ensure_invoice_revision_for_print.rollback",
          log_window_seconds=300,
        )

    # Confirm row exists (could have been inserted by us or concurrently)
    chk = conn.execute(
      "SELECT 1 FROM invoice_revisions WHERE invoice_id=? AND revision_no=?",
      (int(invoice_id), int(new_rev_no)),
    ).fetchone()
    if chk:
      inserted = True
      break

    # Collision: recompute new_rev_no and retry
    latest = conn.execute(
      """
      SELECT revision_no, content_hash
      FROM invoice_revisions
      WHERE invoice_id=?
      ORDER BY revision_no DESC
      LIMIT 1
      """,
      (int(invoice_id),),
    ).fetchone()
    if latest:
      latest_hash = row_get(latest, "content_hash", 1, default=None)
      if latest_hash and str(latest_hash) == str(content_hash):
        rev_no = row_get(latest, "revision_no", 0, default=0)
        try:
          rev_no = int(rev_no or 0)
        except Exception:
          rev_no = 0
        stem = _sanitize_filename_stem(inv_number, fallback=f"invoice_{int(invoice_id)}")
        if rev_no > 0:
          stem = f"{stem}_rev{rev_no}"
        return rev_no, False, stem
      try:
        latest_no_i = int(row_get(latest, "revision_no", 0, default=0) or 0)
      except Exception:
        latest_no_i = 0
      new_rev_no = latest_no_i + 1
      file_stem = stem if new_rev_no <= 0 else f"{stem}_rev{new_rev_no}"

  if not inserted:
    raise RuntimeError("failed to persist invoice revision")

  return int(new_rev_no), True, str(file_stem)


@bp.route("")
def list_invoices():
  bp_id = request.args.get("business_profile_id", "").strip()
  ipm_case_id = (request.args.get("ipm_case_id") or "").strip()
  ipm_case_ref = (request.args.get("ipm_case_ref") or "").strip()
  basis = (request.args.get("basis") or "issued").strip().lower()
  if basis not in ("issued", "settlement"):
    basis = "issued"
  all_profiles = get_all_business_profiles()
  # Build distinct currencies from business profiles
  try:
    currencies = sorted({((p["currency"] or "USD").upper()) for p in all_profiles})
  except Exception:
    currencies = ["USD"]
  try:
    bp_name_map = {p["id"]: p["name"] for p in all_profiles}
  except Exception:
    bp_name_map = {}
  # If filtering by currency group like 'C:USD', do not resolve a single business profile row
  if bp_id and bp_id.upper().startswith("C:"):
    bp_row = None
  else:
    bp_id_int = _safe_int(bp_id, None)
    bp_row = get_business_profile(bp_id_int) if bp_id_int else get_business_profile()

  q = request.args.get("q", "").strip()
  is_compact_q = q and is_compact_query(q)
  status = request.args.get("status", "").strip()
  outgoing_filter = request.args.get("outgoing", "").strip()
  sort = request.args.get("sort", "issue_date_desc").strip().lower()
  date_from = request.args.get("date_from", "").strip()
  date_to = request.args.get("date_to", "").strip()
  min_amount = request.args.get("min_amount", "").strip()
  max_amount = request.args.get("max_amount", "").strip()
  min_amount_v = _safe_float(min_amount, None) if min_amount else None
  max_amount_v = _safe_float(max_amount, None) if max_amount else None

  sort = {
    "number": "number_desc",
    "number_desc": "number_desc",
    "issue_date": "issue_date_desc",
    "issue_date_desc": "issue_date_desc",
    "issue_date_asc": "issue_date_asc",
    "due_date": "due_date_desc",
    "due_date_desc": "due_date_desc",
    "due_date_asc": "due_date_asc",
    "total": "amount_desc",
    "total_desc": "amount_desc",
    "amount_desc": "amount_desc",
    "total_asc": "amount_asc",
    "amount_asc": "amount_asc",
  }.get(sort, "issue_date_desc")

  # page items 
  per_page = _safe_int(request.args.get("per_page", 20), 20, 5, 200)

  conn = get_db()
  _ensure_external_invoice_case_map(conn)
  params, where = [], []
  if q and not is_compact_q:
    invoice_clause, invoice_params = sql_ci_contains_any(
      [
        "invoices.number",
        "invoices.internal_reference",
        "clients.name",
        "invoices.notes",
        "invoices.ipm_case_ref",
        "invoices.ipm_case_id",
      ],
      q,
    )
    linked_clause, linked_params = sql_ci_contains_any(
      [
        "eicm.our_ref",
        "eicm.matter_id",
        "m.our_ref",
        "m.old_our_ref",
        "m.your_ref",
      ],
      q,
    )
    where.append(
      "("
      f"{invoice_clause} "
      "OR EXISTS ("
      " SELECT 1 FROM external_invoice_case_map eicm "
      " LEFT JOIN matter m ON m.matter_id = eicm.matter_id "
      " WHERE eicm.external_invoice_id = invoices.id "
      f"  AND {_not_deleted_sql('eicm.is_deleted')} "
      f"  AND {linked_clause}"
      ")"
      ")"
    )
    params += invoice_params + linked_params
  if status:
    # Map legacy status filter to split columns
    if status == "tax_issued":
      where.append("invoices.billing_status IN ('tax_issued', 'cash_issued', 'processed')")
    elif status in (
      "draft",
      "sent",
      "void",
      "cash_issued",
      "processed",
      "pre_overdue",
    ):
      where.append("invoices.billing_status = ?")
      params.append(status)
    elif status == "sent_unpaid":
      where.append(
        "(invoices.billing_status = 'sent' AND invoices.payment_status = 'unpaid')"
      )
    elif status == "payment_pending":
      where.append(
        "(invoices.billing_status = 'sent' AND invoices.payment_status = 'pending')"
      )
    elif status == "sent_unpaid_or_pending":
      where.append(
        "(invoices.billing_status = 'sent' AND invoices.payment_status IN ('unpaid','pending'))"
      )
    elif status == "paid":
      where.append("invoices.payment_status = 'paid'")
    elif status == "paid_no_tax":
      where.append(
        "(invoices.payment_status = 'paid' AND (invoices.billing_status IS NULL OR invoices.billing_status NOT IN ('tax_issued', 'cash_issued', 'processed')))"
      )
    else:
      # Fallback to legacy column for unknown filters
      where.append("invoices.status = ?")
      params.append(status)
  if outgoing_filter == "1":
    where.append(_outgoing_invoice_filter_sql("invoices"))
  use_settlement_filter = False
  target_bp_id = None
  if bp_id:
    if bp_id.upper().startswith("C:"):
      # Currency group filter: C:USD, C:USD, ...
      where.append("invoices.currency = ?")
      params.append(bp_id.split(":", 1)[1].upper())
    else:
      # basis='settlement'  Issuing business profile Filters SQL , from Settlement business profile to Filters
      if basis == "settlement":
        use_settlement_filter = True
        try:
          target_bp_id = int(bp_id)
        except Exception:
          target_bp_id = None
      else:
        where.append("invoices.business_profile_id = ?")
        bp_id_int = _safe_int(bp_id, None)
        if bp_id_int is not None:
          params.append(int(bp_id_int))
  if date_from:
    where.append("invoices.issue_date >= ?")
    params.append(date_from)
  if date_to:
    where.append("invoices.issue_date <= ?")
    params.append(date_to)
  # Case filter (N:N aware via external_invoice_case_map)
  if ipm_case_id:
    where.append(
      "(UPPER(TRIM(COALESCE(invoices.ipm_case_id, ''))) = UPPER(TRIM(?)) "
      "OR EXISTS (SELECT 1 FROM external_invoice_case_map eicm "
      "      WHERE eicm.external_invoice_id = invoices.id "
      f"       AND {_not_deleted_sql('eicm.is_deleted')} "
      "       AND UPPER(TRIM(COALESCE(eicm.matter_id, ''))) = UPPER(TRIM(?))))"
    )
    params.append(ipm_case_id)
    params.append(ipm_case_id)
  elif ipm_case_ref:
    where.append(
      "(UPPER(TRIM(COALESCE(invoices.ipm_case_ref, ''))) = UPPER(TRIM(?)) "
      "OR EXISTS ("
      " SELECT 1 "
      " FROM external_invoice_case_map eicm "
      " LEFT JOIN matter m ON m.matter_id = eicm.matter_id "
      " WHERE eicm.external_invoice_id = invoices.id "
      f"  AND {_not_deleted_sql('eicm.is_deleted')} "
      "  AND ("
      "   UPPER(TRIM(COALESCE(eicm.our_ref, ''))) = UPPER(TRIM(?)) "
      "   OR UPPER(TRIM(COALESCE(m.our_ref, ''))) = UPPER(TRIM(?)) "
      "   OR UPPER(TRIM(COALESCE(m.old_our_ref, ''))) = UPPER(TRIM(?)) "
      "   OR UPPER(TRIM(COALESCE(m.your_ref, ''))) = UPPER(TRIM(?))"
      "  )"
      "))"
    )
    params.append(ipm_case_ref)
    params.append(ipm_case_ref)
    params.append(ipm_case_ref)
    params.append(ipm_case_ref)
    params.append(ipm_case_ref)

  page = _safe_int(request.args.get("page", 1), 1, 1, None)

  where_sql = (" WHERE " + " AND ".join(where)) if where else ""
  if sort == "number_desc":
    order_clause = "scored.number DESC, scored.id DESC"
  elif sort == "issue_date_asc":
    order_clause = "scored.issue_date ASC, scored.id ASC"
  elif sort == "issue_date_desc":
    order_clause = "scored.issue_date DESC, scored.id DESC"
  elif sort == "due_date_asc":
    order_clause = "scored.due_date ASC, scored.id ASC"
  elif sort == "due_date_desc":
    order_clause = "scored.due_date DESC, scored.id DESC"
  elif sort == "amount_asc":
    order_clause = "scored.total_display ASC, scored.id ASC"
  else:
    order_clause = "scored.total_display DESC, scored.id DESC"

  sql_base = f"""
   SELECT invoices.*, clients.name as client_name, business_profile.name as business_name,
   -- Service/Admin: standard amount (non-estimated)
   (SELECT COALESCE(SUM(line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0)), 0)
    FROM line_items
    WHERE line_items.invoice_id = invoices.id AND line_items.item_type = 'service'
     AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as service_total,
   (SELECT COALESCE(SUM(line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0)), 0)
    FROM line_items
    WHERE line_items.invoice_id = invoices.id AND line_items.item_type = 'admin'
     AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as admin_total,
   -- Foreign: prefer FX metadata when present; otherwise fallback to unit_price
   (SELECT COALESCE(SUM(
     CASE WHEN COALESCE(line_items.fx_rate_used, 0) > 0 THEN
        (COALESCE(line_items.fx_fee,0) + COALESCE(line_items.fx_gov,0))
        * COALESCE(line_items.fx_rate_used, 0)
        * (1 + COALESCE(line_items.fx_markup,0)/100.0)
     ELSE
        (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
     END
   ), 0)
    FROM line_items
    WHERE line_items.invoice_id = invoices.id AND line_items.item_type = 'foreign'
     AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as foreign_total,
   -- Foreign taxable subtotal (prefer FX when present)
   (SELECT COALESCE(SUM(
     CASE WHEN COALESCE(line_items.fx_rate_used, 0) > 0 THEN
        (COALESCE(line_items.fx_fee,0) + COALESCE(line_items.fx_gov,0))
        * COALESCE(line_items.fx_rate_used, 0)
        * (1 + COALESCE(line_items.fx_markup,0)/100.0)
     ELSE
        (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
     END
   ), 0)
    FROM line_items
    WHERE line_items.invoice_id = invoices.id AND line_items.item_type = 'foreign'
     AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
     AND COALESCE(line_items.is_taxable,0)=1) as foreign_taxable_total
   FROM invoices
   JOIN clients ON clients.id=invoices.client_id
   LEFT JOIN business_profile ON business_profile.id=invoices.business_profile_id
   {where_sql}"""

  vat_multiplier_sql = (
    "(CASE WHEN COALESCE(base.vat_rate, 0) > 1 "
    "THEN COALESCE(base.vat_rate, 0) / 100.0 "
    "ELSE COALESCE(base.vat_rate, 0) END)"
  )
  sql_scored = f"""
   SELECT base.*,
       ({vat_multiplier_sql} * (COALESCE(base.service_total, 0) + COALESCE(base.foreign_taxable_total, 0))) AS tax_display,
       (
        COALESCE(base.service_total, 0)
        + COALESCE(base.admin_total, 0)
        + COALESCE(base.foreign_total, 0)
        + ({vat_multiplier_sql} * (COALESCE(base.service_total, 0) + COALESCE(base.foreign_taxable_total, 0)))
       ) AS total_display
   FROM (
    {sql_base}
   ) base
  """

  amount_where = []
  amount_params = []
  if not (use_settlement_filter and target_bp_id is not None):
    if min_amount_v is not None:
      amount_where.append("scored.total_display >= ?")
      amount_params.append(float(min_amount_v))
    if max_amount_v is not None:
      amount_where.append("scored.total_display <= ?")
      amount_params.append(float(max_amount_v))
  amount_where_sql = (" WHERE " + " AND ".join(amount_where)) if amount_where else ""
  sql_filtered = f"SELECT scored.* FROM (\n{sql_scored}\n) scored{amount_where_sql}"
  sql_list = f"{sql_filtered} ORDER BY {order_clause}"
  list_params = params + amount_params
  total_count = 0
  total_pages = 1

  # basis='settlement' + Business profile Filters : All  Settlement business profile to Filters Pagination
  if (use_settlement_filter and target_bp_id is not None) or is_compact_q:
    # Safety: avoid huge fetchall() (OOM / long latency). Cap scan size.
    max_scan = _safe_int(
      current_app.config.get("INVOICE_LIST_PY_FILTER_MAX_ROWS", 20000),
      20000,
      1000,
      200000,
    )
    scan_limit = int(max_scan) + 1
    db_invoices = conn.execute(sql_list + " LIMIT ?", list_params + [scan_limit]).fetchall()
    if len(db_invoices) > max_scan:
      db_invoices = db_invoices[:max_scan]
      flash(
        f"Search  days ( {max_scan}items). /Filters .",
        "warning",
      )
  else:
    total_count = conn.execute(
      f"SELECT COUNT(*) FROM ({sql_filtered}) counted",
      list_params,
    ).fetchone()[0]
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
      page = total_pages
    offset = (page - 1) * per_page
    db_invoices = conn.execute(
      sql_list + " LIMIT ? OFFSET ?",
      list_params + [per_page, offset],
    ).fetchall()

  # Enrich rows with deposit/outstanding and additional columns for list view
  invoices = []

  def _has_settlement_share(drow: dict, target_bp: int) -> bool:
    """Settlement Filters: settlement_meta target_bp  (>0)to True.
    settlement_meta   , Issuing business profile=target_bp 100% .
    """
    meta_s = drow.get("settlement_meta")
    if meta_s:
      parsed = None
      try:
        parsed = safe_json_parse(meta_s)
      except Exception:
        parsed = None
      if isinstance(parsed, list) and not is_default_settlement_split(
        parsed,
        drow.get("business_profile_id"),
      ):
        for rec in parsed:
          try:
            bpv = int(rec.get("business_profile_id"))
            pctv = float(rec.get("percent"))
          except Exception:
            continue
          if pctv <= 0:
            continue
          if bpv == target_bp:
            return True
    # settlement_meta   : Issuing business profile 100% 
    try:
      return int(drow.get("business_profile_id") or 0) == target_bp
    except Exception:
      return False

  for r in db_invoices:
    drow = row_to_dict(r)
    settlement_share_ratio = None
    if use_settlement_filter and target_bp_id is not None:
      settlement_share_ratio = _get_settlement_share_ratio(drow, target_bp_id)
      if settlement_share_ratio <= 0:
        continue
    # Normalize split status for rendering (some old rows may still be legacy-only)
    try:
      bs, ps, st = _normalize_invoice_status_fields(drow)
      drow["billing_status"] = bs
      drow["payment_status"] = ps
      drow["status"] = st
      drow["payment_effectively_complete"] = (
        1 if _is_payment_effectively_complete(drow) else 0
      )
      enrich_invoice_tax_issue_fields(drow)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.list_invoices.normalize_status_fields",
        log_key="billing_invoices.invoices.list_invoices.normalize_status_fields",
        log_window_seconds=300,
      )
    cur_code = (drow.get("currency") or "USD").upper()
    # Compute additional charges (admin + foreign)
    try:
      drow["additional_charges"] = float(drow.get("admin_total") or 0.0) + float(
        drow.get("foreign_total") or 0.0
      )
    except Exception:
      drow["additional_charges"] = 0.0
    # Prefer SQL-scored display totals so list sorting/filtering and rendered totals stay identical.
    try:
      drow["tax"] = float(drow.get("tax_display") or 0.0)
      drow["total"] = float(drow.get("total_display") or 0.0)
    except Exception:
      try:
        vt = float(drow.get("vat_rate") or 0.0)
        vm = (vt / 100.0) if vt > 1 else vt
        svc = float(drow.get("service_total") or 0.0)
        adm = float(drow.get("admin_total") or 0.0)
        frn = float(drow.get("foreign_total") or 0.0)
        frn_taxable = float(drow.get("foreign_taxable_total") or 0.0)
        tax_dynamic = vm * (svc + frn_taxable)
        total_display = svc + adm + frn + tax_dynamic
        drow["tax"] = tax_dynamic
        drow["total"] = total_display
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices.list_invoices.recompute_totals",
          log_key="billing_invoices.invoices.list_invoices.recompute_totals",
          log_window_seconds=300,
        )
    # Deposit info from payment_meta (USD only)
    if settlement_share_ratio is not None:
      for amount_key in (
        "service_total",
        "admin_total",
        "foreign_total",
        "foreign_taxable_total",
        "tax",
        "total",
      ):
        try:
          drow[amount_key] = float(drow.get(amount_key) or 0.0) * settlement_share_ratio
        except (TypeError, ValueError):
          drow[amount_key] = 0.0
      try:
        drow["additional_charges"] = float(drow.get("admin_total") or 0.0) + float(
          drow.get("foreign_total") or 0.0
        )
      except (TypeError, ValueError):
        drow["additional_charges"] = 0.0
      if min_amount_v is not None and float(drow.get("total") or 0.0) < float(min_amount_v):
        continue
      if max_amount_v is not None and float(drow.get("total") or 0.0) > float(max_amount_v):
        continue

    deposit = 0
    deposit_date = ""
    try:
      meta_s = drow.get("payment_meta")
      if meta_s and cur_code == "USD":
        meta = safe_json_parse(meta_s, {})
        # Reuse helper to parse int-like amounts
        try:
          deposit = _parse_int_amount_usd(meta.get("deposit"))
        except Exception:
          deposit = 0
        deposit_date = str(meta.get("date") or "")
    except Exception:
      deposit = 0
      deposit_date = ""
    drow["deposit_amount"] = deposit
    drow["deposit_date"] = deposit_date
    try:
      drow["outstanding"] = max(0.0, float(drow.get("total") or 0.0) - float(deposit or 0))
    except Exception:
      drow["outstanding"] = float(drow.get("total") or 0.0)

    # Settlement summary for list view (Settlement business profile Display)
    settlement_summary = None
    try:
      meta_s = drow.get("settlement_meta")
    except Exception:
      meta_s = None
    if meta_s:
      try:
        parsed = safe_json_parse(meta_s)
      except Exception:
        parsed = None
      if isinstance(parsed, list) and not is_default_settlement_split(
        parsed,
        drow.get("business_profile_id"),
      ):
        parts = []
        for rec in parsed:
          try:
            bpv = int(rec.get("business_profile_id"))
            pctv = float(rec.get("percent"))
          except Exception:
            continue
          if pctv <= 0:
            continue
          name = bp_name_map.get(bpv) or drow.get("business_name") or "-"
          if pctv.is_integer():
            pct_str = f"{int(pctv)}%"
          else:
            pct_str = f"{pctv:.1f}%"
          parts.append(f"{name} {pct_str}")
        if parts:
          settlement_summary = ", ".join(parts)
    if not settlement_summary:
      settlement_summary = drow.get("business_name") or "-"
    drow["settlement_summary"] = settlement_summary

    # basis='settlement' : target_bp Settlement  Invoice 
    if use_settlement_filter and target_bp_id is not None:
      if settlement_share_ratio is None and not _has_settlement_share(drow, target_bp_id):
        continue

    invoices.append(drow)

  if is_compact_q:
    q_compact = to_compact(q)
    filtered = []
    for drow in invoices:
      text = " ".join(
        [
          str(drow.get("number") or ""),
          str(drow.get("internal_reference") or ""),
          str(drow.get("client_name") or ""),
          str(drow.get("notes") or ""),
        ]
      )
      if q_compact in to_compact(text):
        filtered.append(drow)
    invoices = filtered

  if (use_settlement_filter and target_bp_id is not None) or is_compact_q:
    if use_settlement_filter and target_bp_id is not None:
      if sort == "amount_asc":
        invoices.sort(key=lambda row: (float(row.get("total") or 0.0), int(row["id"])))
      elif sort == "amount_desc":
        invoices.sort(
          key=lambda row: (float(row.get("total") or 0.0), int(row["id"])),
          reverse=True,
        )
    total_count = len(invoices)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
      page = total_pages
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    invoices = invoices[start_idx:end_idx]

  try:
    ensure_invoice_attachment_role_schema(conn)
    invoice_ids = [int(row.get("id")) for row in invoices if row.get("id") is not None]
    attached_ids: set[int] = set()
    if invoice_ids:
      placeholders = ",".join("?" for _ in invoice_ids)
      rows = conn.execute(
        f"""
        SELECT invoice_id
         FROM invoice_attachments
         WHERE invoice_id IN ({placeholders})
          AND COALESCE(role, 'general')=?
         GROUP BY invoice_id
        """,
        invoice_ids + [FOREIGN_REMITTANCE_PROOF_ROLE],
      ).fetchall()
      attached_ids = {int(row_get(r, "invoice_id", 0, default=0) or 0) for r in rows}
    for row in invoices:
      try:
        row["foreign_remittance_proof_attached"] = (
          1 if int(row.get("id") or 0) in attached_ids else 0
        )
      except Exception:
        row["foreign_remittance_proof_attached"] = 0
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.list_invoices.attach_remittance_proof_flags",
      log_key="billing_invoices.invoices.list_invoices.attach_remittance_proof_flags",
      log_window_seconds=300,
    )

  try:
    _attach_invoice_case_links_for_list(conn, invoices)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.list_invoices.attach_case_links",
      log_key="billing_invoices.invoices.list_invoices.attach_case_links",
      log_window_seconds=300,
    )
  conn.close()

  return render_template(
    "invoices_list.html",
    invoices=invoices,
    bp=bp_row,
    all_profiles=all_profiles,
    currencies=currencies,
    page=page,
    per_page=per_page,
    total_count=total_count,
    total_pages=total_pages,
    basis=basis,
  )


@bp.route("/create", methods=["GET", "POST"])
def create_invoice_alias():
  # Backward-compatible alias for older links/tools:
  # `/accounting/invoice-system/invoices/create` -> `/.../invoices/new`
  return new_invoice()


@bp.route("/new", methods=["GET", "POST"])
def new_invoice():
  state = load_invoice_create_page_state(
    get_db(),
    ensure_external_invoice_case_map=_ensure_external_invoice_case_map,
  )
  hooks = InvoiceCreateHooks(
    ensure_external_invoice_case_map=_ensure_external_invoice_case_map,
    safe_int=_safe_int,
    parse_settlement_splits=_parse_settlement_splits,
    normalize_invoice_date=_normalize_invoice_date,
    parse_amount_to_minor=_parse_amount_to_minor,
    compute_billing_payment_from_status=_compute_billing_payment_from_status,
    derive_legacy_status_from_split=_derive_legacy_status_from_split,
    resolve_invoice_case_target=_resolve_invoice_case_target,
    sync_legacy_status=_sync_legacy_status,
  )
  if request.method == "POST":
    return handle_invoice_create_submission(
      state,
      hooks,
      sync_clients_bidirectional=sync_clients_bidirectional,
    )
  return render_invoice_create_form(state)


@bp.route("/<int:invoice_id>")
def view_invoice(invoice_id):
  conn = get_db()
  try:
    invoice = _load_invoice_with_client(conn, int(invoice_id))
    if not invoice:
      abort(404)
    invoice["payment_effectively_complete"] = (
      1 if _is_payment_effectively_complete(invoice) else 0
    )

    _ensure_external_invoice_case_map(conn)

    items = _load_invoice_items(conn, int(invoice_id))
    all_attachments = _load_invoice_attachments(conn, int(invoice_id))
    attachments, foreign_remittance_attachments = _split_invoice_attachments_by_role(
      all_attachments
    )
    payment_logs_enriched, billing_logs_enriched = _load_invoice_recent_logs(
      conn,
      invoice_id=int(invoice_id),
      invoice_number=invoice.get("number"),
    )
    settlement_details = _build_invoice_settlement_details(invoice)
    deposit_context = _build_invoice_deposit_context(conn, int(invoice_id), invoice)
    bp_row = _resolve_invoice_business_profile(invoice)
    invoice_lang, outgoing_mode = _resolve_invoice_render_options(invoice)
    _apply_invoice_client_language(invoice, invoice_lang)
    foreign_remittance_required = invoice_requires_foreign_remittance_proof(
      conn,
      int(invoice_id),
      invoice,
    )
    foreign_remittance_satisfied = (
      (not foreign_remittance_required)
      or bool(foreign_remittance_attachments)
      or invoice_has_foreign_remittance_proof(conn, int(invoice_id))
    )
    case_links = _load_invoice_case_links(conn, int(invoice_id), invoice)
    internal_reference_suggestions = _load_invoice_internal_reference_suggestions(
      conn,
      invoice=invoice,
      case_links=case_links,
    )
    invoice_revisions = _load_invoice_revisions(conn, int(invoice_id))

    return render_template(
      "invoice_view.html",
      invoice=invoice,
      items=items,
      biz_profile=bp_row,
      invoice_lang=invoice_lang,
      attachments=attachments,
      foreign_remittance_attachments=foreign_remittance_attachments,
      foreign_remittance_required=foreign_remittance_required,
      foreign_remittance_satisfied=foreign_remittance_satisfied,
      foreign_remittance_attachment_role=FOREIGN_REMITTANCE_PROOF_ROLE,
      foreign_remittance_required_message=foreign_remittance_required_message(),
      outgoing_mode=outgoing_mode,
      payment_logs=payment_logs_enriched,
      billing_logs=billing_logs_enriched,
      case_links=case_links,
      internal_reference_suggestions=internal_reference_suggestions,
      settlement_details=settlement_details,
      invoice_revisions=invoice_revisions,
      is_revision_view=False,
      revision_no=None,
      revision_created_at=None,
      revision_created_by_username=None,
      revision_file_name=None,
      **deposit_context,
    )
  finally:
    conn.close()


@bp.post("/<int:invoice_id>/revisions/ensure-print")
def ensure_print_revision(invoice_id: int):
  """Ensure a stored invoice document revision exists for the current content.

  Used by the Print/PDF flow to generate a stable `revN` filename + history.
  """
  conn = get_db()
  try:
    lang = (request.args.get("lang") or request.form.get("lang") or "").strip().lower() or None
    if lang not in (None, "en"):
      lang = None
    invoice_row = conn.execute(
      "SELECT is_outgoing FROM invoices WHERE id=?",
      (int(invoice_id),),
    ).fetchone()
    outgoing = _stored_outgoing_mode(invoice_row) if invoice_row else None

    try:
      rev_no, created, file_stem = _ensure_invoice_revision_for_print(
        conn,
        int(invoice_id),
        render_lang=lang,
        render_outgoing=outgoing,
        source="print",
      )
    except RuntimeError as exc:
      # Most common: migrations not applied yet.
      msg = str(exc) or "The table required for version history is not ready."
      return (
        jsonify(
          {
            "ok": False,
            "error": {
              "code": "schema_missing",
              "message": msg,
            },
          }
        ),
        500,
      )

    title = str(file_stem or "").strip() or f"invoice_{int(invoice_id)}"

    # : revision  
    if created:
      try:
        log_audit(
          "invoice.revision.create",
          "invoice",
          int(invoice_id),
          json.dumps(
            {"revision_no": int(rev_no), "file_name": title},
            ensure_ascii=False,
          ),
        )
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.invoices.ensure_print_revision.log_audit",
          log_key="billing_invoices.invoices.ensure_print_revision.log_audit",
          log_window_seconds=300,
        )

    return jsonify(
      {
        "ok": True,
        "revision_no": int(rev_no),
        "created": bool(created),
        "title": title,
        "file_name": f"{title}.pdf",
      }
    )
  except LookupError:
    return (
      jsonify(
        {
          "ok": False,
          "error": {"code": "not_found", "message": "Invoice not found."},
        }
      ),
      404,
    )
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.ensure_print_revision",
      log_key="billing_invoices.invoices.ensure_print_revision",
      log_window_seconds=300,
    )
    return (
      jsonify(
        {
          "ok": False,
          "error": {"code": "server_error", "message": "Process Error ."},
        }
      ),
      500,
    )
  finally:
    try:
      conn.close()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.ensure_print_revision.close",
        log_key="billing_invoices.invoices.ensure_print_revision.close",
        log_window_seconds=300,
      )


@bp.route("/<int:invoice_id>/revisions/<int:revision_no>")
def view_invoice_revision(invoice_id: int, revision_no: int):
  conn = get_db()
  try:
    if not _table_exists(conn, "invoice_revisions"):
      abort(404)

    row = conn.execute(
      """
      SELECT r.*, u.username AS created_by_username
      FROM invoice_revisions r
      LEFT JOIN users u ON u.id = r.created_by
      WHERE r.invoice_id=? AND r.revision_no=?
      """,
      (int(invoice_id), int(revision_no)),
    ).fetchone()
    if not row:
      abort(404)
    rev = row_to_dict(row)
    payload = None
    try:
      payload = safe_json_parse(rev.get("snapshot_json"), {})
    except Exception:
      payload = None
    if not isinstance(payload, dict):
      abort(500, "Invoice version data is damaged.")

    invoice = payload.get("invoice") or {}
    items = payload.get("items") or []
    bp_row = payload.get("biz_profile") or {}
    if (
      not isinstance(invoice, dict)
      or not isinstance(items, list)
      or not isinstance(bp_row, dict)
    ):
      abort(500, "Invoice version data format is invalid.")

    # Language can be previewed via URL; outgoing layout follows the stored invoice type.
    invoice_lang = (
      request.args.get("lang") or rev.get("render_lang") or invoice.get("language") or "en"
    )
    _apply_invoice_client_language(invoice, invoice_lang)
    try:
      if "is_outgoing" in invoice:
        outgoing_mode = _stored_outgoing_mode(invoice)
      elif rev.get("render_outgoing") is not None:
        outgoing_mode = _coerce_outgoing_mode(rev.get("render_outgoing"))
      else:
        outgoing_mode = _coerce_outgoing_mode(request.args.get("outgoing"))
    except Exception:
      outgoing_mode = False

    # Revision selector list
    invoice_revisions = []
    try:
      rows = conn.execute(
        """
        SELECT r.revision_no, r.file_name, r.created_at, r.created_by, r.source,
            r.render_lang, r.render_outgoing, u.username AS created_by_username
        FROM invoice_revisions r
        LEFT JOIN users u ON u.id = r.created_by
        WHERE r.invoice_id=?
        ORDER BY r.revision_no DESC
        """,
        (int(invoice_id),),
      ).fetchall()
      invoice_revisions = [row_to_dict(r) for r in (rows or [])]
    except Exception:
      invoice_revisions = []

    return render_template(
      "invoice_view.html",
      invoice=invoice,
      items=items,
      biz_profile=bp_row,
      invoice_lang=invoice_lang,
      attachments=[],
      outgoing_mode=outgoing_mode,
      payment_logs=[],
      billing_logs=[],
      case_links=[],
      settlement_details=None,
      deposit_balance_minor=None,
      deposit_apply_rows=[],
      deposit_applied_minor=0,
      deposit_outstanding_minor=None,
      invoice_revisions=invoice_revisions,
      is_revision_view=True,
      revision_no=int(revision_no),
      revision_created_at=rev.get("created_at"),
      revision_created_by_username=rev.get("created_by_username"),
      revision_file_name=rev.get("file_name"),
    )
  finally:
    try:
      conn.close()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.view_invoice_revision.close",
        log_key="billing_invoices.invoices.view_invoice_revision.close",
        log_window_seconds=300,
      )


@bp.route("/<int:invoice_id>/case-links/add", methods=["POST"])
@role_required("admin", "staff")
def add_invoice_case_link(invoice_id: int):
  conn = get_db()
  _ensure_external_invoice_case_map(conn)

  ref = (request.form.get("case_ref") or "").strip()
  if not ref:
    conn.close()
    flash("Matter ID/Our Ref/Former Our Ref/Your Ref(Internal reference) enter.", "warning")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))

  ref_inputs = _extract_invoice_case_ref_inputs(ref)
  existing_matter_ids = _load_existing_invoice_case_link_ids(conn, invoice_id)
  inserted_links: list[dict[str, str]] = []
  already_linked: list[dict[str, str]] = []
  ambiguous_inputs: list[dict[str, object]] = []
  not_found_inputs: list[str] = []

  for ref_input in ref_inputs:
    resolved = resolve_matter_identifier(conn, ref_input)
    status = str(resolved.get("status") or "").strip()
    if status != "ok":
      if status == "ambiguous":
        ambiguous_inputs.append(
          {
            "input": ref_input,
            "matches": list(resolved.get("matches") or []),
          }
        )
      else:
        not_found_inputs.append(ref_input)
      continue

    matter_id = str(resolved.get("matter_id") or "").strip()
    our_ref = str(resolved.get("our_ref") or "").strip() or matter_id
    if not matter_id:
      not_found_inputs.append(ref_input)
      continue
    if matter_id in existing_matter_ids:
      already_linked.append({"matter_id": matter_id, "our_ref": our_ref})
      continue
    try:
      conn.execute(
        "INSERT INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        (matter_id, our_ref, int(invoice_id)),
      )
      conn.execute(
        """
        UPDATE external_invoice_case_map
          SET is_deleted=FALSE,
            deleted_at=NULL,
            deleted_by=NULL,
            delete_reason=NULL,
            deleted_op_id=NULL
         WHERE external_invoice_id=?
          AND matter_id=?
        """,
        (int(invoice_id), matter_id),
      )
      existing_matter_ids.add(matter_id)
      inserted_links.append({"matter_id": matter_id, "our_ref": our_ref})
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.add_invoice_case_link.insert",
        log_key="billing_invoices.invoices.add_invoice_case_link.insert",
        log_window_seconds=300,
      )

  if inserted_links:
    try:
      conn.commit()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.add_invoice_case_link.commit",
        log_key="billing_invoices.invoices.add_invoice_case_link.commit",
        log_window_seconds=300,
      )
    try:
      _set_invoice_primary_case(conn, invoice_id)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.invoices.add_invoice_case_link.set_primary",
        log_key="billing_invoices.invoices.add_invoice_case_link.set_primary",
        log_window_seconds=300,
      )

  conn.close()

  if inserted_links:
    if len(inserted_links) == 1:
      flash("Matter Link.", "success")
    else:
      flash(f"Matter {len(inserted_links)}items Link.", "success")
  if already_linked:
    sample = ", ".join(
      [
        str(item.get("our_ref") or item.get("matter_id") or "-")
        for item in already_linked[:3]
      ]
    )
    msg = f" Link Matter {len(already_linked)}items items."
    if sample:
      msg = f"{msg} {sample}"
    flash(msg, "info")
  if ambiguous_inputs:
    sample_candidates = []
    for item in ambiguous_inputs[:2]:
      matches = item.get("matches") or []
      sample = ", ".join(
        [
          f"{(m.get('our_ref') or m.get('matter_id') or '-')}({m.get('matter_id')})"
          for m in matches[:3]
        ]
      )
      label = str(item.get("input") or "").strip()
      sample_candidates.append(f"{label}: {sample}" if sample else label)
    msg = "Inputvalue days match Matter items items."
    if sample_candidates:
      msg = f"{msg} : {' / '.join(sample_candidates)}"
    flash(msg, "warning")
  if not_found_inputs:
    labels = ", ".join(not_found_inputs[:3])
    msg = "Inputvalue days Matter  items."
    if labels:
      msg = f"{msg} {labels}"
    flash(msg, "warning" if inserted_links else "danger")

  if not inserted_links and not already_linked and not ambiguous_inputs and not not_found_inputs:
    flash(
      "Matter not found. Matter ID/Our Ref/Former Our Ref/Your Ref(Internal reference) Confirm.",
      "danger",
    )
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/case-links/remove", methods=["POST"])
@role_required("admin", "staff")
def remove_invoice_case_link(invoice_id: int):
  conn = get_db()
  _ensure_external_invoice_case_map(conn)

  matter_id = (request.form.get("matter_id") or "").strip()
  if not matter_id:
    conn.close()
    flash("matter_id required.", "warning")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))

  try:
    conn.execute(
      "DELETE FROM external_invoice_case_map WHERE external_invoice_id=? AND matter_id=?",
      (int(invoice_id), matter_id),
    )
    conn.commit()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.remove_invoice_case_link.delete",
      log_key="billing_invoices.invoices.remove_invoice_case_link.delete",
      log_window_seconds=300,
    )

  try:
    _set_invoice_primary_case(conn, invoice_id)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices.remove_invoice_case_link.set_primary",
      log_key="billing_invoices.invoices.remove_invoice_case_link.set_primary",
      log_window_seconds=300,
    )

  conn.close()
  flash("Link .", "success")
  return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))


def _parse_amount_to_minor(amount_raw: str, currency: str) -> int:
  s = str(amount_raw or "").strip().replace(",", "")
  if not s:
    raise ValueError("Amount enter.")
  return int(to_minor(Decimal(s), currency))


def _normalize_invoice_date(value):
  if not value:
    return None
  s = str(value).strip()
  if not s:
    return None
  try:
    return date.fromisoformat(s[:10]).isoformat()
  except ValueError:
    # Try other formats below.
    pass
  for fmt in ("%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
    try:
      return datetime.strptime(s, fmt).date().isoformat()
    except Exception:
      continue
  return None


@bp.route("/fx_rates", methods=["GET"])  # Exchange-rate times, one day
def fx_rates():
  """Return FX rates (cached up to 1 hour).
  Query:
   - currencies: comma-separated like "USD,JPY,EUR". Defaults to USD,JPY,EUR,CNY.
  Response: { success: true, rates: { 'USD': {base_rate, sending, receiving, ...}, ... } }
  """
  currencies = (request.args.get("currencies") or "USD,JPY,EUR,CNY").upper()
  codes = [c.strip() for c in currencies.split(",") if c.strip()]
  try:
    # 1) Try 1-hour DB cache
    cached_all = get_fx_rates_cache(max_age_seconds=3600, source="sample")
    if cached_all and isinstance(cached_all, dict):
      all_rates = cached_all
    else:
      # 2) Load built-in sample rates once, store in cache
      all_rates = fetch_sample_rates_all()
      if not all_rates:
        # Fallback: per-code detail page
        all_rates = fetch_sample_rates(codes)
      if all_rates:
        set_fx_rates_cache(all_rates, source="sample")

    # 3) Return all or filter by requested codes
    if any(c in ("ALL", "*") for c in codes):
      rates = all_rates
    else:
      rates = {ccy: all_rates.get(ccy) for ccy in codes if ccy in all_rates}
    return jsonify({"success": True, "rates": rates})
  except Exception as e:
    return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
def edit_invoice(invoice_id):
  conn = get_db()
  try:
    state = load_invoice_edit_page_state(conn, invoice_id)
  except LookupError:
    conn.close()
    abort(404)
  invoice = state.invoice
  # Lock edits for finalized billing statuses.
  try:
    billing_status = _resolve_billing_status(invoice)
  except Exception:
    billing_status = (invoice.get("billing_status") or "").strip().lower()
  legacy_status = (invoice.get("status") or "").strip().lower()
  if billing_status in ("tax_issued", "cash_issued", "processed") or legacy_status in (
    "tax_issued",
    "cash_issued",
    "processed",
  ):
    conn.close()
    abort(403, "tax-recorded status Invoice Edit not available. ( change status required)")

  # Paid Invoice Edit Amount/Payment status match ,
  # Payment status 'Payment pending'to ( ) Edit .
  try:
    payment_status = _resolve_payment_status(invoice)
  except Exception:
    payment_status = (invoice.get("payment_status") or "").strip().lower()
  try:
    payment_verified = int(invoice.get("payment_verified") or 0)
  except Exception:
    payment_verified = 0
  if payment_status == "paid" or payment_verified == 1:
    msg = "Paid Invoice Edit not available. Payment status (Payment pending) Edit."
    conn.close()
    flash(msg, "error")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
  hooks = InvoiceEditHooks(
    table_exists=_table_exists,
    ensure_invoice_revision_for_print=_ensure_invoice_revision_for_print,
    safe_int=_safe_int,
    parse_settlement_splits=_parse_settlement_splits,
    normalize_invoice_date=_normalize_invoice_date,
    parse_amount_to_minor=_parse_amount_to_minor,
    compute_billing_payment_from_status=_compute_billing_payment_from_status,
    derive_legacy_status_from_split=_derive_legacy_status_from_split,
    sync_legacy_status=_sync_legacy_status,
  )
  if request.method == "POST":
    return handle_invoice_edit_submission(state, hooks, billing_status=billing_status)
  response = render_invoice_edit_form(state)
  conn.close()
  return response


@bp.route("/<int:invoice_id>/delete", methods=["POST"])
@role_required("admin", "staff")
def delete_invoice(invoice_id):
  conn = get_db()
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
      [invoice_id],
      hooks,
      created_by_user_id=(user["id"] if user else None),
      skip_missing=False,
      error_context="billing_invoices.invoices.delete_invoice",
    )
  except InvoiceDeleteNotFoundError:
    conn.close()
    abort(404)
  except InvoiceDeleteBlockedError as exc:
    conn.close()
    flash(exc.message, "error")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
  except InvoiceDeleteExecutionError as exc:
    conn.close()
    flash(exc.message, "error")
    return redirect(url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id))
  conn.close()

  snapshot = result.snapshots[0]
  log_invoice_delete_cancel_audits(
    result.canceled_deposit_entries,
    error_context="billing_invoices.invoices.delete_invoice.cancel_deposit_before_delete",
  )
  record_single_invoice_delete_operation(snapshot)
  log_audit("invoice.delete", "invoice", invoice_id, f'{{"number": "{snapshot["number"]}"}}')

  flash("Invoice Delete.", "success")
  # Redirect back to the previous list URL to preserve filters/page
  default_url = url_for("billing_invoices.invoices.list_invoices")
  invoice_path = url_for("billing_invoices.invoices.view_invoice", invoice_id=invoice_id)
  next_url = _safe_same_host_referrer(request.referrer, default_url, invoice_path)
  return redirect(next_url)


# ==== Payment verification/Save/ Process ====
def _parse_int_amount_usd(val: str) -> int:
  try:
    return int(str(val or "0").replace(",", "").replace(" ", ""))
  except Exception:
    return 0


# Register route modules.
from . import ( # noqa: F401,E402
  invoices_attachments,
  invoices_export,
  invoices_llm,
  invoices_logs,
  invoices_payments,
  invoices_status,
)
