"""
Unified Invoice Service Layer

This module provides a service abstraction over the billing_invoices database,
enabling consistent access patterns and business logic encapsulation.
Now uses SQLAlchemy via the billing_invoices.db module for PostgreSQL compatibility.

Classes:
  InvoiceService: Core invoice CRUD operations
  PaymentService: Payment management and reconciliation
  IntegrationService: External provider (App) integration
  InvoiceLinkService: N:N case-invoice mapping
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

# Import get_db from billing_invoices for PostgreSQL-compatible connection
from app.services.billing.db_core import (
  _actual_table_name,
  get_db,
  row_get,
  row_to_dict,
  safe_json_parse,
)
from app.services.billing.utils import CURRENCY_SCALE, to_minor
from app.utils.error_logging import report_swallowed_exception
from app.utils.search import sql_raw_ci_contains_any

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class InvoiceSummary:
  """Summary view of an invoice."""

  id: int
  number: Optional[str]
  client_id: int
  client_name: Optional[str]
  issue_date: Optional[str]
  due_date: Optional[str]
  status: Optional[str]
  billing_status: Optional[str]
  payment_status: Optional[str]
  total_minor: int
  currency: str
  business_profile_id: int


@dataclass
class PaymentRecord:
  """Single payment record."""

  id: int
  invoice_id: int
  paid_at: str
  amount_minor: int
  currency: str
  method: Optional[str]
  reference: Optional[str]
  verified: bool
  created_at: str


@dataclass
class CaseLink:
  """Invoice-case mapping record."""

  id: int
  invoice_id: int
  case_id: Optional[int]
  matter_id: Optional[str]
  our_ref: Optional[str]
  role: str
  created_at: str


@dataclass
class IntegrationRecord:
  """External provider integration record."""

  id: int
  invoice_id: int
  provider: str
  external_invoice_id: Optional[str]
  external_invoice_number: Optional[str]
  external_case_id: Optional[str]
  sync_status: str
  last_synced_at: Optional[str]
  created_at: str


# ---------------------------------------------------------------------------
# Database Connection Helper
# ---------------------------------------------------------------------------


def _get_connection():
  """Get database connection via billing_invoices.db (PostgreSQL compatible)."""
  return get_db()


def _table_name(base: str) -> str:
  """Resolve the actual invoice table name in integrated/unified mode (prefix-safe)."""
  return _actual_table_name(str(base))


def _currency_multiplier_sql(column_expr: str) -> str:
  """Return SQL CASE for minor-unit multiplier based on currency code."""
  cases = []
  for cur, scale in CURRENCY_SCALE.items():
    try:
      multiplier = 10 ** int(scale)
    except Exception:
      multiplier = 100
    cases.append(f"WHEN UPPER(COALESCE({column_expr}, 'USD')) = '{cur}' THEN {multiplier}")
  return "CASE " + " ".join(cases) + " ELSE 100 END"


def _not_deleted_sql(column_expr: str) -> str:
  """Return a raw-SQL soft-delete predicate that works for bool/int/text DB values."""
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def _amount_to_minor(value: Any, currency: Optional[str]) -> int:
  cur = (currency or "USD").strip().upper() or "USD"
  if value is None:
    return 0
  raw = str(value).replace(",", "").replace(" ", "").strip()
  if not raw:
    return 0
  if cur in {"USD", "JPY"}:
    try:
      return int(Decimal(raw).quantize(Decimal("1")))
    except Exception:
      return 0
  try:
    return int(to_minor(Decimal(raw), cur))
  except Exception:
    return 0


def _payment_meta_paid_minor(meta_value: Any, currency: Optional[str]) -> int:
  meta = safe_json_parse(meta_value, {})
  if not isinstance(meta, dict):
    return 0

  deposits = meta.get("deposits")
  if isinstance(deposits, list):
    total = 0
    for rec in deposits:
      if not isinstance(rec, dict):
        continue
      rec_currency = rec.get("currency") or meta.get("currency") or currency
      total += _amount_to_minor(rec.get("deposit"), rec_currency)
    if total:
      return int(total)

  return _amount_to_minor(meta.get("deposit"), meta.get("currency") or currency)


def _payment_meta_paid_at(meta_value: Any, fallback: Optional[str] = None) -> str:
  meta = safe_json_parse(meta_value, {})
  if not isinstance(meta, dict):
    return fallback or ""
  deposits = meta.get("deposits")
  if isinstance(deposits, list) and deposits:
    for rec in reversed(deposits):
      if isinstance(rec, dict) and rec.get("date"):
        return str(rec.get("date") or "")
  for key in ("verified_at", "date", "paid_at"):
    if meta.get(key):
      return str(meta.get(key) or "")
  return fallback or ""


def _deposit_applied_minor(conn, invoice_id: int) -> int:
  try:
    row = conn.execute(
      f"""
      SELECT COALESCE(SUM(amount_minor), 0) AS total
      FROM {_table_name("client_deposit_ledger")}
      WHERE related_invoice_id=?
       AND entry_type IN ('apply','cancel_apply')
      """,
      (int(invoice_id),),
    ).fetchone()
    return max(0, -int(row_get(row, "total", 0, 0) or 0))
  except Exception:
    return 0


def _normalized_payment_total_minor(conn, invoice_id: int) -> int:
  try:
    row = conn.execute(
      f"""
      SELECT COALESCE(SUM(amount_minor), 0) as total
      FROM {_table_name("invoice_payments")}
      WHERE invoice_id = ?
       AND {_not_deleted_sql("is_deleted")}
      """,
      (int(invoice_id),),
    ).fetchone()
    return int(row_get(row, "total", 0, 0) or 0)
  except Exception:
    return 0


def _invoice_total_minor(invoice: Dict[str, Any]) -> int:
  currency = (invoice.get("currency") or "USD").upper()
  try:
    total_minor = int(invoice.get("total_minor") or 0)
  except Exception:
    total_minor = 0
  if total_minor <= 0 and invoice.get("total") is not None:
    total_minor = _amount_to_minor(invoice.get("total"), currency)
  return int(total_minor or 0)


def _invoice_marked_paid(invoice: Dict[str, Any]) -> bool:
  payment_status = str(invoice.get("payment_status") or "").strip().lower()
  legacy_status = str(invoice.get("status") or "").strip().lower()
  try:
    verified = int(invoice.get("payment_verified") or 0) == 1
  except Exception:
    verified = bool(invoice.get("payment_verified"))
  return verified or payment_status in {"paid", "overpaid"} or legacy_status == "paid"


def _effective_paid_minor(conn, invoice: Dict[str, Any]) -> int:
  try:
    invoice_id = int(invoice.get("id") or 0)
  except Exception:
    invoice_id = 0
  if invoice_id <= 0:
    return 0

  currency = (invoice.get("currency") or "USD").upper()
  paid_minor = max(
    _normalized_payment_total_minor(conn, invoice_id),
    _payment_meta_paid_minor(invoice.get("payment_meta"), currency),
    _deposit_applied_minor(conn, invoice_id),
  )
  total_minor = _invoice_total_minor(invoice)
  if _invoice_marked_paid(invoice) and total_minor > 0:
    paid_minor = max(paid_minor, total_minor)
  return int(paid_minor or 0)


# ---------------------------------------------------------------------------
# InvoiceService
# ---------------------------------------------------------------------------


class InvoiceService:
  """
  Core invoice operations.

  This service provides read/write access to the canonical invoice data
  stored in the billing_invoices SQLite database.
  """

  @staticmethod
  def get_by_id(invoice_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single invoice by ID."""
    conn = _get_connection()
    try:
      row = conn.execute(
        f"""
        SELECT i.*, c.name as client_name, bp.name as business_name
        FROM {_table_name("invoices")} i
        LEFT JOIN {_table_name("clients")} c ON c.id = i.client_id
        LEFT JOIN {_table_name("business_profile")} bp ON bp.id = i.business_profile_id
        WHERE i.id = ?
      """,
        (invoice_id,),
      ).fetchone()

      if row:
        return row_to_dict(row)
      return None
    finally:
      conn.close()

  @staticmethod
  def get_by_number(number: str) -> Optional[Dict[str, Any]]:
    """Fetch invoice by invoice number."""
    conn = _get_connection()
    try:
      row = conn.execute(
        f"""
        SELECT i.*, c.name as client_name
        FROM {_table_name("invoices")} i
        LEFT JOIN {_table_name("clients")} c ON c.id = i.client_id
        WHERE i.number = ?
      """,
        (number,),
      ).fetchone()

      if row:
        return row_to_dict(row)
      return None
    finally:
      conn.close()

  @staticmethod
  def list_invoices(
    *,
    client_id: Optional[int] = None,
    status: Optional[str] = None,
    billing_status: Optional[str] = None,
    payment_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
  ) -> List[Dict[str, Any]]:
    """List invoices with optional filters."""
    conn = _get_connection()
    try:
      where_clauses = []
      params = []

      if client_id:
        where_clauses.append("i.client_id = ?")
        params.append(client_id)
      if status:
        where_clauses.append("i.status = ?")
        params.append(status)
      if billing_status:
        where_clauses.append("i.billing_status = ?")
        params.append(billing_status)
      if payment_status:
        where_clauses.append("i.payment_status = ?")
        params.append(payment_status)
      if date_from:
        where_clauses.append("i.issue_date >= ?")
        params.append(date_from)
      if date_to:
        where_clauses.append("i.issue_date <= ?")
        params.append(date_to)

      where_sql = ""
      if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

      rows = conn.execute(
        f"""
        SELECT i.*, c.name as client_name, bp.name as business_name
        FROM {_table_name("invoices")} i
        LEFT JOIN {_table_name("clients")} c ON c.id = i.client_id
        LEFT JOIN {_table_name("business_profile")} bp ON bp.id = i.business_profile_id
        {where_sql}
        ORDER BY i.id DESC
        LIMIT ? OFFSET ?
      """,
        params + [limit, offset],
      ).fetchall()

      return [row_to_dict(r) for r in rows]
    finally:
      conn.close()

  @staticmethod
  def search_invoices(*, q: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Search invoices by number, internal reference, client name, or notes."""
    q = (q or "").strip()
    if not q:
      return []
    conn = _get_connection()
    try:
      search_clause, search_params = sql_raw_ci_contains_any(
        ["i.number", "i.internal_reference", "c.name", "i.notes"],
        q,
      )
      if not search_clause:
        return []
      rows = conn.execute(
        f"""
        SELECT i.*, c.name as client_name, bp.name as business_name
        FROM {_table_name("invoices")} i
        LEFT JOIN {_table_name("clients")} c ON c.id = i.client_id
        LEFT JOIN {_table_name("business_profile")} bp ON bp.id = i.business_profile_id
        WHERE {search_clause}
        ORDER BY i.issue_date DESC, i.id DESC
        LIMIT ?
        """,
        tuple(search_params + [limit]),
      ).fetchall()
      return [row_to_dict(r) for r in rows]
    finally:
      conn.close()

  @staticmethod
  def get_line_items(invoice_id: int) -> List[Dict[str, Any]]:
    """Get line items for an invoice."""
    conn = _get_connection()
    try:
      rows = conn.execute(
        f"""
        SELECT * FROM {_table_name("line_items")}
        WHERE invoice_id = ?
        ORDER BY id
      """,
        (invoice_id,),
      ).fetchall()
      return [row_to_dict(r) for r in rows]
    finally:
      conn.close()

  @staticmethod
  def calculate_totals(invoice_id: int) -> Dict[str, Any]:
    """Calculate invoice totals from line items and payments."""
    conn = _get_connection()
    try:
      row = conn.execute(
        f"SELECT * FROM {_table_name('invoices')} WHERE id = ?",
        (invoice_id,),
      ).fetchone()
      if not row:
        return {}
      invoice = row_to_dict(row)
      currency = (invoice.get("currency") or "USD").upper()
      total_minor = _invoice_total_minor(invoice)
      paid_total = _effective_paid_minor(conn, invoice)
      outstanding = max(0, total_minor - paid_total)
    finally:
      conn.close()

    return {
      "total_minor": total_minor,
      "paid_total": paid_total,
      "outstanding": outstanding,
      "currency": currency or "USD",
      "is_paid": outstanding == 0 and paid_total > 0,
    }

  @staticmethod
  def get_client_statistics(
    start_date: str, end_date: str, limit: int = 30
  ) -> List[Dict[str, Any]]:
    """
    Get billing statistics grouped by client, using calculated totals to match the Invoice List view.
    Uses payment records with legacy payment_meta fallback; if an invoice is marked paid/overpaid,
    treat it as fully paid to keep stats aligned with invoice status.
    """
    conn = _get_connection()
    try:
      clients_tbl = _table_name("clients")
      invoices_tbl = _table_name("invoices")
      line_items_tbl = _table_name("line_items")
      payments_tbl = _table_name("invoice_payments")

      def _parse_payment_meta_minor(meta_val: Any, currency: Optional[str]) -> int:
        meta = safe_json_parse(meta_val, {})
        if not isinstance(meta, dict):
          return 0

        deposit = meta.get("deposit")
        if deposit is None and isinstance(meta.get("deposits"), list):
          total = 0
          for rec in meta.get("deposits") or []:
            if not isinstance(rec, dict):
              continue
            try:
              total += int(str(rec.get("deposit") or "0").replace(",", "").strip())
            except Exception:
              continue
          deposit = total

        if deposit is None:
          return 0

        cur = (meta.get("currency") or currency or "USD").strip().upper()
        if not cur:
          cur = "USD"
        if cur == "USD":
          try:
            return int(str(deposit or "0").replace(",", "").replace(" ", ""))
          except Exception:
            return 0
        try:
          return int(to_minor(Decimal(str(deposit).replace(",", "")), cur))
        except Exception:
          return 0

      # Helper SQL for component sums
      def _subq(item_type, tax_filter=""):
        base = f"SELECT SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)) FROM {line_items_tbl} li WHERE li.invoice_id = i.id AND li.item_type = '{item_type}' AND (li.is_estimated IS NULL OR li.is_estimated = 0)"
        if item_type == "foreign":
          # Complex foreign calc
          base = f"""SELECT SUM(
            CASE WHEN COALESCE(fx_rate_used, 0) > 0 THEN
              (COALESCE(fx_fee,0) + COALESCE(fx_gov,0)) * COALESCE(fx_rate_used, 0) * (1 + COALESCE(fx_markup,0)/100.0)
            ELSE
              (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          ) FROM {line_items_tbl} li WHERE li.invoice_id = i.id AND li.item_type = 'foreign' AND (li.is_estimated IS NULL OR li.is_estimated = 0)"""

        if tax_filter:
          base += f" AND {tax_filter}"
        return f"COALESCE(({base}), 0)"

      svc_sql = _subq("service")
      adm_sql = _subq("admin")
      frn_sql = _subq("foreign")
      frn_tax_sql = _subq("foreign", "COALESCE(li.is_taxable,0)=1")

      # (svc + frn_taxable) * rate
      tax_sql = f"(({svc_sql} + {frn_tax_sql}) * (CASE WHEN COALESCE(i.vat_rate, 0) > 1 THEN COALESCE(i.vat_rate, 0)/100.0 ELSE COALESCE(i.vat_rate, 0) END))"

      calc_total_sql = f"({svc_sql} + {adm_sql} + {frn_sql} + {tax_sql})"
      multiplier_sql = _currency_multiplier_sql("i.currency")

      # Subquery to get total payments per invoice
      payments_subq = f"COALESCE((SELECT SUM(p.amount_minor) FROM {payments_tbl} p WHERE p.invoice_id = i.id), 0)"
      payments_count_subq = (
        f"COALESCE((SELECT COUNT(1) FROM {payments_tbl} p WHERE p.invoice_id = i.id), 0)"
      )

      sql = f"""
        SELECT
          i.id as invoice_id,
          i.client_id,
          COALESCE(c.name, 'Unknown') as client_name,
          i.currency,
          i.payment_meta,
          i.payment_status,
          ({calc_total_sql}) * {multiplier_sql} as total_minor_calc,
          {payments_subq} as total_paid_minor,
          {payments_count_subq} as payment_count
        FROM {invoices_tbl} i
        JOIN {clients_tbl} c ON c.id = i.client_id
        WHERE i.issue_date >= ? AND i.issue_date <= ?
      """

      rows = conn.execute(sql, [start_date, end_date]).fetchall()
      by_client: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
      for r in rows:
        d = row_to_dict(r)
        client_id = d.get("client_id")
        client_name = d.get("client_name") or "Unknown"
        key = (client_id, client_name)

        try:
          billed_minor = int(d.get("total_minor_calc") or 0)
        except Exception:
          billed_minor = 0
        try:
          paid_minor = int(d.get("total_paid_minor") or 0)
        except Exception:
          paid_minor = 0
        try:
          payment_count = int(d.get("payment_count") or 0)
        except Exception:
          payment_count = 0
        legacy_paid_minor = _parse_payment_meta_minor(
          d.get("payment_meta"), d.get("currency")
        )
        if payment_count <= 0:
          if legacy_paid_minor:
            paid_minor = legacy_paid_minor
        elif legacy_paid_minor and legacy_paid_minor > paid_minor:
          paid_minor = legacy_paid_minor

        payment_status = (d.get("payment_status") or "").strip().lower()
        if payment_status in ("paid", "overpaid") and billed_minor > 0:
          paid_minor = max(paid_minor, billed_minor)

        if key not in by_client:
          by_client[key] = {
            "client_id": client_id,
            "client_name": client_name,
            "total_billed": 0,
            "total_paid": 0,
          }
        by_client[key]["total_billed"] += billed_minor
        by_client[key]["total_paid"] += paid_minor

      data = [v for v in by_client.values() if (v.get("total_billed") or 0) > 0]
      data.sort(key=lambda x: x.get("total_billed") or 0, reverse=True)
      return data[:limit]
    finally:
      conn.close()

  @staticmethod
  def get_monthly_statistics(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """
    Get billing statistics grouped by month, using calculated totals.
    """
    conn = _get_connection()
    try:
      invoices_tbl = _table_name("invoices")
      line_items_tbl = _table_name("line_items")

      dialect = getattr(conn, "dialect_name", "").lower()
      if not dialect and hasattr(conn, "engine"):
        dialect = conn.engine.dialect.name.lower()
      if not dialect:
        dialect = "sqlite"

      if dialect.startswith("sqlite"):
        month_expr = "strftime('%Y-%m', issue_date)"
      else:
        month_expr = "to_char(issue_date::date, 'YYYY-MM')"

      # Reuse subquery logic (duplicated for safety in this static method scope)
      def _subq(item_type, tax_filter=""):
        base = f"SELECT SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)) FROM {line_items_tbl} li WHERE li.invoice_id = i.id AND li.item_type = '{item_type}' AND (li.is_estimated IS NULL OR li.is_estimated = 0)"
        if item_type == "foreign":
          base = f"""SELECT SUM(
            CASE WHEN COALESCE(fx_rate_used, 0) > 0 THEN
              (COALESCE(fx_fee,0) + COALESCE(fx_gov,0)) * COALESCE(fx_rate_used, 0) * (1 + COALESCE(fx_markup,0)/100.0)
            ELSE
              (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          ) FROM {line_items_tbl} li WHERE li.invoice_id = i.id AND li.item_type = 'foreign' AND (li.is_estimated IS NULL OR li.is_estimated = 0)"""
        if tax_filter:
          base += f" AND {tax_filter}"
        return f"COALESCE(({base}), 0)"

      svc_sql = _subq("service")
      adm_sql = _subq("admin")
      frn_sql = _subq("foreign")
      frn_tax_sql = _subq("foreign", "COALESCE(li.is_taxable,0)=1")

      tax_sql = f"(({svc_sql} + {frn_tax_sql}) * (CASE WHEN COALESCE(i.vat_rate, 0) > 1 THEN COALESCE(i.vat_rate, 0)/100.0 ELSE COALESCE(i.vat_rate, 0) END))"
      calc_total_sql = f"({svc_sql} + {adm_sql} + {frn_sql} + {tax_sql})"
      multiplier_sql = _currency_multiplier_sql("i.currency")

      sql = f"""
        WITH calc_invoices AS (
          SELECT
            {month_expr} as month,
            i.billing_status,
            i.payment_status,
            ({calc_total_sql}) * {multiplier_sql} as total_minor_calc
          FROM {invoices_tbl} i
          WHERE i.issue_date >= ? AND i.issue_date <= ?
        )
        SELECT
          month,
          SUM(CASE
            WHEN billing_status NOT IN ('void', 'draft') OR billing_status IS NULL
            THEN total_minor_calc
            ELSE 0
          END) as billed,
          SUM(CASE
            WHEN payment_status IN ('paid', 'overpaid')
            THEN total_minor_calc
            ELSE 0
          END) as paid
        FROM calc_invoices
        GROUP BY month
        ORDER BY month
      """

      rows = conn.execute(sql, [start_date, end_date]).fetchall()
      return [row_to_dict(r) for r in rows]
    finally:
      conn.close()


# ---------------------------------------------------------------------------
# PaymentService
# ---------------------------------------------------------------------------


class PaymentService:
  """
  Payment management operations.

  Manages invoice_payments table - the normalized payment records
  replacing the legacy payment_meta JSON approach.
  """

  @staticmethod
  def get_payments(invoice_id: int) -> List[PaymentRecord]:
    """Get all payments for an invoice."""
    conn = _get_connection()
    try:
      rows = conn.execute(
        f"""
        SELECT * FROM {_table_name("invoice_payments")}
        WHERE invoice_id = ?
         AND {_not_deleted_sql("is_deleted")}
        ORDER BY paid_at DESC
      """,
        (invoice_id,),
      ).fetchall()

      records = [
        PaymentRecord(
          id=r["id"],
          invoice_id=r["invoice_id"],
          paid_at=r["paid_at"],
          amount_minor=r["amount_minor"],
          currency=r["currency"],
          method=r["method"],
          reference=r["reference"],
          verified=bool(r["verified"]),
          created_at=r["created_at"],
        )
        for r in rows
      ]
      if records:
        return records

      invoice_row = conn.execute(
        f"SELECT * FROM {_table_name('invoices')} WHERE id = ?",
        (invoice_id,),
      ).fetchone()
      if not invoice_row:
        return []
      invoice = row_to_dict(invoice_row)
      currency = (invoice.get("currency") or "USD").upper()
      deposit_paid = _deposit_applied_minor(conn, invoice_id)
      meta_paid = _payment_meta_paid_minor(invoice.get("payment_meta"), currency)
      total_minor = _invoice_total_minor(invoice)

      amount_minor = max(deposit_paid, meta_paid)
      method = "deposit" if deposit_paid >= meta_paid and deposit_paid > 0 else "legacy_meta"
      reference = "client_deposit_ledger" if method == "deposit" else "payment_meta"
      if amount_minor <= 0 and _invoice_marked_paid(invoice) and total_minor > 0:
        amount_minor = total_minor
        method = "status"
        reference = "payment_status"
      if amount_minor <= 0:
        return []

      return [
        PaymentRecord(
          id=-int(invoice_id),
          invoice_id=int(invoice_id),
          paid_at=_payment_meta_paid_at(
            invoice.get("payment_meta"), invoice.get("issue_date")
          ),
          amount_minor=int(amount_minor),
          currency=currency,
          method=method,
          reference=reference,
          verified=_invoice_marked_paid(invoice),
          created_at=str(invoice.get("issue_date") or ""),
        )
      ]
    finally:
      conn.close()

  @staticmethod
  def get_total_paid(invoice_id: int) -> int:
    """Get total paid amount (minor units) for an invoice."""
    conn = _get_connection()
    try:
      invoice_row = conn.execute(
        f"SELECT * FROM {_table_name('invoices')} WHERE id = ?",
        (invoice_id,),
      ).fetchone()
      if not invoice_row:
        return 0
      return _effective_paid_minor(conn, row_to_dict(invoice_row))
    finally:
      conn.close()

  @staticmethod
  def add_payment(
    invoice_id: int,
    amount_minor: int,
    paid_at: str,
    currency: str = "USD",
    method: Optional[str] = None,
    reference: Optional[str] = None,
    verified: bool = False,
    meta_json: Optional[str] = None,
    created_by: Optional[int] = None,
  ) -> int:
    """Add a new payment record. Returns the new payment ID."""
    conn = _get_connection()
    try:
      cur = conn.execute(
        f"""
        INSERT INTO {_table_name("invoice_payments")}
        (invoice_id, paid_at, amount_minor, currency, method, reference, verified, meta_json, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
        (
          invoice_id,
          paid_at,
          amount_minor,
          currency,
          method,
          reference,
          1 if verified else 0,
          meta_json,
          created_by,
          datetime.utcnow().isoformat(),
        ),
      )
      conn.commit()
      return cur.lastrowid
    finally:
      conn.close()

  @staticmethod
  def delete_payment(payment_id: int) -> bool:
    """Delete a payment record."""
    conn = _get_connection()
    try:
      conn.execute(
        f"""
        DELETE FROM {_table_name("invoice_payments")}
        WHERE id = ?
      """,
        (payment_id,),
      )
      conn.commit()
      return True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="invoice_services.PaymentService.delete_payment",
        log_key="invoice_services.PaymentService.delete_payment",
        log_window_seconds=300,
      )
      return False
    finally:
      conn.close()


# ---------------------------------------------------------------------------
# InvoiceLinkService
# ---------------------------------------------------------------------------


class InvoiceLinkService:
  """
  N:N Invoice-Case mapping operations.

  Manages invoice_case_map table for linking invoices to cases/matters.
  """

  @staticmethod
  def get_links(invoice_id: int) -> List[CaseLink]:
    """Get all case links for an invoice."""
    conn = _get_connection()
    try:
      rows = conn.execute(
        f"""
        SELECT * FROM {_table_name("invoice_case_map")}
        WHERE invoice_id = ?
         AND {_not_deleted_sql("is_deleted")}
        ORDER BY id
      """,
        (invoice_id,),
      ).fetchall()

      links = [
        CaseLink(
          id=r["id"],
          invoice_id=r["invoice_id"],
          case_id=r["case_id"],
          matter_id=r["matter_id"],
          our_ref=r["our_ref"],
          role=r["role"],
          created_at=r["created_at"],
        )
        for r in rows
      ]
      seen = {(link.case_id, link.matter_id or "", link.our_ref or "") for link in links}

      external_rows = conn.execute(
        f"""
        SELECT
          l.id,
          l.external_invoice_id AS invoice_id,
          NULL AS case_id,
          l.matter_id,
          COALESCE(m.our_ref, l.our_ref) AS our_ref,
          'primary' AS role,
          l.created_at
        FROM external_invoice_case_map l
        LEFT JOIN matter m ON m.matter_id = l.matter_id
        WHERE l.external_invoice_id = ?
         AND {_not_deleted_sql("l.is_deleted")}
        ORDER BY l.id
        """,
        (invoice_id,),
      ).fetchall()
      for r in external_rows or []:
        key = (
          None,
          row_get(r, "matter_id", default="") or "",
          row_get(r, "our_ref", default="") or "",
        )
        if key in seen:
          continue
        seen.add(key)
        links.append(
          CaseLink(
            id=row_get(r, "id", default=0),
            invoice_id=row_get(r, "invoice_id", default=invoice_id),
            case_id=None,
            matter_id=row_get(r, "matter_id", default=None),
            our_ref=row_get(r, "our_ref", default=None),
            role=row_get(r, "role", default="primary") or "primary",
            created_at=row_get(r, "created_at", default="") or "",
          )
        )
      return links
    finally:
      conn.close()

  @staticmethod
  def get_invoices_for_case(
    *, case_id: Optional[int] = None, matter_id: Optional[str] = None
  ) -> List[int]:
    """Get invoice IDs linked to a case."""
    conn = _get_connection()
    try:
      if case_id:
        rows = conn.execute(
          f"""
          SELECT invoice_id FROM {_table_name("invoice_case_map")}
          WHERE case_id = ?
           AND {_not_deleted_sql("is_deleted")}
        """,
          (case_id,),
        ).fetchall()
      elif matter_id:
        rows = conn.execute(
          f"""
          SELECT invoice_id FROM {_table_name("invoice_case_map")}
          WHERE matter_id = ?
           AND {_not_deleted_sql("is_deleted")}
          UNION
          SELECT external_invoice_id AS invoice_id
          FROM external_invoice_case_map
          WHERE matter_id = ?
           AND {_not_deleted_sql("is_deleted")}
        """,
          (matter_id, matter_id),
        ).fetchall()
      else:
        return []

      return [r["invoice_id"] for r in rows]
    finally:
      conn.close()

  @staticmethod
  def add_link(
    invoice_id: int,
    *,
    case_id: Optional[int] = None,
    matter_id: Optional[str] = None,
    our_ref: Optional[str] = None,
    role: str = "primary",
  ) -> int:
    """Add a case link. Returns the new link ID."""
    conn = _get_connection()
    try:
      cur = conn.execute(
        f"""
        INSERT INTO {_table_name("invoice_case_map")}
        (invoice_id, case_id, matter_id, our_ref, role, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
      """,
        (invoice_id, case_id, matter_id, our_ref, role, datetime.utcnow().isoformat()),
      )
      if matter_id:
        conn.execute(
          """
          INSERT INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
          VALUES (?, ?, ?)
          ON CONFLICT DO NOTHING
          """,
          (matter_id, our_ref, invoice_id),
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
          (invoice_id, matter_id),
        )
      conn.commit()
      return cur.lastrowid
    finally:
      conn.close()

  @staticmethod
  def remove_link(link_id: int) -> bool:
    """Remove a case link."""
    conn = _get_connection()
    try:
      conn.execute(
        f"""
        DELETE FROM {_table_name("invoice_case_map")}
        WHERE id = ?
      """,
        (link_id,),
      )
      conn.commit()
      return True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="invoice_services.InvoiceLinkService.remove_link",
        log_key="invoice_services.InvoiceLinkService.remove_link",
        log_window_seconds=300,
      )
      return False
    finally:
      conn.close()


# ---------------------------------------------------------------------------
# IntegrationService
# ---------------------------------------------------------------------------


class IntegrationService:
  """
  External provider integration operations.

  Manages invoice_integrations table for internal and external systems.
  """

  PROVIDER_INTERNAL = "ipm"
  PROVIDER_LEGACY = "legacy"

  @staticmethod
  def get_integrations(invoice_id: int) -> List[IntegrationRecord]:
    """Get all integrations for an invoice."""
    conn = _get_connection()
    try:
      rows = conn.execute(
        f"""
        SELECT * FROM {_table_name("invoice_integrations")}
        WHERE invoice_id = ?
        ORDER BY id
      """,
        (invoice_id,),
      ).fetchall()

      return [
        IntegrationRecord(
          id=r["id"],
          invoice_id=r["invoice_id"],
          provider=r["provider"],
          external_invoice_id=r["external_invoice_id"],
          external_invoice_number=r["external_invoice_number"],
          external_case_id=r["external_case_id"],
          sync_status=r["sync_status"],
          last_synced_at=r["last_synced_at"],
          created_at=r["created_at"],
        )
        for r in rows
      ]
    finally:
      conn.close()

  @staticmethod
  def get_by_external_id(provider: str, external_id: str) -> Optional[Dict[str, Any]]:
    """Find integration by provider and external ID."""
    conn = _get_connection()
    try:
      row = conn.execute(
        f"""
        SELECT * FROM {_table_name("invoice_integrations")}
        WHERE provider = ? AND external_invoice_id = ?
      """,
        (provider, external_id),
      ).fetchone()

      if row:
        return row_to_dict(row)
      return None
    finally:
      conn.close()

  @staticmethod
  def add_integration(
    invoice_id: int,
    provider: str,
    *,
    external_invoice_id: Optional[str] = None,
    external_invoice_number: Optional[str] = None,
    external_invoice_url: Optional[str] = None,
    external_case_id: Optional[str] = None,
    external_case_ref: Optional[str] = None,
    meta_json: Optional[str] = None,
  ) -> int:
    """Add an integration record. Returns the new ID."""
    conn = _get_connection()
    try:
      cur = conn.execute(
        f"""
        INSERT INTO {_table_name("invoice_integrations")}
        (invoice_id, provider, external_invoice_id, external_invoice_number,
         external_invoice_url, external_case_id, external_case_ref,
         sync_status, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT DO NOTHING
      """,
        (
          invoice_id,
          provider,
          external_invoice_id,
          external_invoice_number,
          external_invoice_url,
          external_case_id,
          external_case_ref,
          meta_json,
          datetime.utcnow().isoformat(),
        ),
      )
      conn.commit()
      return cur.lastrowid
    finally:
      conn.close()

  @staticmethod
  def update_sync_status(integration_id: int, status: str) -> bool:
    """Update sync status of an integration."""
    conn = _get_connection()
    try:
      conn.execute(
        f"""
        UPDATE {_table_name("invoice_integrations")}
        SET sync_status = ?, last_synced_at = ?
        WHERE id = ?
      """,
        (status, datetime.utcnow().isoformat(), integration_id),
      )
      conn.commit()
      return True
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="invoice_services.IntegrationService.update_sync_status",
        log_key="invoice_services.IntegrationService.update_sync_status",
        log_window_seconds=300,
      )
      return False
    finally:
      conn.close()
