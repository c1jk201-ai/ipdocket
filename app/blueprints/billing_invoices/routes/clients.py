import json
import mimetypes
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any, List, Optional

from flask import (
  Blueprint,
  abort,
  current_app,
  flash,
  g,
  jsonify,
  redirect,
  render_template,
  request,
  send_file,
  url_for,
)
from flask_login import current_user

from app.extensions import db
from app.services.billing.invoice_bridge import (
  InvoiceBridgeError,
  ensure_ipm_client_link_from_invoice_client,
)
from app.services.billing.utils import is_compact_query, sql_ci_contains_any, to_compact, to_minor
from app.services.client.background_jobs import (
  enqueue_invoice_client_post_save,
  invoice_client_search_tags_fast,
)
from app.services.client.client_tagging import build_client_search_tags_text
from app.services.core.llm_runtime import get_openai_api_key
from app.services.uploads.intake_security import (
  UploadSecurityError,
  scan_upload_path,
  validate_upload_path,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_admin, is_invoice_manager
from app.utils.upload_io import UploadTooLarge, resolve_first_positive_int
from app.utils.upload_io import save_upload_stream as _save_upload_stream_impl

from ..auth import get_current_user, log_audit, role_required
from ..db import (
  build_client_deposit_audit_meta,
  get_all_business_profiles,
  get_client_deposit_balances_minor,
  get_db,
  insert_client_deposit_ledger_entry,
  unified_clients_enabled,
)
from .admin import _create_backup_file, _write_backup_meta

try:
  from client_sync_sqlite import sync_clients_bidirectional
except Exception:
  sync_clients_bidirectional = None

bp = Blueprint("clients", __name__)


# ==============================================================================
# 1. Helper Functions: Database & Utils
# ==============================================================================


def _get_row_value(row: Any, key: str, index: Optional[int] = None, default: Any = None) -> Any:
  """sqlite3.Row(Dict) Tuple  USD value """
  if row is None:
    return default
  try:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
      try:
        return mapping[key]
      except Exception:
        return default
  except Exception:
    mapping = None
  if callable(getattr(row, "keys", None)):
    try:
      return row[key]
    except Exception:
      return default
  if index is not None:
    try:
      return row[index] if len(row) > index else default
    except Exception:
      return default
  return default


def _get_client_or_404(conn, client_id: int):
  """ times 404 """
  client = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
  if not client:
    conn.close()
    abort(404)
  return client


def _normalize_string(s: Optional[str]) -> str:
  """column (Search/)"""
  if not s:
    return ""
  return "".join(ch for ch in s.lower().strip() if ch.isalnum())


def _parse_amount_to_minor(amount_raw: str, currency: str) -> int:
  s = str(amount_raw or "").strip().replace(",", "")
  if not s:
    raise ValueError("Amount enter.")
  try:
    return int(to_minor(Decimal(s), currency))
  except Exception:
    raise ValueError("Amount is invalid.")


def _safe_int(v, default=None, min_=None, max_=None):
  try:
    if v is None:
      return default
    n = int(v)
    if min_ is not None and n < min_:
      n = min_
    if max_ is not None and n > max_:
      n = max_
    return n
  except Exception:
    return default


# ==============================================================================
# 2. Helper Functions: File System & Naming
# ==============================================================================


def _client_attachment_dir(client_id: int) -> str:
  """ File  (if not available )"""
  base = current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients")
  path = os.path.join(base, f"client_{client_id}")
  os.makedirs(path, exist_ok=True)
  return path


def _allowed_attachment_exts() -> set[str]:
  allowed = current_app.config.get(
    "ALLOWED_ATTACHMENT_EXTENSIONS",
    {"pdf", "png", "jpg", "jpeg", "gif", "zip", "hwp", "xlsx", "docx"},
  )
  if isinstance(allowed, str):
    values = re.split(r"[,\s]+", allowed)
  elif isinstance(allowed, (list, tuple, set)):
    values = [str(value) for value in allowed]
  else:
    values = []
  return {value.strip().lower().lstrip(".") for value in values if value.strip()}


def _allowed_attachment_exts_with_dot() -> set[str]:
  return {f".{ext}" for ext in _allowed_attachment_exts() if ext}


def _allowed_attachment(filename: str) -> bool:
  """ """
  if not filename or "." not in filename:
    return False
  ext = filename.rsplit(".", 1)[1].lower()
  return ext in _allowed_attachment_exts()


def _remove_file_if_exists(path: str | None) -> None:
  if not path:
    return
  try:
    if os.path.exists(path):
      os.remove(path)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.remove_file_if_exists",
      log_key="billing_invoices.clients.remove_file_if_exists",
      log_window_seconds=300,
    )


def _validate_stored_attachment(path: str, *, filename: str) -> None:
  validation = validate_upload_path(
    path,
    filename=filename,
    allowed_exts=_allowed_attachment_exts_with_dot(),
  )
  if not validation.ok:
    raise UploadSecurityError("upload_validation_failed")
  scan_upload_path(path, filename=filename)


def _sanitize_filename_preserve_unicode(name: str) -> str:
  """File name  ( )"""
  try:
    name = os.path.basename(name or "")
    name = unicodedata.normalize("NFC", name).replace("\x00", "")
    invalid = '<>:"/\\|?*\r\n\t'
    table = {ord(ch): "_" for ch in invalid}
    name = name.translate(table).strip(" .")

    base, ext = os.path.splitext(name)
    if not base:
      base = "file"

    # Process Length 
    if len(ext) > 30:
      ext = ext[:30]
    max_base = 200 - len(ext)
    if len(base) > max_base:
      base = base[:max_base]

    return f"{base}{ext}"
  except Exception:
    return "file.bin"


def _unique_stored_name(directory: str, filename: str) -> str:
  """Duplicate File name """
  base, ext = os.path.splitext(os.path.basename(filename))
  if not base:
    base = "file"
  candidate = f"{base}{ext}"
  i = 1
  while os.path.exists(os.path.join(directory, candidate)):
    candidate = f"{base} ({i}){ext}"
    i += 1
    if i > 500: #  
      candidate = f"{base}__{uuid.uuid4().hex}{ext}"
      break
  return candidate


def _max_attachment_bytes() -> int:
  return resolve_first_positive_int(
    (
      "INVOICE_ATTACHMENT_MAX_BYTES",
      "FILE_ASSET_MAX_BYTES",
      "UPLOAD_MAX_BYTES",
      "MAX_CONTENT_LENGTH",
    ),
    default=0,
  )


class _UploadTooLarge(UploadTooLarge):
  pass


def _save_upload_stream(file_obj, dst: str, *, max_bytes: int) -> int:
  return _save_upload_stream_impl(
    file_obj,
    dst,
    max_bytes=max_bytes,
    too_large_exc=_UploadTooLarge,
    context_prefix="billing_invoices.clients._save_upload_stream",
    report_seek_errors=False,
    log_window_seconds=300,
  )


# ==============================================================================
# 3. Helper Functions: PDF Processing & LLM Logic (No Tesseract)
# ==============================================================================

def _auto_apply_bizreg_to_client(conn, client_id: int, parsed: dict):
  """ Business profile information Client information Auto Apply ()"""
  if not parsed or not isinstance(parsed, dict):
    return
  if not current_app.config.get("AUTO_APPLY_BIZREG_TO_CLIENT", False):
    return

  overwrite = current_app.config.get("AUTO_APPLY_BIZREG_OVERWRITE", False)
  row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
  if not row:
    return

  def getv(r, k):
    return _get_row_value(r, k, None, None)

  mapping = {
    "biz_reg_number": "reg_number",
    "biz_company_name": "company_name",
    "biz_representative_name": "representative_name",
    "biz_opening_date": "opening_date",
    "biz_corp_registration_number": "corp_registration_number",
    "biz_business_location": "business_location",
    "biz_head_office_location": "head_office_location",
    "biz_business_type": "business_type",
    "biz_tax_invoice_email": "tax_invoice_email",
  }

  updates = {}
  # Biz Fields Update
  for col, key in mapping.items():
    nv = (parsed.get(key) or "").strip()
    if not nv:
      continue

    cv = None
    try:
      cv = getv(row, col)
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.clients.auto_apply_bizreg.getv",
        log_key="billing_invoices.clients.auto_apply_bizreg.getv",
        log_window_seconds=300,
      )

    if overwrite or not (cv or "").strip():
      updates[col] = nv

  # Core Fields Update (Email, Address, Manager) - only if empty or overwrite
  try:
    tv = (parsed.get("tax_invoice_email") or "").strip()
    if tv and (overwrite or not (getv(row, "email") or "").strip()):
      updates["email"] = tv
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.auto_apply_bizreg.email",
      log_key="billing_invoices.clients.auto_apply_bizreg.email",
      log_window_seconds=300,
    )

  try:
    av = (parsed.get("business_location") or parsed.get("head_office_location") or "").strip()
    if av and (overwrite or not (getv(row, "address") or "").strip()):
      updates["address"] = av
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.auto_apply_bizreg.address",
      log_key="billing_invoices.clients.auto_apply_bizreg.address",
      log_window_seconds=300,
    )

  try:
    mv = (parsed.get("representative_name") or "").strip()
    if mv and (overwrite or not (getv(row, "manager") or "").strip()):
      updates["manager"] = mv
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.auto_apply_bizreg.manager",
      log_key="billing_invoices.clients.auto_apply_bizreg.manager",
      log_window_seconds=300,
    )

  name_val = (parsed.get("company_name") or "").strip() or (getv(row, "name") or "").strip()
  biz_name = (parsed.get("company_name") or "").strip() or (
    getv(row, "biz_company_name") or ""
  ).strip()
  api_key = get_openai_api_key(allow_legacy=False)
  search_tags = build_client_search_tags_text(
    [
      name_val,
      biz_name,
      (parsed.get("reg_number") or "").strip(),
      (parsed.get("corp_registration_number") or "").strip(),
    ],
    api_key=api_key,
    use_llm=bool(api_key),
  )
  if search_tags:
    updates["search_tags"] = search_tags

  if not updates:
    return

  cols = list(updates.keys())
  vals = [updates[c] for c in cols]
  sql = "UPDATE clients SET " + ",".join([f"{c}=?" for c in cols]) + " WHERE id=?"
  conn.execute(sql, (*vals, client_id))


# ==============================================================================
# 4. Routes: Client List & Create
# ==============================================================================


@bp.route("")
def list_clients():
  try:
    if (
      sync_clients_bidirectional
      and current_app.config.get("INVOICEAPP_CLIENT_SYNC_ENABLED")
      and current_app.config.get("INVOICEAPP_INTEGRATED")
      and not unified_clients_enabled()
    ):
      sync_clients_bidirectional(current_app.config.get("DB_PATH") or "")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.list_clients.sync_clients_bidirectional",
      log_key="billing_invoices.clients.list_clients.sync_clients_bidirectional",
      log_window_seconds=300,
    )

  conn = get_db()

  # 
  search_query = request.args.get("q", "").strip()
  ipm_client_id = request.args.get("ipm_client_id", "").strip()
  ipm_party_id = request.args.get("ipm_party_id", "").strip()
  has_outstanding = (request.args.get("has_outstanding") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
  }
  is_compact_q = search_query and is_compact_query(search_query)
  requested_sort = (request.args.get("sort") or "").strip()
  sort_aliases = {
    # Legacy UI param: created_at effectively means newest registration first.
    "created_at": "recent",
    "id": "recent",
  }
  sort_by = sort_aliases.get(requested_sort, requested_sort or "recent")
  page = _safe_int(request.args.get("page", 1), 1, 1, None)
  per_page = _safe_int(request.args.get("per_page", 50), 50, 10, 200)

  # Default 
  base_query = """
    SELECT c.*,
        COUNT(DISTINCT i.id) as invoice_count,
        COUNT(DISTINCT i.currency) as currency_count,
        COALESCE(SUM(
          COALESCE(i.subtotal,0)
          - COALESCE(li.admin_sum,0)
          - COALESCE(li.foreign_sum,0)
        ), 0) as total_revenue,
        MAX(i.issue_date) as last_invoice_date,
        MAX(i.currency) as currency_code
    FROM clients c
    LEFT JOIN invoices i ON i.client_id = c.id
    LEFT JOIN (
      SELECT
        invoice_id,
        COALESCE(SUM(CASE
          WHEN item_type = 'admin' THEN (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
          ELSE 0
        END), 0) as admin_sum,
        COALESCE(SUM(CASE
          WHEN item_type = 'foreign' THEN (
            CASE WHEN COALESCE(fx_rate_used, 0) > 0 THEN
               (COALESCE(fx_fee,0) + COALESCE(fx_gov,0))
               * COALESCE(fx_rate_used, 0)
               * (1 + COALESCE(fx_markup,0)/100.0)
            ELSE
               (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          )
          ELSE 0
        END), 0) as foreign_sum,
        0 as _unused_foreign_taxable_sum
      FROM line_items
      WHERE (is_estimated IS NULL OR is_estimated = 0)
      GROUP BY invoice_id
    ) li ON li.invoice_id = i.id
  """

  where_conditions = ["c.is_deleted IS NOT TRUE"] # Exclude soft-deleted clients
  params = []

  # days Searchdays SQL LIKE , -only Search Process row
  if search_query and not is_compact_q:
    search_clause, search_params = sql_ci_contains_any(
      [
        "c.name",
        "c.email",
        "c.phone",
        "c.address",
        "c.notes",
        "c.search_tags",
        "c.biz_reg_number",
        "c.biz_company_name",
        "c.biz_tax_invoice_email",
      ],
      search_query,
    )
    if search_clause:
      where_conditions.append(search_clause)
      params.extend(search_params)

  if ipm_client_id and ipm_client_id.isdigit():
    where_conditions.append("c.ipm_client_id = ?")
    params.append(int(ipm_client_id))

  if ipm_party_id:
    where_conditions.append("c.ipm_party_id = ?")
    params.append(ipm_party_id)

  if has_outstanding:
    where_conditions.append(
      """
      EXISTS (
        SELECT 1
        FROM invoices oi
        WHERE oi.client_id = c.id
         AND (
          (
           COALESCE(oi.billing_status, '') = 'sent'
           AND COALESCE(oi.payment_status, '') IN ('unpaid', 'pending')
          )
          OR COALESCE(oi.status, '') IN ('sent_unpaid', 'payment_pending', 'pre_overdue')
         )
      )
      """
    )

  base_query += " WHERE " + " AND ".join(where_conditions)

  base_query += " GROUP BY c.id"

  # Sort
  sort_clauses = {
    "name": "ORDER BY c.name, c.id DESC",
    "recent": "ORDER BY c.id DESC",
    "invoices": "ORDER BY invoice_count DESC, c.name",
    "revenue": "ORDER BY total_revenue DESC, c.name",
    "last_invoice": "ORDER BY last_invoice_date DESC NULLS LAST, c.name",
  }
  order_clause = sort_clauses.get(sort_by, "ORDER BY c.id DESC")

  # Pagination / times
  if is_compact_q:
    # -only Search: SQLfrom q Filters  times from  Filters
    rows_all = conn.execute(f"{base_query} {order_clause}", params).fetchall()
    q_compact = to_compact(search_query)
    filtered_rows = []
    for r in rows_all:
      # Tuple fallback (id, name, email, phone, address, ..., notes at index 6)
      name = str(_get_row_value(r, "name", 1, "") or "")
      email = str(_get_row_value(r, "email", 2, "") or "")
      phone = str(_get_row_value(r, "phone", 3, "") or "")
      address = str(_get_row_value(r, "address", 4, "") or "")
      notes = str(_get_row_value(r, "notes", 6, "") or "")
      search_tags = str(_get_row_value(r, "search_tags", None, "") or "")
      biz_company = str(_get_row_value(r, "biz_company_name", None, "") or "")
      biz_reg = str(_get_row_value(r, "biz_reg_number", None, "") or "")
      text = " ".join([name, email, phone, address, notes, search_tags, biz_company, biz_reg])
      if q_compact in to_compact(text):
        filtered_rows.append(r)
    total_count = len(filtered_rows)
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
    if page > total_pages:
      page = total_pages
    offset = (page - 1) * per_page
    clients = filtered_rows[offset : offset + per_page]
  else:
    # Existing: SQL COUNT + LIMIT/OFFSET Pagination
    count_query = f"SELECT COUNT(*) FROM ({base_query}) AS client_stats"
    total_count = conn.execute(count_query, params).fetchone()[0]
    total_pages = (total_count + per_page - 1) // per_page if total_count else 1
    if page > total_pages:
      page = total_pages
    offset = (page - 1) * per_page
    clients = conn.execute(
      f"{base_query} {order_clause} LIMIT ? OFFSET ?",
      params + [per_page, offset],
    ).fetchall()

  # 
  stats_query = """
    SELECT
      COUNT(DISTINCT c.id) as total_clients,
      COUNT(DISTINCT CASE WHEN i.id IS NOT NULL THEN c.id END) as clients_with_invoices,
      COUNT(DISTINCT i.id) as total_invoices,
      COALESCE(SUM(
        COALESCE(i.subtotal,0)
        - COALESCE(li.admin_sum,0)
        - COALESCE(li.foreign_sum,0)
      ), 0) as total_revenue
    FROM clients c
    LEFT JOIN invoices i ON i.client_id = c.id
    LEFT JOIN (
      SELECT
        invoice_id,
        COALESCE(SUM(CASE
          WHEN item_type = 'admin' THEN (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
          ELSE 0
        END), 0) as admin_sum,
        COALESCE(SUM(CASE
          WHEN item_type = 'foreign' THEN (
            CASE WHEN COALESCE(fx_rate_used, 0) > 0 THEN
               (COALESCE(fx_fee,0) + COALESCE(fx_gov,0))
               * COALESCE(fx_rate_used, 0)
               * (1 + COALESCE(fx_markup,0)/100.0)
            ELSE
               (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          )
          ELSE 0
        END), 0) as foreign_sum,
        0 as _unused_foreign_taxable_sum
      FROM line_items
      WHERE (is_estimated IS NULL OR is_estimated = 0)
      GROUP BY invoice_id
    ) li ON li.invoice_id = i.id
  """
  if where_conditions:
    stats_query += " WHERE " + " AND ".join(where_conditions)
  stats = conn.execute(stats_query, params).fetchone()

  # Duplicate  ()
  duplicate_groups = []
  seen = set()

  # Email/Phone  (Notes )
  counts = {}
  rows_iter = conn.execute(
    "SELECT id, name, email, phone FROM clients WHERE is_deleted IS NOT TRUE"
  )
  for r in rows_iter:
    email_val = _get_row_value(r, "email", 2)
    email = (email_val or "").strip().lower()
    phone_val = _get_row_value(r, "phone", 3)
    phone = "".join(filter(str.isdigit, (phone_val or "")))

    if email:
      key = f"e:{email}"
      counts[key] = counts.get(key, 0) + 1
    if phone:
      key = f"p:{phone}"
      counts[key] = counts.get(key, 0) + 1

  dup_keys = {k for k, v in counts.items() if v > 1}
  lookup = {} # key -> list of rows
  if dup_keys:
    rows_iter = conn.execute(
      "SELECT id, name, email, phone FROM clients WHERE is_deleted IS NOT TRUE"
    )
    for r in rows_iter:
      email_val = _get_row_value(r, "email", 2)
      email = (email_val or "").strip().lower()
      phone_val = _get_row_value(r, "phone", 3)
      phone = "".join(filter(str.isdigit, (phone_val or "")))

      if email:
        key = f"e:{email}"
        if key in dup_keys:
          lookup.setdefault(key, []).append(r)
      if phone:
        key = f"p:{phone}"
        if key in dup_keys:
          lookup.setdefault(key, []).append(r)

  for group in lookup.values():
    if len(group) < 2:
      continue
    ids = tuple(sorted(_get_row_value(g, "id", 0) for g in group))
    if ids not in seen:
      seen.add(ids)
      duplicate_groups.append(
        [
          {
            "id": _get_row_value(g, "id", 0),
            "name": _get_row_value(g, "name", 1),
          }
          for g in group
        ]
      )

  # Recent 
  try:
    recent_merge_logs = conn.execute(
      "SELECT id, target_id, created_at FROM client_merge_log WHERE undone_at IS NULL ORDER BY id DESC LIMIT 5"
    ).fetchall()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.list_clients.recent_merge_logs",
      log_key="billing_invoices.clients.list_clients.recent_merge_logs",
      log_window_seconds=300,
    )
    recent_merge_logs = []

  conn.close()

  all_profiles = get_all_business_profiles()
  try:
    currency_options = sorted({((p["currency"] or "USD").upper()) for p in all_profiles})
  except Exception:
    currency_options = ["USD"]
  if not currency_options:
    currency_options = ["USD"]

  return render_template(
    "clients_list.html",
    clients=clients,
    search_query=search_query,
    sort_by=sort_by,
    page=page,
    per_page=per_page,
    total_count=total_count,
    total_pages=total_pages,
    stats=stats,
    duplicate_groups=duplicate_groups[:10],
    recent_merge_logs=recent_merge_logs,
    all_profiles=all_profiles,
    currency_options=currency_options,
  )


@bp.route("/new", methods=["GET", "POST"])
def new_client():
  conn = get_db()
  if request.method == "POST":
    try:
      f = request.form
      # Biz Info Fields
      b_reg = (f.get("biz_reg_number") or "").strip()
      b_company = (f.get("biz_company_name") or "").strip()
      b_rep = (f.get("biz_representative_name") or "").strip()
      b_open = (f.get("biz_opening_date") or "").strip()
      b_corp = (f.get("biz_corp_registration_number") or "").strip()
      b_loc = (f.get("biz_business_location") or "").strip()
      b_head = (f.get("biz_head_office_location") or "").strip()
      b_type = (f.get("biz_business_type") or "").strip()
      b_email = (f.get("biz_tax_invoice_email") or "").strip()

      # Core Fields (Fallback to Biz info if empty)
      email = (f.get("email") or "").strip() or b_email
      address = (f.get("address") or "").strip() or (b_loc or b_head)
      manager = (f.get("manager") or "").strip() or b_rep
      search_tags = invoice_client_search_tags_fast(
        [f["name"], b_company, b_reg, b_corp, (f.get("phone") or "").strip()],
      )

      conn.execute(
        """
        INSERT INTO clients (
          name, email, phone, address, notes, manager, search_tags,
          biz_reg_number, biz_company_name, biz_representative_name, biz_opening_date,
          biz_corp_registration_number, biz_business_location, biz_head_office_location,
          biz_business_type, biz_tax_invoice_email
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      """,
        (
          f["name"],
          email,
          (f.get("phone") or "").strip(),
          address,
          f.get("notes"),
          manager,
          search_tags,
          b_reg,
          b_company,
          b_rep,
          b_open,
          b_corp,
          b_loc,
          b_head,
          b_type,
          b_email,
        ),
      )
      conn.commit()

      # Get the newly inserted client ID (lastrowid doesn't work reliably with PostgreSQL)
      new_client_row = conn.execute(
        "SELECT id FROM clients WHERE name=? ORDER BY id DESC LIMIT 1",
        (f["name"],),
      ).fetchone()
      new_client_id = new_client_row[0] if new_client_row else None

      if not new_client_id:
        flash("Client ID not found.", "warning")
        return redirect(url_for("billing_invoices.clients.list_clients"))

      try:
        enqueue_invoice_client_post_save(new_client_id)
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.clients.create_client.enqueue_post_save",
          log_key="billing_invoices.clients.create_client.enqueue_post_save",
          log_window_seconds=300,
        )
      return redirect(
        url_for("billing_invoices.clients.view_client", client_id=new_client_id)
      )
    finally:
      conn.close()

  conn.close()
  return render_template("client_form.html", client=None)


# ---------------------------------------------------------------------------
# Quick parse: Business registration file -> preview fields (no persistence)
# ---------------------------------------------------------------------------
@bp.route("/bizreg/parse", methods=["POST"])
def parse_bizreg():
  return jsonify({"success": False, "error": "Business document parsing is not available."}), 404

  try:
    f = request.files.get("file")
    if not f:
      files = request.files.getlist("files[]") or request.files.getlist("files")
      f = files[0] if files else None
    if not f or not f.filename:
      return jsonify({"success": False, "error": "File not available."}), 400
    if not _allowed_attachment(f.filename):
      return (
        jsonify({"success": False, "error": " File ."}),
        400,
      )

    api_key = get_openai_api_key(allow_legacy=False)
    if not api_key:
      return (
        jsonify({"success": False, "error": "OpenAI API  ."}),
        500,
      )

    tmp_path = None
    try:
      suffix = os.path.splitext(f.filename)[1]
      with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
      max_bytes = _max_attachment_bytes()
      try:
        _save_upload_stream(f, tmp_path, max_bytes=max_bytes)
      except _UploadTooLarge:
        return jsonify({"success": False, "error": "File ."}), 413
      analysis_meta = json.dumps({"biz_reg": {}}, ensure_ascii=False)
    finally:
      if tmp_path and os.path.exists(tmp_path):
        try:
          os.remove(tmp_path)
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.clients.parse_bizreg.cleanup_tmp",
            log_key="billing_invoices.clients.parse_bizreg.cleanup_tmp",
            log_window_seconds=300,
          )

    if not analysis_meta:
      return jsonify({"success": False, "error": "analysis not available."}), 200
    try:
      meta = json.loads(analysis_meta)
    except Exception:
      meta = {}
    biz_reg = meta.get("biz_reg") if isinstance(meta, dict) else None
    if not biz_reg:
      return (
        jsonify({"success": False, "error": "Business profileRegistration not found."}),
        200,
      )
    return jsonify({"success": True, "biz_reg": biz_reg})

  except Exception as e:
    current_app.logger.exception("Biz reg parse failed")
    return jsonify({"success": False, "error": str(e)}), 500


# ==============================================================================
# 5. Routes: View, Edit & Delete
# ==============================================================================


@bp.route("/<int:client_id>")
def view_client(client_id):
  conn = get_db()
  client = _get_client_or_404(conn, client_id)

  # Filters & Args
  status = request.args.get("status", "").strip()
  bp_id = request.args.get("business_profile_id", "").strip()
  currency = request.args.get("currency", "").strip()
  date_from = request.args.get("date_from", "").strip()
  date_to = request.args.get("date_to", "").strip()
  sort = request.args.get("sort", "issue_date")

  # View Mode (Overdue vs Outstanding)
  overdue_vals = [v.lower() for v in request.args.getlist("overdue_only")]
  overdue_only = any(v in ("1", "true", "yes", "on") for v in overdue_vals)
  current_vals = [v.lower() for v in request.args.getlist("current_only")]
  current_only = any(v in ("1", "true", "yes", "on") for v in current_vals)

  try:
    as_of_date = date.fromisoformat(request.args.get("as_of", ""))
  except (ValueError, TypeError):
    as_of_date = date.today()

  view_mode = "all"
  if overdue_only:
    view_mode = "overdue"
  elif current_only:
    view_mode = "outstanding"

  # Invoice Query Construction
  where_clauses = ["invoices.client_id = ?"]
  params = [client_id]

  # Split-status aware expressions (fallback to legacy when split columns are NULL)
  billing_expr = "COALESCE(invoices.billing_status, invoices.status)"
  payment_expr = (
    "COALESCE("
    "invoices.payment_status,"
    "CASE "
    " WHEN invoices.status='paid' THEN 'paid'"
    " WHEN invoices.status IN ('payment_pending','pre_overdue') THEN 'pending'"
    " WHEN invoices.status='void' THEN 'none'"
    " ELSE 'unpaid'"
    "END"
    ")"
  )
  outstanding_clause = (
    f"(({payment_expr} IN ('unpaid','pending')) OR ({billing_expr}='pre_overdue'))"
  )

  if status:
    s = (status or "").strip()
    if s in ("draft", "sent", "void", "tax_issued", "cash_issued", "processed", "pre_overdue"):
      where_clauses.append(f"{billing_expr} = ?")
      params.append(s)
    elif s == "sent_unpaid":
      where_clauses.append(f"({billing_expr}='sent' AND {payment_expr}='unpaid')")
    elif s == "payment_pending":
      where_clauses.append(f"({billing_expr}='sent' AND {payment_expr}='pending')")
    elif s == "sent_unpaid_or_pending":
      where_clauses.append(
        f"({billing_expr}='sent' AND {payment_expr} IN ('unpaid','pending'))"
      )
    elif s == "paid":
      where_clauses.append(f"{payment_expr}='paid'")
    elif s == "paid_no_tax":
      where_clauses.append(
        f"({payment_expr}='paid' AND {billing_expr} NOT IN ('tax_issued','cash_issued','processed'))"
      )
    else:
      where_clauses.append("invoices.status = ?")
      params.append(s)
  if bp_id and bp_id.isdigit():
    where_clauses.append("invoices.business_profile_id = ?")
    params.append(int(bp_id))
  if currency:
    where_clauses.append("invoices.currency = ?")
    params.append(currency)
  if date_from:
    where_clauses.append("invoices.issue_date >= ?")
    params.append(date_from)
  if date_to:
    where_clauses.append("invoices.issue_date <= ?")
    params.append(date_to)

  due_expr = "COALESCE(invoices.due_date, invoices.issue_date)"

  if view_mode == "overdue":
    where_clauses.append(f"(({due_expr} < ?) OR {billing_expr}='pre_overdue')")
    params.append(as_of_date.isoformat())
    where_clauses.append(outstanding_clause)
  elif view_mode == "outstanding":
    where_clauses.append(outstanding_clause)

  order_map = {
    "due_date": "invoices.due_date DESC, invoices.id DESC",
    "total": "invoices.total DESC, invoices.id DESC",
  }
  order_clause = order_map.get(sort, "invoices.issue_date DESC, invoices.id DESC")

  sql = f"""
   SELECT invoices.*, business_profile.name as business_name, business_profile.currency
   FROM invoices
   JOIN business_profile ON business_profile.id=invoices.business_profile_id
   WHERE {' AND '.join(where_clauses)}
   ORDER BY {order_clause}
  """
  invoices = conn.execute(sql, params).fetchall()

  # Stats Calculation
  stats = {
    "invoiced_by_currency": {},
    "outstanding_by_currency": {},
    "current_by_currency": {},
    "overdue_by_currency": {},
  }

  def _fallback_billing_payment(legacy_status: str) -> tuple[str, str]:
    s = (legacy_status or "draft").strip().lower()
    # billing
    if s in ("draft", "sent", "void", "tax_issued", "cash_issued", "processed", "pre_overdue"):
      b = s
    elif s in ("payment_pending", "paid"):
      b = "sent"
    else:
      b = "draft"
    # payment
    if s == "paid":
      p = "paid"
    elif s in ("payment_pending", "pre_overdue"):
      p = "pending"
    elif s == "void":
      p = "none"
    else:
      p = "unpaid"
    # void/not available coupling
    if b == "void" or p == "none":
      return "void", "none"
    return b, p

  for inv in invoices:
    cur = (inv["currency"] or "USD").upper()
    amt = float(inv["total"] or 0.0)
    stats["invoiced_by_currency"][cur] = stats["invoiced_by_currency"].get(cur, 0.0) + amt

    legacy = (inv["status"] or "").strip()
    bs = (inv.get("billing_status") or "").strip().lower()
    ps = (inv.get("payment_status") or "").strip().lower()
    if not bs or not ps:
      bs, ps = _fallback_billing_payment(legacy)
    if bs == "void" or ps == "none":
      continue

    is_outstanding = (ps in ("unpaid", "pending")) or (bs == "pre_overdue")
    if is_outstanding:
      stats["outstanding_by_currency"][cur] = (
        stats["outstanding_by_currency"].get(cur, 0.0) + amt
      )

      due_str = inv["due_date"] or inv["issue_date"]
      try:
        due = date.fromisoformat(due_str) if due_str else as_of_date
      except (ValueError, TypeError):
        due = as_of_date

      if bs == "pre_overdue" or (as_of_date - due).days > 0:
        stats["overdue_by_currency"][cur] = stats["overdue_by_currency"].get(cur, 0.0) + amt
      else:
        stats["current_by_currency"][cur] = stats["current_by_currency"].get(cur, 0.0) + amt

  total_invoiced = sum(stats["invoiced_by_currency"].values())
  total_outstanding = sum(stats["outstanding_by_currency"].values())

  # Attachments & Biz Info Retrieval
  try:
    attachments = conn.execute(
      """
      SELECT id, original_name, stored_name, content_type, size, uploaded_at, analysis_meta
      FROM client_attachments WHERE client_id=? ORDER BY uploaded_at DESC
    """,
      (client_id,),
    ).fetchall()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.view_client.attachments",
      log_key="billing_invoices.clients.view_client.attachments",
      log_window_seconds=300,
    )
    attachments = []

  biz_info = None
  # 1. Latest Attachment Analysis
  for r in attachments:
    try:
      meta = _get_row_value(r, "analysis_meta", 6)
      if meta:
        d = json.loads(meta)
        if isinstance(d, dict) and "biz_reg" in d:
          biz_info = d["biz_reg"]
          break
    except (TypeError, ValueError, json.JSONDecodeError):
      continue

  # 2. DB Stored Values Fallback
  db_biz_keys = [
    ("biz_reg_number", "reg_number"),
    ("biz_company_name", "company_name"),
    ("biz_representative_name", "representative_name"),
    ("biz_opening_date", "opening_date"),
    ("biz_corp_registration_number", "corp_registration_number"),
    ("biz_business_location", "business_location"),
    ("biz_head_office_location", "head_office_location"),
    ("biz_business_type", "business_type"),
    ("biz_tax_invoice_email", "tax_invoice_email"),
  ]

  temp_biz = {}
  has_db_val = False
  for db_col, key in db_biz_keys:
    val = _get_row_value(client, db_col, None, None)
    if val:
      has_db_val = True
    temp_biz[key] = val or ""

  if has_db_val:
    biz_info = temp_biz

  # Display fallbacks
  display_email = (_get_row_value(client, "email", None, None)) or (
    biz_info.get("tax_invoice_email") if biz_info else None
  )
  display_manager = (_get_row_value(client, "manager", None, None)) or (
    biz_info.get("representative_name") if biz_info else None
  )
  display_address = (_get_row_value(client, "address", None, None)) or (
    (biz_info.get("business_location") or biz_info.get("head_office_location"))
    if biz_info
    else None
  )

  # Pre-compute available currencies before closing the connection
  available_currencies = [
    _get_row_value(r, "currency", 0)
    for r in conn.execute(
      "SELECT DISTINCT currency FROM invoices WHERE client_id=?",
      (client_id,),
    ).fetchall()
    if _get_row_value(r, "currency", 0)
  ]

  deposit_balances = {}
  try:
    rows = conn.execute(
      """
      SELECT currency, COALESCE(SUM(amount_minor), 0) AS bal
      FROM client_deposit_ledger
      WHERE client_id=?
      GROUP BY currency
      """,
      (int(client_id),),
    ).fetchall()
    for r in rows or []:
      cur = _get_row_value(r, "currency", 0, "") or ""
      cur = (cur or "").strip().upper()
      if not cur:
        continue
      try:
        bal = int(_get_row_value(r, "bal", 1, 0) or 0)
      except Exception:
        bal = 0
      deposit_balances[cur] = bal
  except Exception:
    deposit_balances = {}
  deposit_balances_sorted = sorted(deposit_balances.items(), key=lambda x: x[0])
  conn.close()

  return render_template(
    "client_view.html",
    client=client,
    client_email_display=display_email,
    client_manager_display=display_manager,
    client_address_display=display_address,
    invoices=invoices,
    total_invoiced=total_invoiced,
    total_outstanding=total_outstanding,
    invoiced_by_currency=stats["invoiced_by_currency"],
    outstanding_by_currency=stats["outstanding_by_currency"],
    current_by_currency=stats["current_by_currency"],
    overdue_by_currency=stats["overdue_by_currency"],
    status=status,
    business_profile_id=bp_id,
    currency=currency,
    date_from=date_from,
    date_to=date_to,
    sort=sort,
    overdue_only=overdue_only,
    current_only=current_only,
    view_mode=view_mode,
    as_of=as_of_date.isoformat(),
    all_profiles=get_all_business_profiles(),
    available_currencies=available_currencies,
    deposit_balances_sorted=deposit_balances_sorted,
    client_attachments=attachments,
    biz_info=biz_info,
  )


@bp.route("/deposit")
def deposit_overview():
  conn = get_db()
  q = (request.args.get("q") or "").strip()
  currency = (request.args.get("currency") or "").strip().upper()
  bp_id = _safe_int((request.args.get("business_profile_id") or "").strip(), default=None, min_=1)
  page = _safe_int(request.args.get("page"), default=1, min_=1) or 1
  per_page = _safe_int(request.args.get("per_page"), default=50, min_=20, max_=200) or 50
  if per_page not in (20, 50, 100, 200):
    per_page = 50

  filters = []
  params = []
  if bp_id is not None:
    filters.append("l.business_profile_id = ?")
    params.append(int(bp_id))
  if currency:
    filters.append("l.currency = ?")
    params.append(currency)
  if q:
    search_clause, search_params = sql_ci_contains_any(
      ["c.name", "CAST(l.client_id AS TEXT)"],
      q,
    )
    if search_clause:
      filters.append(search_clause)
      params.extend(search_params)

  where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
  total_row = conn.execute(
    f"""
    SELECT COUNT(*)
    FROM (
      SELECT l.client_id, l.currency
      FROM client_deposit_ledger l
      JOIN clients c ON c.id = l.client_id
      {where_sql}
      GROUP BY l.client_id, l.currency
    ) t
    """,
    params,
  ).fetchone()
  total_count = int(_get_row_value(total_row, "count", 0, 0) or 0)
  total_pages = max(1, (total_count + per_page - 1) // per_page)
  if page > total_pages:
    page = total_pages
  offset = (page - 1) * per_page

  rows = conn.execute(
    f"""
    SELECT
      l.client_id,
      c.name AS client_name,
      l.currency,
      COALESCE(SUM(l.amount_minor), 0) AS balance_minor,
      COUNT(*) AS entry_count,
      MAX(l.created_at) AS last_created_at
    FROM client_deposit_ledger l
    JOIN clients c ON c.id = l.client_id
    {where_sql}
    GROUP BY l.client_id, c.name, l.currency
    ORDER BY MAX(l.created_at) DESC NULLS LAST, l.client_id DESC
    LIMIT ? OFFSET ?
    """,
    params + [per_page, offset],
  ).fetchall()

  grand = conn.execute(
    f"""
    SELECT
      l.currency,
      COALESCE(SUM(l.amount_minor), 0) AS balance_minor
    FROM client_deposit_ledger l
    JOIN clients c ON c.id = l.client_id
    {where_sql}
    GROUP BY l.currency
    ORDER BY l.currency
    """,
    params,
  ).fetchall()
  conn.close()

  return render_template(
    "clients/deposit_overview.html",
    rows=rows,
    q=q,
    currency=currency,
    business_profile_id=bp_id,
    page=page,
    per_page=per_page,
    total_count=total_count,
    total_pages=total_pages,
    grand_totals=grand,
    all_profiles=get_all_business_profiles(),
  )


@bp.route("/<int:client_id>/deposit")
def client_deposit_ledger(client_id: int):
  conn = get_db()
  client = _get_client_or_404(conn, client_id)

  all_profiles = get_all_business_profiles()
  profiles_by_id = {}
  try:
    for p in all_profiles or []:
      try:
        pid = int(p.get("id"))
      except Exception:
        continue
      profiles_by_id[pid] = p
  except Exception:
    profiles_by_id = {}

  next_url = (request.full_path or request.path or "").strip()
  if next_url.endswith("?"):
    next_url = next_url[:-1]

  scope_kind, business_profile_id, business_profile_filter_value = _parse_deposit_ledger_scope(
    request.args
  )

  currency = (request.args.get("currency") or "").strip().upper()
  q = (request.args.get("q") or "").strip()
  try:
    page = int(request.args.get("page") or 1)
  except Exception:
    page = 1
  if page < 1:
    page = 1
  try:
    per_page = int(request.args.get("per_page") or 50)
  except Exception:
    per_page = 50
  if per_page not in (20, 50, 100, 200):
    per_page = 50

  try:
    currency_options = sorted({((p["currency"] or "USD").upper()) for p in all_profiles})
  except Exception:
    currency_options = ["USD"]
  if not currency_options:
    currency_options = ["USD"]

  default_currency = "USD"
  try:
    default_bp_id = int(all_profiles[0]["id"]) if all_profiles else 1
  except Exception:
    default_bp_id = 1
  try:
    for p in all_profiles:
      if business_profile_id is not None and int(p["id"]) == int(business_profile_id):
        default_currency = (p["currency"] or "USD").upper()
        break
      if business_profile_id is None and int(p["id"]) == int(default_bp_id):
        default_currency = (p["currency"] or "USD").upper()
  except Exception:
    default_currency = "USD"

  balances = {}
  balances_sorted = []
  entries = []
  total_count = 0
  total_pages = 1
  try:
    if scope_kind == "all":
      rows = conn.execute(
        """
        SELECT currency, COALESCE(SUM(amount_minor), 0) AS bal
        FROM client_deposit_ledger
        WHERE client_id=?
        GROUP BY currency
        """,
        (int(client_id),),
      ).fetchall()
      balances = {}
      for r in rows or []:
        cur = _get_row_value(r, "currency", 0, "") or ""
        cur = (cur or "").strip().upper()
        if not cur:
          continue
        try:
          bal = int(_get_row_value(r, "bal", 1, 0) or 0)
        except Exception:
          bal = 0
        balances[cur] = bal
    else:
      balances = get_client_deposit_balances_minor(conn, business_profile_id, client_id)
    balances_sorted = sorted(balances.items(), key=lambda x: x[0])

    where = ["client_deposit_ledger.client_id=?"]
    params = [int(client_id)]
    if scope_kind == "business_profile" and business_profile_id is not None:
      where.append("client_deposit_ledger.business_profile_id=?")
      params.append(int(business_profile_id))
    elif scope_kind == "global":
      where.append("client_deposit_ledger.business_profile_id IS NULL")
    if currency:
      where.append("client_deposit_ledger.currency=?")
      params.append(currency)
    if q:
      search_clause, search_params = sql_ci_contains_any(
        [
          "client_deposit_ledger.memo",
          "client_deposit_ledger.related_bank_transaction_id",
          "invoices.number",
          "CAST(client_deposit_ledger.related_invoice_id AS TEXT)",
          "CAST(client_deposit_ledger.id AS TEXT)",
        ],
        q,
      )
      if search_clause:
        where.append(search_clause)
        params.extend(search_params)

    where_sql = " AND ".join(where)
    total_count = conn.execute(
      f"""
      SELECT COUNT(*)
      FROM client_deposit_ledger
      LEFT JOIN invoices ON invoices.id = client_deposit_ledger.related_invoice_id
      WHERE {where_sql}
      """,
      params,
    ).fetchone()[0]
    total_pages = max(1, (int(total_count) + per_page - 1) // per_page)
    if page > total_pages:
      page = total_pages

    offset = (page - 1) * per_page
    entries = conn.execute(
      f"""
      SELECT
       client_deposit_ledger.*,
       invoices.number AS related_invoice_number
      FROM client_deposit_ledger
      LEFT JOIN invoices ON invoices.id = client_deposit_ledger.related_invoice_id
      WHERE {where_sql}
      ORDER BY client_deposit_ledger.created_at DESC, client_deposit_ledger.id DESC
      LIMIT ? OFFSET ?
      """,
      params + [per_page, offset],
    ).fetchall()
  finally:
    conn.close()

  return render_template(
    "clients/deposit_ledger.html",
    client=client,
    all_profiles=all_profiles,
    profiles_by_id=profiles_by_id,
    scope_kind=scope_kind,
    business_profile_id=business_profile_id,
    business_profile_filter_value=business_profile_filter_value,
    currency=currency,
    q=q,
    next_url=next_url,
    currency_options=currency_options,
    default_currency=default_currency,
    balances_sorted=balances_sorted,
    entries=entries,
    page=page,
    per_page=per_page,
    total_pages=total_pages,
    total_count=total_count,
  )


def _parse_deposit_ledger_scope(args) -> tuple[str, int | None, str]:
  """Parse deposit ledger scope from query parameters.

  Returns:
  - scope kind: "all" | "global" | "business_profile"
  - business_profile_id: selected profile id for business-profile scope
  - normalized query value for `business_profile_id`
  """

  raw = (args.get("business_profile_id") or "").strip().lower()
  if not raw or raw == "all":
    return "all", None, ""
  if raw == "global":
    return "global", None, "global"
  if raw.isdigit():
    bp_id = int(raw)
    return "business_profile", bp_id, str(bp_id)
  return "all", None, ""


def _parse_deposit_business_profile_scope(form) -> int | None:
  """Parse deposit scope from form data.

  Legacy callers may omit `business_profile_id`; default those requests to
  office-wide/global deposit scope so historical workflows continue to work
  with the new default semantics.
  """
  bp_id_raw = (form.get("business_profile_id") or "").strip().lower()
  if "business_profile_id" not in form:
    return None
  if not bp_id_raw or bp_id_raw in {"all", "global"}:
    return None
  if bp_id_raw.isdigit():
    return int(bp_id_raw)
  abort(400, "Business profile select.")


def _deposit_ledger_redirect_scope_kwargs(business_profile_id: int | None) -> dict[str, str | int]:
  """Build redirect query params for the ledger page after a deposit action."""

  if business_profile_id is None:
    return {"business_profile_id": "global"}
  return {"business_profile_id": int(business_profile_id)}


@bp.route("/<int:client_id>/deposit/topup", methods=["POST"])
@role_required("admin", "staff")
def client_deposit_topup(client_id: int):
  conn = get_db()
  _get_client_or_404(conn, client_id)

  next_url = (request.form.get("next") or "").strip()

  business_profile_id = _parse_deposit_business_profile_scope(request.form)
  currency = (request.form.get("currency") or "").strip().upper()
  memo = (request.form.get("memo") or "").strip() or None
  related_bank_transaction_id = (request.form.get("related_bank_transaction_id") or "").strip() or None
  user = get_current_user()

  try:
    amount_minor = _parse_amount_to_minor(request.form.get("amount"), currency)
    if amount_minor <= 0:
      raise ValueError(" Amount 0 .")

    res = insert_client_deposit_ledger_entry(
      conn,
      business_profile_id,
      client_id,
      currency,
      amount_minor,
      "topup",
      memo=memo,
      related_bank_transaction_id=related_bank_transaction_id,
      created_by=(user["id"] if user else None),
    )
    meta = build_client_deposit_audit_meta(
      entry_id=res.get("entry_id"),
      business_profile_id=business_profile_id,
      client_id=client_id,
      currency=currency,
      amount_minor=amount_minor,
      entry_type="topup",
      memo=memo,
      related_bank_transaction_id=related_bank_transaction_id,
      balance_before_minor=res.get("balance_before_minor"),
      balance_after_minor=res.get("balance_after_minor"),
    )
    log_audit("client.deposit.topup", "client", client_id, meta)
    flash("Retainer Registration.", "success")
  except Exception as e:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.clients.client_deposit_topup.rollback",
        log_key="billing_invoices.clients.client_deposit_topup.rollback",
        log_window_seconds=300,
      )
    msg = getattr(e, "description", None) or str(e)
    flash(f"Retainer : {msg}", "error")
  finally:
    conn.close()

  if next_url:
    try:
      next_url = next_url.replace("\r", "").replace("\n", "")
    except Exception:
      next_url = ""
    if next_url.startswith("/") and not next_url.startswith("//"):
      return redirect(next_url)

  return redirect(
    url_for(
      "billing_invoices.clients.client_deposit_ledger",
      client_id=client_id,
      currency=currency,
      **_deposit_ledger_redirect_scope_kwargs(business_profile_id),
    )
  )


@bp.route("/<int:client_id>/deposit/refund", methods=["POST"])
@role_required("admin", "staff")
def client_deposit_refund(client_id: int):
  conn = get_db()
  _get_client_or_404(conn, client_id)

  next_url = (request.form.get("next") or "").strip()

  business_profile_id = _parse_deposit_business_profile_scope(request.form)
  currency = (request.form.get("currency") or "").strip().upper()
  memo = (request.form.get("memo") or "").strip() or None
  user = get_current_user()

  try:
    amt = _parse_amount_to_minor(request.form.get("amount"), currency)
    amt = abs(int(amt))
    if amt <= 0:
      raise ValueError(" Amount 0 .")

    amount_minor = -amt
    res = insert_client_deposit_ledger_entry(
      conn,
      business_profile_id,
      client_id,
      currency,
      amount_minor,
      "refund",
      memo=memo,
      created_by=(user["id"] if user else None),
    )
    meta = build_client_deposit_audit_meta(
      entry_id=res.get("entry_id"),
      business_profile_id=business_profile_id,
      client_id=client_id,
      currency=currency,
      amount_minor=amount_minor,
      entry_type="refund",
      memo=memo,
      balance_before_minor=res.get("balance_before_minor"),
      balance_after_minor=res.get("balance_after_minor"),
    )
    log_audit("client.deposit.refund", "client", client_id, meta)
    flash("Retainer Registration.", "success")
  except Exception as e:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.clients.client_deposit_refund.rollback",
        log_key="billing_invoices.clients.client_deposit_refund.rollback",
        log_window_seconds=300,
      )
    msg = getattr(e, "description", None) or str(e)
    flash(f"Retainer : {msg}", "error")
  finally:
    conn.close()

  if next_url:
    try:
      next_url = next_url.replace("\r", "").replace("\n", "")
    except Exception:
      next_url = ""
    if next_url.startswith("/") and not next_url.startswith("//"):
      return redirect(next_url)

  return redirect(
    url_for(
      "billing_invoices.clients.client_deposit_ledger",
      client_id=client_id,
      currency=currency,
      **_deposit_ledger_redirect_scope_kwargs(business_profile_id),
    )
  )


@bp.route("/<int:client_id>/deposit/adjust", methods=["POST"])
@role_required("admin", "staff")
def client_deposit_adjust(client_id: int):
  conn = get_db()
  _get_client_or_404(conn, client_id)

  next_url = (request.form.get("next") or "").strip()

  business_profile_id = _parse_deposit_business_profile_scope(request.form)
  currency = (request.form.get("currency") or "").strip().upper()
  memo = (request.form.get("memo") or "").strip() or None
  user = get_current_user()

  try:
    amount_minor = _parse_amount_to_minor(request.form.get("amount"), currency)
    if amount_minor == 0:
      raise ValueError(" Amount 0  not available.")

    res = insert_client_deposit_ledger_entry(
      conn,
      business_profile_id,
      client_id,
      currency,
      amount_minor,
      "adjust",
      memo=memo,
      created_by=(user["id"] if user else None),
    )
    meta = build_client_deposit_audit_meta(
      entry_id=res.get("entry_id"),
      business_profile_id=business_profile_id,
      client_id=client_id,
      currency=currency,
      amount_minor=amount_minor,
      entry_type="adjust",
      memo=memo,
      balance_before_minor=res.get("balance_before_minor"),
      balance_after_minor=res.get("balance_after_minor"),
    )
    log_audit("client.deposit.adjust", "client", client_id, meta)
    flash("Retainer Registration.", "success")
  except Exception as e:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.clients.client_deposit_adjust.rollback",
        log_key="billing_invoices.clients.client_deposit_adjust.rollback",
        log_window_seconds=300,
      )
    msg = getattr(e, "description", None) or str(e)
    flash(f"Retainer : {msg}", "error")
  finally:
    conn.close()

  if next_url:
    try:
      next_url = next_url.replace("\r", "").replace("\n", "")
    except Exception:
      next_url = ""
    if next_url.startswith("/") and not next_url.startswith("//"):
      return redirect(next_url)

  return redirect(
    url_for(
      "billing_invoices.clients.client_deposit_ledger",
      client_id=client_id,
      currency=currency,
      **_deposit_ledger_redirect_scope_kwargs(business_profile_id),
    )
  )


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
def edit_client(client_id):
  conn = get_db()
  client = _get_client_or_404(conn, client_id)

  if request.method == "POST":
    try:
      f = request.form
      b_email = (f.get("biz_tax_invoice_email") or "").strip()
      b_loc = (f.get("biz_business_location") or "").strip()
      b_head = (f.get("biz_head_office_location") or "").strip()

      email = (f.get("email") or "").strip() or b_email
      address = (f.get("address") or "").strip() or (b_loc or b_head)

      cur_mgr = _get_row_value(client, "manager", None, None)
      manager = (
        (f.get("manager") or "").strip()
        or (cur_mgr or "").strip()
        or (f.get("biz_representative_name") or "").strip()
      )
      b_company = (f.get("biz_company_name") or "").strip()
      search_tags = invoice_client_search_tags_fast(
        [
          f["name"],
          b_company,
          f.get("biz_reg_number", "").strip(),
          f.get("biz_corp_registration_number", "").strip(),
          f.get("phone", "").strip(),
        ],
      )

      conn.execute(
        """
        UPDATE clients SET
          name=?, email=?, phone=?, address=?, manager=?, notes=?, search_tags=?,
          biz_reg_number=?, biz_company_name=?, biz_representative_name=?, biz_opening_date=?,
          biz_corp_registration_number=?, biz_business_location=?, biz_head_office_location=?,
          biz_business_type=?, biz_tax_invoice_email=?
        WHERE id=?
      """,
        (
          f["name"],
          email,
          f.get("phone", "").strip(),
          address,
          manager,
          f.get("notes"),
          search_tags,
          f.get("biz_reg_number", "").strip(),
          b_company,
          f.get("biz_representative_name", "").strip(),
          f.get("biz_opening_date", "").strip(),
          f.get("biz_corp_registration_number", "").strip(),
          b_loc,
          b_head,
          f.get("biz_business_type", "").strip(),
          b_email,
          client_id,
        ),
      )
      conn.commit()
      try:
        enqueue_invoice_client_post_save(client_id)
      except Exception as exc:
        report_swallowed_exception(
          exc,
          context="billing_invoices.clients.edit_client.enqueue_post_save",
          log_key="billing_invoices.clients.edit_client.enqueue_post_save",
          log_window_seconds=300,
        )
      return redirect(url_for("billing_invoices.clients.list_clients"))
    finally:
      conn.close()

  # GET: Fetch attachments for biz_info prefill
  try:
    attachments = conn.execute(
      "SELECT analysis_meta FROM client_attachments WHERE client_id=? ORDER BY uploaded_at DESC",
      (client_id,),
    ).fetchall()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.edit_client.attachments",
      log_key="billing_invoices.clients.edit_client.attachments",
      log_window_seconds=300,
    )
    attachments = []

  biz_info = None
  for r in attachments:
    try:
      meta = _get_row_value(r, "analysis_meta", 0)
      if meta:
        d = json.loads(meta)
        if d.get("biz_reg"):
          biz_info = d["biz_reg"]
          break
    except (TypeError, ValueError, json.JSONDecodeError):
      continue

  conn.close()
  return render_template("client_form.html", client=client, biz_info=biz_info)


@bp.route("/<int:client_id>/delete", methods=["POST"])
@role_required("admin")
def delete_client(client_id):
  conn = get_db()
  client = _get_client_or_404(conn, client_id)

  # Check invoices
  inv_count = conn.execute(
    "SELECT COUNT(*) FROM invoices WHERE client_id=?", (client_id,)
  ).fetchone()[0]
  if inv_count > 0:
    conn.close()
    flash(f"Invoice {inv_count}items cannot be deleted.", "error")
    return redirect(url_for("billing_invoices.clients.view_client", client_id=client_id))

  # Backup
  try:
    backup_path = _create_backup_file()
    user = get_current_user()
    _write_backup_meta(
      backup_path,
      source="forced",
      note=f"pre-delete client id={client_id} name={client['name']}",
      tags=["pre-delete", "client", f"id:{client_id}"],
      created_by=(user["id"] if user else None),
    )
    log_audit(
      "backup.pre_delete",
      "client",
      client_id,
      json.dumps({"path": backup_path, "name": client["name"]}),
    )
  except Exception as exc:
    # Backup Delete Open  ( Open)
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.delete_client.backup",
      log_key="billing_invoices.clients.delete_client.backup",
      log_window_seconds=300,
    )

  ipm_client_id = None
  try:
    ipm_client_id_val = _get_row_value(client, "ipm_client_id", None, None)
    if ipm_client_id_val is not None:
      ipm_client_id = int(ipm_client_id_val)
  except Exception:
    ipm_client_id = None

  if ipm_client_id:
    try:
      case_count = conn.execute(
        "SELECT COUNT(*) FROM cases WHERE client_id=?", (ipm_client_id,)
      ).fetchone()[0]
    except Exception:
      case_count = 0

    if int(case_count or 0) > 0:
      conn.close()
      flash(" Client Link Matter cannot be deleted.", "error")
      return redirect(url_for("billing_invoices.clients.view_client", client_id=client_id))

    # Soft delete instead of hard delete
    try:
      now_ts = datetime.now().isoformat()
      conn.execute("BEGIN")
      # Soft delete in main.clients (CRM table)
      conn.execute(
        "UPDATE main.clients SET is_deleted=TRUE, deleted_at=? WHERE id=?",
        (
          now_ts,
          ipm_client_id,
        ),
      )
      # Soft delete in billing clients (same table in unified mode)
      conn.execute(
        "UPDATE clients SET is_deleted=TRUE, deleted_at=? WHERE id=?",
        (
          now_ts,
          client_id,
        ),
      )
      conn.execute("COMMIT")
    except Exception:
      try:
        conn.execute("ROLLBACK")
      except Exception as rollback_exc:
        report_swallowed_exception(
          rollback_exc,
          context="billing_invoices.clients.delete_client.rollback",
          log_key="billing_invoices.clients.delete_client.rollback",
          log_window_seconds=300,
        )
      conn.close()
      flash("Client Delete Error .", "error")
      return redirect(url_for("billing_invoices.clients.view_client", client_id=client_id))
    conn.close()

    log_audit("client.delete", "client", client_id, json.dumps({"name": client["name"]}))
    flash("Client Delete.", "success")
    return redirect(url_for("billing_invoices.clients.list_clients"))

  # Soft delete instead of hard delete
  now_ts = datetime.now().isoformat()
  conn.execute(
    "UPDATE clients SET is_deleted=TRUE, deleted_at=? WHERE id=?",
    (
      now_ts,
      client_id,
    ),
  )
  conn.commit()
  conn.close()

  log_audit("client.delete", "client", client_id, json.dumps({"name": client["name"]}))
  flash("Client Delete.", "success")
  return redirect(url_for("billing_invoices.clients.list_clients"))


# ==============================================================================
# 6. Routes: Merge, Undo & Reassign
# ==============================================================================


@bp.route("/merge", methods=["POST"])
@role_required("admin")
def merge_clients():
  try:
    from app.services.client.client_merge_service import ClientMergeService

    target_id = int(request.form.get("target_id") or 0)
    source_ids = [int(s) for s in request.form.getlist("source_ids[]") if s.strip()]
    source_ids = sorted({s for s in source_ids if s != target_id})

    if not target_id or not source_ids:
      flash(" target is invalid.", "error")
      return redirect(url_for("billing_invoices.clients.list_clients"))

    merge_notes = (request.form.get("merge_notes", "1") or "").lower() in (
      "1",
      "true",
      "on",
      "yes",
    )
    user = get_current_user()

    target_crm = ensure_ipm_client_link_from_invoice_client(target_id)
    source_crm_ids: List[int] = []
    for sid in source_ids:
      sc = ensure_ipm_client_link_from_invoice_client(sid)
      if sc:
        source_crm_ids.append(int(sc.id))

    source_crm_ids = sorted({i for i in source_crm_ids if i != int(target_crm.id)})
    if not source_crm_ids:
      flash(" target is invalid.", "error")
      return redirect(url_for("billing_invoices.clients.list_clients"))

    result = ClientMergeService.merge_clients(
      target_client_id=int(target_crm.id),
      source_client_ids=source_crm_ids,
      merge_notes=merge_notes,
      merged_by=(user["id"] if user else None),
      reason="Invoice UI merge",
      backup_required=True,
      backup_attachments=True,
    )
    attachment_issues = ClientMergeService.collect_attachment_move_issues(
      result.get("invoice", {})
    )
    flash(
      f"{len(result['source_client_ids'])}items Client .",
      "success",
    )
    if any(attachment_issues.values()):
      flash(
        "File Go / exists.  Confirm "
        f"( {attachment_issues['missing']}items, "
        f" {attachment_issues['copy_failures']}items, "
        f"Delete {attachment_issues['delete_failures']}items).",
        "warning",
      )
    return redirect(url_for("billing_invoices.clients.view_client", client_id=target_id))
  except InvoiceBridgeError as e:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.clients.merge_clients.db_session_rollback",
        log_key="billing_invoices.clients.merge_clients.db_session_rollback",
        log_window_seconds=300,
      )
    current_app.logger.exception(
      "Merge failed (request_id=%s): invoice bridge error",
      getattr(g, "request_id", None),
    )
    flash(f" Error : {e}", "error")
    return redirect(url_for("billing_invoices.clients.list_clients"))
  except Exception:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.clients.merge_clients.db_session_rollback",
        log_key="billing_invoices.clients.merge_clients.db_session_rollback",
        log_window_seconds=300,
      )
    current_app.logger.exception(
      "Merge failed (request_id=%s)",
      getattr(g, "request_id", None),
    )
    flash(" Error .", "error")
    return redirect(url_for("billing_invoices.clients.list_clients"))


@bp.route("/undo_merge/<int:log_id>", methods=["POST"])
@role_required("admin")
def undo_merge(log_id: int):
  conn = get_db()
  try:
    row = conn.execute(
      "SELECT * FROM client_merge_log WHERE id=? AND undone_at IS NULL", (log_id,)
    ).fetchone()
    if not row:
      flash("  .", "error")
      return redirect(url_for("billing_invoices.clients.list_clients"))

    # Parse keys safely
    def g(k, idx):
      return _get_row_value(row, k, idx)

    target_id = g("target_id", 2)
    sources = json.loads(g("sources_json", 3) or "[]")
    inv_map = json.loads(g("invoice_map_json", 4) or "{}")
    notes_appended = g("notes_appended", 5)

    invoice_map = inv_map if isinstance(inv_map, dict) else {}
    ledger_map = {}
    attachments_map = {}
    if isinstance(inv_map, dict) and "invoices" in inv_map:
      invoice_map = inv_map.get("invoices") or {}
      ledger_map = inv_map.get("client_deposit_ledger") or {}
      attachments_map = inv_map.get("client_attachments") or {}

    # Restore Clients
    for s in sources:
      if not conn.execute("SELECT 1 FROM clients WHERE id=?", (s["id"],)).fetchone():
        conn.execute(
          "INSERT INTO clients (id, name, email, phone, address, notes) VALUES (?,?,?,?,?,?)",
          (
            s["id"],
            s["name"],
            s.get("email"),
            s.get("phone"),
            s.get("address"),
            s.get("notes"),
          ),
        )

    # Restore Invoices
    for inv_id, old_cid in invoice_map.items():
      conn.execute("UPDATE invoices SET client_id=? WHERE id=?", (old_cid, inv_id))

    # Restore Ledger
    for row_id, old_cid in ledger_map.items():
      conn.execute(
        "UPDATE client_deposit_ledger SET client_id=? WHERE id=?",
        (old_cid, row_id),
      )

    # Restore Client Attachments (DB + files, best-effort)
    if attachments_map and target_id:
      base_dir = current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients")
      tgt_dir = os.path.join(base_dir, f"client_{int(target_id)}")
      for att_id, info in attachments_map.items():
        if not isinstance(info, dict):
          continue
        old_cid = info.get("client_id")
        if not old_cid:
          continue
        stored_name = str(info.get("stored_name") or "")
        stored_after = str(info.get("stored_name_after") or stored_name)
        dst_dir = os.path.join(base_dir, f"client_{int(old_cid)}")
        os.makedirs(dst_dir, exist_ok=True)

        dst_name = stored_name or stored_after
        if dst_name and os.path.exists(os.path.join(dst_dir, dst_name)):
          dst_name = _unique_stored_name(dst_dir, dst_name)
        src_path = os.path.join(tgt_dir, stored_after)
        dst_path = os.path.join(dst_dir, dst_name)
        try:
          if stored_after and os.path.exists(src_path):
            shutil.move(src_path, dst_path)
        except Exception as exc:
          # Best-effort file restore: DB state is still restored even if a file move fails.
          report_swallowed_exception(
            exc,
            context="billing_invoices.clients.undo_merge.restore_attachment_file",
            log_key="billing_invoices.clients.undo_merge.restore_attachment_file",
            log_window_seconds=300,
          )

        conn.execute(
          "UPDATE client_attachments SET client_id=?, stored_name=? WHERE id=?",
          (old_cid, dst_name, att_id),
        )

    # Restore Notes (Remove appended)
    if notes_appended:
      cur_target = conn.execute(
        "SELECT notes FROM clients WHERE id=?", (target_id,)
      ).fetchone()
      if cur_target:
        curr = _get_row_value(cur_target, "notes", 0, "") or ""
        if notes_appended in curr:
          new_n = curr.replace(notes_appended, "").strip()
          # Cleanup newlines
          new_n = "\n\n".join([p.strip() for p in new_n.split("\n\n") if p.strip()])
          conn.execute("UPDATE clients SET notes=? WHERE id=?", (new_n, target_id))

    conn.execute(
      "UPDATE client_merge_log SET undone_at=CURRENT_TIMESTAMP WHERE id=?",
      (log_id,),
    )
    conn.commit()
    flash(" Cancel Done.", "success")
    return redirect(url_for("billing_invoices.clients.view_client", client_id=target_id))
  except Exception as e:
    conn.rollback()
    flash(f"Cancel : {e}", "error")
    return redirect(url_for("billing_invoices.clients.list_clients"))
  finally:
    conn.close()


@bp.route("/reassign_invoices", methods=["POST"])
def reassign_invoices():
  conn = get_db()
  try:
    from_id = int(request.form.get("from_client_id") or 0)
    inv_ids = [int(i) for i in request.form.getlist("invoice_ids[]") if i.isdigit()]
    dest_id_raw = request.form.get("dest_client_id")

    if not inv_ids:
      flash("Select Invoice not available.", "error")
      return redirect(url_for("billing_invoices.clients.view_client", client_id=from_id))

    dest_id = int(dest_id_raw) if dest_id_raw and dest_id_raw.isdigit() else None

    # Create new client if needed
    if not dest_id:
      name = (request.form.get("new_name") or "").strip()
      if not name:
        flash("New client Name enter.", "error")
        return redirect(url_for("billing_invoices.clients.view_client", client_id=from_id))
      cur = conn.execute(
        "INSERT INTO clients (name, email, phone, address, notes) VALUES (?,?,?,?,?)",
        (
          name,
          request.form.get("new_email"),
          request.form.get("new_phone"),
          request.form.get("new_address"),
          request.form.get("new_notes"),
        ),
      )
      dest_id = cur.lastrowid

    # Reassign
    placeholders = ",".join(["?"] * len(inv_ids))
    conn.execute(
      f"UPDATE invoices SET client_id=? WHERE id IN ({placeholders})",
      (dest_id, *inv_ids),
    )
    conn.commit()

    log_audit(
      "client.invoices_reassign",
      "client",
      dest_id,
      json.dumps({"from": from_id, "count": len(inv_ids)}),
    )
    flash(f"Invoice {len(inv_ids)}items Go.", "success")
    return redirect(url_for("billing_invoices.clients.view_client", client_id=dest_id))

  except Exception:
    conn.rollback()
    flash("Invoice Go .", "error")
    return redirect(
      url_for(
        "billing_invoices.clients.view_client", client_id=request.form.get("from_client_id")
      )
    )
  finally:
    conn.close()


# ==============================================================================
# 7. JSON APIs
# ==============================================================================


@bp.route("/search.json")
def search_clients_json():
  conn = get_db()
  q = request.args.get("q", "").strip()
  limit = _safe_int(request.args.get("limit", 20), 20, 1, 50)

  rows = []
  if q:
    if is_compact_query(q):
      # -only Search: times from  Filters
      rows_all = conn.execute(
        """
        SELECT c.id, c.name, c.email, c.phone, c.search_tags, c.biz_company_name, c.biz_reg_number,
            COUNT(DISTINCT i.id) AS invoice_count,
            COALESCE(SUM(i.total), 0) AS total_revenue,
            MAX(i.issue_date) AS last_invoice_date
        FROM clients c
        LEFT JOIN invoices i ON i.client_id = c.id
        WHERE c.is_deleted IS NOT TRUE
        GROUP BY c.id
        ORDER BY invoice_count DESC, c.name
        """,
      ).fetchall()
      q_compact = to_compact(q)
      filtered = []
      for r in rows_all:
        name = str(_get_row_value(r, "name", 1, "") or "")
        email = str(_get_row_value(r, "email", 2, "") or "")
        phone = str(_get_row_value(r, "phone", 3, "") or "")
        search_tags = str(_get_row_value(r, "search_tags", None, "") or "")
        biz_company = str(_get_row_value(r, "biz_company_name", None, "") or "")
        biz_reg = str(_get_row_value(r, "biz_reg_number", None, "") or "")
        text = " ".join([name, email, phone, search_tags, biz_company, biz_reg])
        if q_compact in to_compact(text):
          filtered.append(r)
      rows = filtered[:limit]
    else:
      search_clause, search_params = sql_ci_contains_any(
        [
          "c.name",
          "c.email",
          "c.phone",
          "c.search_tags",
          "c.biz_company_name",
          "c.biz_reg_number",
        ],
        q,
      )
      rows = []
      if search_clause:
        rows = conn.execute(
          """
          SELECT c.id, c.name, c.email, c.phone,
              COUNT(DISTINCT i.id) AS invoice_count,
              COALESCE(SUM(i.total), 0) AS total_revenue,
              MAX(i.issue_date) AS last_invoice_date
          FROM clients c
          LEFT JOIN invoices i ON i.client_id = c.id
          WHERE c.is_deleted IS NOT TRUE
           AND """
          + search_clause
          + """
          GROUP BY c.id
          ORDER BY invoice_count DESC, c.name
          LIMIT ?
        """,
          tuple(search_params + [limit]),
        ).fetchall()
  conn.close()

  results = []
  for r in rows:
    d = {
      "id": _get_row_value(r, "id", 0),
      "name": _get_row_value(r, "name", 1),
      "email": _get_row_value(r, "email", 2),
      "phone": _get_row_value(r, "phone", 3),
      "invoice_count": _get_row_value(r, "invoice_count", 4),
      "total_revenue": _get_row_value(r, "total_revenue", 5),
      "last_invoice_date": _get_row_value(r, "last_invoice_date", 6),
    }
    d["total_revenue"] = float(d["total_revenue"] or 0)
    results.append(d)
  return jsonify({"results": results})


@bp.route("/get.json")
def get_client_json():
  cid = request.args.get("id", "")
  if not cid.isdigit():
    return jsonify({"error": "invalid id"}), 400
  conn = get_db()
  r = conn.execute("SELECT id, name FROM clients WHERE id=?", (cid,)).fetchone()
  conn.close()
  if not r:
    return jsonify({"error": "not found"}), 404
  return jsonify(
    {
      "id": _get_row_value(r, "id", 0),
      "name": _get_row_value(r, "name", 1),
    }
  )


@bp.route("/brief.json")
def brief_clients_json():
  ids = [int(x) for x in (request.args.get("ids") or "").split(",") if x.strip().isdigit()]
  if not ids:
    return jsonify({"results": []})

  conn = get_db()
  placeholders = ",".join(["?"] * len(ids))
  rows = conn.execute(
    f"""
    SELECT c.id, c.name, c.email, c.phone,
        COUNT(DISTINCT i.id) as invoice_count,
        COALESCE(SUM(i.total), 0) as total_revenue,
        MAX(i.issue_date) as last_invoice_date
    FROM clients c
    LEFT JOIN invoices i ON i.client_id = c.id
    WHERE c.id IN ({placeholders})
    GROUP BY c.id
  """,
    tuple(ids),
  ).fetchall()
  conn.close()

  results = []
  for r in rows:
    d = {
      "id": _get_row_value(r, "id", 0),
      "name": _get_row_value(r, "name", 1),
      "email": _get_row_value(r, "email", 2),
      "phone": _get_row_value(r, "phone", 3),
      "invoice_count": _get_row_value(r, "invoice_count", 4),
      "total_revenue": _get_row_value(r, "total_revenue", 5),
      "last_invoice_date": _get_row_value(r, "last_invoice_date", 6),
    }
    d["total_revenue"] = float(d["total_revenue"] or 0)
    results.append(d)
  return jsonify({"results": results})


# ==============================================================================
# 8. Routes: Attachments (Modified for Tesseract-Free Logic)
# ==============================================================================


@bp.route("/<int:client_id>/attachments", methods=["GET"])
def list_client_attachments(client_id: int):
  conn = get_db()
  rows = conn.execute(
    "SELECT id, original_name, size, uploaded_at, content_type, analysis_meta FROM client_attachments WHERE client_id=? ORDER BY uploaded_at DESC",
    (client_id,),
  ).fetchall()
  conn.close()

  items = []
  for r in rows:
    meta = _get_row_value(r, "analysis_meta", 5)
    items.append(
      {
        "id": _get_row_value(r, "id", 0),
        "name": _get_row_value(r, "original_name", 1),
        "size": _get_row_value(r, "size", 2),
        "uploaded_at": _get_row_value(r, "uploaded_at", 3),
        "content_type": _get_row_value(r, "content_type", 4),
        "analysis": json.loads(meta) if meta else None,
      }
    )
  return jsonify(items)


@bp.route("/<int:client_id>/attachments/upload", methods=["POST"])
def upload_client_attachments(client_id: int):
  """
  [Refactored] Tesseract .
  PDF: pypdf  ->  -> (Text Mode / Vision Mode) Auto quarter
  Image: Vision Mode 
  """
  conn = get_db()
  try:
    # Validate Client
    if not conn.execute("SELECT 1 FROM clients WHERE id=?", (client_id,)).fetchone():
      abort(404)

    files = request.files.getlist("files[]") or request.files.getlist("files")
    if not files and request.files.get("file"):
      files = [request.files.get("file")]

    if not files:
      return jsonify({"success": False, "error": "File not available."}), 400

    upload_dir = _client_attachment_dir(client_id)
    api_key = get_openai_api_key(allow_legacy=False)
    user = get_current_user()
    saved_list = []
    max_bytes = _max_attachment_bytes()

    for f in files:
      if not f or not f.filename:
        continue
      if not _allowed_attachment(f.filename):
        continue
      if max_bytes:
        try:
          content_len = int(getattr(f, "content_length", 0) or 0)
        except Exception:
          content_len = 0
        if content_len and content_len > max_bytes:
          raise _UploadTooLarge("File .")

      # Save File
      clean_name = _sanitize_filename_preserve_unicode(f.filename)
      stored_name = _unique_stored_name(upload_dir, clean_name)
      dst = os.path.join(upload_dir, stored_name)
      try:
        size = _save_upload_stream(f, dst, max_bytes=max_bytes)
        _validate_stored_attachment(dst, filename=f.filename)
      except Exception:
        _remove_file_if_exists(dst)
        raise
      ctype = f.mimetype

      # -------------------------------------------------------
      # LLM Analysis (Text vs Vision) - No Tesseract
      # -------------------------------------------------------
      analysis_meta = json.dumps({"biz_reg": {}}, ensure_ascii=False)
      first_page_text = ""

      # DB Insert
      cur = conn.execute(
        """
        INSERT INTO client_attachments (
          client_id, original_name, stored_name, content_type, size,
          uploaded_by, first_page_text, analysis_meta
        ) VALUES (?,?,?,?,?,?,?,?)
      """,
        (
          client_id,
          f.filename,
          stored_name,
          ctype,
          size,
          user["id"] if user else None,
          first_page_text,
          analysis_meta,
        ),
      )

      # Auto Apply Logic
      if analysis_meta:
        try:
          m = json.loads(analysis_meta)
          if m.get("biz_reg"):
            _auto_apply_bizreg_to_client(conn, client_id, m["biz_reg"])
        except Exception as exc:
          report_swallowed_exception(
            exc,
            context="billing_invoices.clients.upload.parse_analysis_meta",
            log_key="billing_invoices.clients.upload.parse_analysis_meta",
            log_window_seconds=300,
          )

      saved_list.append(
        {
          "id": cur.lastrowid,
          "name": f.filename,
          "analysis": json.loads(analysis_meta) if analysis_meta else None,
        }
      )

      try:
        log_audit(
          "client.attachment.upload",
          "client",
          client_id,
          json.dumps({"name": f.filename, "method": "llm_optimized"}),
        )
      except Exception as exc:
        # Audit logging should not block uploads, but shouldn't be silent either.
        report_swallowed_exception(
          exc,
          context="billing_invoices.clients.upload.log_audit",
          log_key="billing_invoices.clients.upload.log_audit",
          log_window_seconds=300,
        )

    conn.commit()
    return jsonify({"success": True, "attachments": saved_list})

  except _UploadTooLarge as e:
    conn.rollback()
    current_app.logger.warning("Upload rejected: %s", e)
    return jsonify({"success": False, "error": str(e)}), 413
  except UploadSecurityError:
    conn.rollback()
    return jsonify({"success": False, "error": "Upload  failed."}), 400
  except Exception as e:
    conn.rollback()
    current_app.logger.exception("Upload failed")
    return jsonify({"success": False, "error": str(e)}), 500
  finally:
    conn.close()


@bp.route("/<int:client_id>/attachments/<int:att_id>/download")
def download_client_attachment(client_id: int, att_id: int):
  debug = request.args.get("debug") == "1" and is_admin(current_user)
  debug_info = {
    "requested_client_id": client_id,
    "requested_attachment_id": att_id,
    "resolved_client_id": client_id,
    "row_source": None,
    "row": None,
    "attachment_dir": None,
    "stored_path": None,
    "absolute_path": None,
    "open_ok": None,
    "open_error": None,
    "stored_exists": None,
    "files": [],
  }

  def _list_files(dir_path: str):
    try:
      files = [
        f
        for f in os.listdir(dir_path)
        if os.path.isfile(os.path.join(dir_path, f)) and _allowed_attachment(f)
      ]
      return files[:50]
    except Exception:
      return []

  def _debug_response(status_code: int):
    return jsonify({"ok": status_code < 400, "debug": debug_info}), status_code

  def _send_path(path: str, download_name: str):
    try:
      return send_file(path, as_attachment=True, download_name=download_name)
    except Exception:
      try:
        with open(path, "rb") as fh:
          data = fh.read()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return send_file(
          BytesIO(data),
          mimetype=mime,
          as_attachment=True,
          download_name=download_name,
        )
      except Exception:
        current_app.logger.exception("Attachment download failed")
        abort(404)

  conn = get_db()
  row = conn.execute(
    "SELECT original_name, stored_name FROM client_attachments WHERE id=? AND client_id=?",
    (att_id, client_id),
  ).fetchone()
  resolved_client_id = client_id
  if row:
    debug_info["row_source"] = "direct"

  if not row and not unified_clients_enabled():
    try:
      alt = conn.execute(
        "SELECT id FROM clients WHERE ipm_client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,),
      ).fetchone()
      if alt:
        resolved_client_id = int(alt[0])
        row = conn.execute(
          "SELECT original_name, stored_name FROM client_attachments WHERE id=? AND client_id=?",
          (att_id, resolved_client_id),
        ).fetchone()
        if row:
          debug_info["row_source"] = "alt"
    except Exception:
      resolved_client_id = client_id

  if not row:
    d = _client_attachment_dir(resolved_client_id)
    debug_info["attachment_dir"] = d
    debug_info["files"] = _list_files(d)
    conn.close()
    if debug:
      return _debug_response(404)
    abort(404)

  conn.close()
  debug_info["resolved_client_id"] = resolved_client_id
  debug_info["row"] = {
    "original_name": _get_row_value(row, "original_name", 0),
    "stored_name": _get_row_value(row, "stored_name", 1),
  }

  d = _client_attachment_dir(resolved_client_id)
  debug_info["attachment_dir"] = d
  sn = _get_row_value(row, "stored_name", 1)
  on = _get_row_value(row, "original_name", 0)

  sn_raw = str(sn or "")
  sn2 = os.path.basename(sn_raw)
  if not sn2 or sn2 != sn_raw:
    if debug:
      return _debug_response(404)
    abort(404)
  sn = sn2

  file_path = os.path.abspath(os.path.join(d, sn))
  debug_info["stored_path"] = file_path
  debug_info["absolute_path"] = file_path
  debug_info["stored_exists"] = os.path.exists(file_path)
  if debug:
    try:
      with open(file_path, "rb"):
        pass
      debug_info["open_ok"] = True
    except Exception as exc:
      debug_info["open_ok"] = False
      debug_info["open_error"] = str(exc)

  if not os.path.exists(file_path):
    debug_info["files"] = _list_files(d)
    if debug:
      return _debug_response(404)
    abort(404)

  if debug:
    return _debug_response(200)
  return _send_path(file_path, on)


@bp.route("/<int:client_id>/attachments/<int:att_id>/delete", methods=["POST"])
def delete_client_attachment(client_id: int, att_id: int):
  if not is_invoice_manager(current_user):
    abort(403)

  conn = get_db()
  row = conn.execute(
    "SELECT stored_name FROM client_attachments WHERE id=? AND client_id=?",
    (att_id, client_id),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)

  sn = _get_row_value(row, "stored_name", 0)
  sn_raw = str(sn or "")
  sn2 = os.path.basename(sn_raw)
  if not sn2 or sn2 != sn_raw:
    sn = None
  else:
    sn = sn2
  try:
    if sn:
      path = os.path.join(_client_attachment_dir(client_id), sn)
      if os.path.exists(path):
        os.remove(path)
  except Exception as exc:
    # Best-effort cleanup; the DB row is deleted regardless.
    report_swallowed_exception(
      exc,
      context="billing_invoices.clients.delete_attachment.remove_file",
      log_key="billing_invoices.clients.delete_attachment.remove_file",
      log_window_seconds=300,
    )

  conn.execute("DELETE FROM client_attachments WHERE id=?", (att_id,))
  conn.commit()
  conn.close()

  if request.is_json:
    return jsonify({"success": True})
  return redirect(url_for("billing_invoices.clients.view_client", client_id=client_id))


@bp.route("/<int:client_id>/attachments/<int:att_id>/meta", methods=["POST"])
def update_client_attachment_meta(client_id: int, att_id: int):
  conn = get_db()
  row = conn.execute(
    "SELECT analysis_meta FROM client_attachments WHERE id=? AND client_id=?",
    (att_id, client_id),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)

  memo = (request.get_json(silent=True) or request.form).get("user_memo", "").strip()[:4000]

  existing = _get_row_value(row, "analysis_meta", 0)
  try:
    meta = json.loads(existing) if existing else {}
  except Exception:
    meta = {}
  if not isinstance(meta, dict):
    meta = {}

  meta["user_memo"] = memo
  conn.execute(
    "UPDATE client_attachments SET analysis_meta=? WHERE id=?",
    (json.dumps(meta, ensure_ascii=False), att_id),
  )
  conn.commit()
  conn.close()

  return jsonify({"success": True, "analysis": meta})
