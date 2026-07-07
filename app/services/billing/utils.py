from __future__ import annotations

import html
import io
import os
import re
import secrets
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple

from flask import abort, current_app
from werkzeug.utils import secure_filename

from app.services.core.config_service import ConfigService
from app.utils.error_logging import report_swallowed_exception
from app.utils.search import sql_raw_ci_contains_any
from app.utils.upload_io import UploadTooLarge, resolve_first_positive_int
from app.utils.upload_io import save_upload_stream as _save_upload_stream_impl

# ===== Numbers and calculations =====

# Decimal places by currency. Keep USD for legacy imported invoices.
CURRENCY_SCALE = {"USD": 0, "USD": 2, "EUR": 2, "JPY": 0, "CNY": 2}
_LOGO_URL_PREFIX = "/accounting/invoice-system/uploads/"
_LEGACY_LOGO_URL_PREFIX = "/uploads/"


def _runtime_bool_config(key: str, default: bool = False) -> bool:
  return ConfigService.get_bool(key, current_app.config.get(key, default))


def accounting_feature_disabled() -> bool:
  return _runtime_bool_config("INVOICEAPP_DISABLE_ACCOUNTING_FEATURES", False)


def to_minor(amount: Decimal, currency: str) -> int:
  """Convert an amount to the smallest currency unit, such as cents for USD."""
  scale = CURRENCY_SCALE.get((currency or "USD").upper(), 2)
  q = Decimal(10) ** (-scale)
  return int(amount.quantize(q, rounding=ROUND_HALF_UP) * (10**scale))


def from_minor(amount_minor: int, currency: str) -> Decimal:
  """Convert the smallest currency unit back to a Decimal amount."""
  scale = CURRENCY_SCALE.get((currency or "USD").upper(), 2)
  return Decimal(amount_minor) / (10**scale)


def d(v) -> Decimal:
  return Decimal(str(v or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _clamp_discount(pct: Decimal) -> Decimal:
  return max(Decimal("0"), min(Decimal("100"), pct))


def compute_totals(
  items: List[Dict[str, Any]], vat_rate: Decimal
) -> Tuple[Decimal, Decimal, Decimal]:
  """Calculate subtotal, tax, and total from line items, excluding estimates."""
  subtotal = Decimal("0")
  taxable = Decimal("0")
  for it in items:
    # Estimated items are excluded from the invoice amount.
    if int(it.get("is_estimated", 0)) == 1:
      continue

    item_type = it.get("item_type") or "service"
    item_type = item_type.strip().lower() if hasattr(item_type, "strip") else str(item_type)

    if item_type == "foreign":
      fx_rate_used = it.get("fx_rate_used")
      try:
        fx_rate_used = (
          Decimal(str(fx_rate_used)) if fx_rate_used is not None else Decimal("0")
        )
      except (InvalidOperation, TypeError, ValueError):
        fx_rate_used = Decimal("0")

      if fx_rate_used > 0:
        fx_fee = it.get("fx_fee")
        fx_gov = it.get("fx_gov")
        fx_markup = it.get("fx_markup")
        try:
          fx_fee = d(fx_fee)
        except (InvalidOperation, TypeError, ValueError):
          fx_fee = Decimal("0")
        try:
          fx_gov = d(fx_gov)
        except (InvalidOperation, TypeError, ValueError):
          fx_gov = Decimal("0")
        try:
          fx_markup = d(fx_markup)
        except (InvalidOperation, TypeError, ValueError):
          fx_markup = Decimal("0")

        item_amt = (
          (fx_fee + fx_gov) * fx_rate_used * (Decimal("1") + (fx_markup / Decimal("100")))
        )
        subtotal += item_amt
        if int(it.get("is_taxable", 0)):
          taxable += item_amt
        continue

    # Keep signed qty/unit to support adjustment rows (e.g. prepaid offsets).
    qty = d(it.get("qty"))
    unit = d(it.get("unit_price"))
    disc = _clamp_discount(d(it.get("discount", 0)))
    item_amt = qty * unit
    discounted = item_amt - (item_amt * disc / Decimal("100"))
    subtotal += discounted
    if int(it.get("is_taxable", 1)):
      taxable += discounted
  vr = d(vat_rate)
  vat_multiplier = (vr / Decimal("100")) if vr > 1 else vr
  tax = (taxable * vat_multiplier).quantize(Decimal("0.01"))
  total = subtotal + tax
  return subtotal, tax, total


def compute_totals_minor(
  items: List[Dict[str, Any]], vat_rate: Decimal, currency: str
) -> Tuple[int, int, int]:
  """Calculate subtotal, tax, and total as minor-unit integers."""
  subtotal, tax, total = compute_totals(items, vat_rate)
  return (
    to_minor(subtotal, currency),
    to_minor(tax, currency),
    to_minor(total, currency),
  )


def currency_format(amount: Decimal, currency: str) -> str:
  """Format a Decimal amount with a currency code."""
  currency = (currency or "USD").upper()
  scale = CURRENCY_SCALE.get(currency, 2)
  if scale == 0:
    amt = int(Decimal(amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{amt:,} {currency}"
  q = Decimal(10) ** (-scale)
  return f"{Decimal(amount).quantize(q, rounding=ROUND_HALF_UP):,} {currency}"


def currency_format_minor(amount_minor: int, currency: str) -> str:
  """Format a minor-unit amount with a currency code."""
  amount = from_minor(amount_minor, currency)
  return currency_format(amount, currency)


def to_compact(s):
  if not s:
    return ""
  result = []
  for ch in str(s).casefold():
    if ch.isalnum():
      result.append(ch)
  return "".join(result)


def is_compact_query(s):
  if not s:
    return False
  s = str(s).strip()
  if not s:
    return False
  for ch in s:
    if ch.isspace():
      continue
    if not ch.isalnum():
      return False
  return True


def sql_ci_contains_any(
  expressions: list[str] | tuple[str, ...], value: str | None
) -> tuple[str, list[str]]:
  """Backward-compatible alias for raw SQL search clauses in billing routes."""
  return sql_raw_ci_contains_any(expressions, value)


def normalize_case_linked_filter(value: str | None) -> str:
  normalized = str(value or "").strip().lower()
  if normalized in {"linked", "1", "true", "yes", "on"}:
    return "linked"
  if normalized in {"unlinked", "0", "false", "no", "off"}:
    return "unlinked"
  return ""


def invoice_has_case_link_sql(invoice_alias: str = "invoices") -> str:
  alias = str(invoice_alias or "invoices").strip() or "invoices"
  active_link = (
    "COALESCE(LOWER(CAST(eicm.is_deleted AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )
  return (
    "("
    f"COALESCE(TRIM({alias}.ipm_case_id), '') <> '' "
    f"OR COALESCE(TRIM({alias}.ipm_case_ref), '') <> '' "
    f"OR EXISTS ("
    f"SELECT 1 FROM external_invoice_case_map eicm WHERE eicm.external_invoice_id = {alias}.id "
    f"AND {active_link}"
    f")"
    ")"
  )


def invoice_case_link_filter_sql(case_linked: str, invoice_alias: str = "invoices") -> str | None:
  normalized = normalize_case_linked_filter(case_linked)
  if not normalized:
    return None
  has_case_link_sql = invoice_has_case_link_sql(invoice_alias)
  if normalized == "linked":
    return has_case_link_sql
  return f"(NOT {has_case_link_sql})"


# NOTE: Importing i18n locally to avoid circular imports if i18n also uses utils (rare but safe)
def status_label(code: str) -> str:
  # Small adapter for i18n
  from .i18n import t

  return {
    "draft": t("status_draft"),
    "sent": t("status_sent"),
    "paid": t("status_paid"),
    "payment_pending": t("status_payment_pending"),
    "void": t("status_void"),
    "tax_issued": t("status_tax_issued"),
    "cash_issued": t("status_cash_issued"),
    "processed": t("status_processed"),
    "pre_overdue": t("status_pre_overdue"),
  }.get(code, code)


# ===== Uploads and images =====
def allowed_file(filename: str) -> bool:
  allowed = current_app.config.get("ALLOWED_EXTENSIONS", {"png", "jpg", "jpeg", "gif", "webp"})
  return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed


def safe_image(file_storage):
  if not file_storage or not file_storage.filename:
    abort(400, "No file was provided.")
  if not allowed_file(file_storage.filename):
    abort(400, "This file type is not allowed.")
  if not (file_storage.mimetype or "").startswith("image/"):
    abort(400, "Only image files can be uploaded.")
  head = file_storage.stream.read(32)
  file_storage.stream.seek(0)
  # Simple magic number check
  if head.startswith(b"\x89PNG\r\n\x1a\n"):
    return file_storage
  if head.startswith(b"\xff\xd8\xff"):
    return file_storage
  if head[:6] in (b"GIF87a", b"GIF89a"):
    return file_storage
  if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
    return file_storage
  abort(400, "The image is corrupt or unsupported.")


class _UploadTooLarge(UploadTooLarge, ValueError):
  pass


def _max_logo_bytes() -> int:
  return resolve_first_positive_int(
    ("LOGO_MAX_BYTES", "FILE_ASSET_MAX_BYTES", "UPLOAD_MAX_BYTES", "MAX_CONTENT_LENGTH"),
    default=0,
  )


def _save_upload_stream(file_obj, dst: str, *, max_bytes: int) -> int:
  return _save_upload_stream_impl(
    file_obj,
    dst,
    max_bytes=max_bytes,
    too_large_exc=_UploadTooLarge,
    context_prefix="billing_invoices.utils._save_upload_stream",
    report_seek_errors=False,
    log_window_seconds=300,
  )


def save_logo(file_storage, old_path: str | None) -> str | None:
  if not file_storage or not file_storage.filename:
    return old_path
  file_storage = safe_image(file_storage)
  filename = secure_filename(file_storage.filename)
  ext = filename.rsplit(".", 1)[1].lower() if "." in filename else "png"
  new_name = f"{secrets.token_hex(4)}.{ext}"

  # Use constant UPLOAD_FOLDER from config
  abs_dir = current_app.config.get("UPLOAD_FOLDER", "uploads")
  os.makedirs(abs_dir, exist_ok=True)
  abs_path = os.path.join(abs_dir, new_name)
  max_bytes = _max_logo_bytes()
  if max_bytes:
    try:
      content_len = int(getattr(file_storage, "content_length", 0) or 0)
    except Exception:
      content_len = 0
    if content_len and content_len > max_bytes:
      raise _UploadTooLarge("The file is too large.")
  _save_upload_stream(file_storage, abs_path, max_bytes=max_bytes)

  # Clean up old file
  if old_path:
    if old_path.startswith(_LOGO_URL_PREFIX):
      old_name = old_path[len(_LOGO_URL_PREFIX) :]
    elif old_path.startswith(_LEGACY_LOGO_URL_PREFIX):
      old_name = old_path[len(_LEGACY_LOGO_URL_PREFIX) :]
    else:
      old_name = None
    if old_name:
      old_abs = os.path.join(abs_dir, old_name)
      if os.path.exists(old_abs):
        try:
          os.remove(old_abs)
        except OSError:
          pass
  return f"{_LOGO_URL_PREFIX}{new_name}"


def invoice_logo_url(path: str | None) -> str:
  if not path:
    return ""
  if path.startswith(_LOGO_URL_PREFIX):
    return path
  if path.startswith(_LEGACY_LOGO_URL_PREFIX):
    return f"{_LOGO_URL_PREFIX}{path[len(_LEGACY_LOGO_URL_PREFIX) :]}"
  return path


# ===== Safe note rendering =====
def format_notes(text: str) -> str:
  if not text:
    return ""
  text = html.escape(text)
  text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
  text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

  lines = text.split("\n")
  out, stack = [], []

  def close_list():
    if stack:
      out.append(f"</{stack.pop()}>")

  for line in lines:
    s = line.strip()
    if s.startswith("- "):
      if not stack or stack[-1] != "ul":
        close_list()
        out.append('<ul style="margin:4px 0; padding-left:20px;">')
        stack.append("ul")
      out.append(f"<li>{s[2:]}</li>")
    elif len(s) >= 3 and s[0].isdigit() and s[1] in ")." and s[2] == " ":
      if not stack or stack[-1] != "ol":
        close_list()
        out.append('<ol style="margin:4px 0; padding-left:20px;">')
        stack.append("ol")
      out.append(f"<li>{s[3:]}</li>")
    else:
      close_list()
      out.append(s.replace(" ", "&nbsp;&nbsp;"))
  close_list()
  return "<br>".join(out)
