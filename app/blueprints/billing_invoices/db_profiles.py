from __future__ import annotations

import json
import random
import re
import time

from flask import abort, current_app
from sqlalchemy.exc import DBAPIError

from app.services.billing.db_core import (
  DB_ERRORS,
  DB_OP_ERRORS,
  DatabaseError,
  _is_sqlite,
  get_db,
  row_to_dict,
)
from app.utils.error_logging import report_swallowed_exception


def _runtime_schema_bootstrap_enabled() -> bool:
  try:
    return bool(current_app.config.get("TESTING")) or bool(
      current_app.config.get("INVOICEAPP_RUNTIME_SCHEMA_BOOTSTRAP", False)
    )
  except RuntimeError:
    return False


def get_business_profile(bp_id=None):
  conn = get_db()
  if bp_id:
    row = conn.execute("SELECT * FROM business_profile WHERE id=?", (bp_id,)).fetchone()
  else:
    try:
      row = conn.execute(
        "SELECT * FROM business_profile ORDER BY COALESCE(sort_order, 0), id LIMIT 1"
      ).fetchone()
    except DB_ERRORS:
      row = conn.execute("SELECT * FROM business_profile ORDER BY id LIMIT 1").fetchone()
  conn.close()
  return row_to_dict(row) if row else None


def get_all_business_profiles():
  conn = get_db()
  try:
    rows = conn.execute(
      "SELECT * FROM business_profile ORDER BY COALESCE(sort_order, 0), name, id"
    ).fetchall()
  except DB_ERRORS:
    rows = conn.execute("SELECT * FROM business_profile ORDER BY name").fetchall()
  conn.close()
  return [row_to_dict(r) for r in rows]


def ensure_tax_invoice_profiles(conn) -> bool:
  changed = False
  if _runtime_schema_bootstrap_enabled():
    try:
      conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tax_invoice_profiles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          tax_id TEXT,
          ceo_name TEXT,
          address TEXT,
          biz_type TEXT,
          biz_class TEXT,
          email TEXT,
          phone TEXT,
          is_default INTEGER DEFAULT 0,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
      )
    except DB_ERRORS:
      pass
    try:
      conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tax_invoice_profiles_default ON tax_invoice_profiles(is_default)"
      )
    except DB_ERRORS:
      pass
  try:
    default_row = conn.execute(
      "SELECT id FROM tax_invoice_profiles WHERE is_default=1 LIMIT 1"
    ).fetchone()
  except DB_ERRORS:
    default_row = None
  if not default_row:
    try:
      first_row = conn.execute(
        "SELECT id FROM tax_invoice_profiles ORDER BY id LIMIT 1"
      ).fetchone()
    except DB_ERRORS:
      first_row = None
    if first_row:
      try:
        conn.execute(
          "UPDATE tax_invoice_profiles SET is_default=1 WHERE id=?",
          (first_row[0],),
        )
        changed = True
      except DB_ERRORS:
        pass
    else:
      try:
        bp_row = conn.execute(
          "SELECT name, address, email, phone, tax_id FROM business_profile "
          "ORDER BY COALESCE(sort_order, 0), id LIMIT 1"
        ).fetchone()
      except DB_ERRORS:
        bp_row = None
      try:
        if bp_row:
          conn.execute(
            "INSERT INTO tax_invoice_profiles (name, address, email, phone, tax_id, is_default) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (bp_row[0], bp_row[1], bp_row[2], bp_row[3], bp_row[4]),
          )
        else:
          conn.execute(
            "INSERT INTO tax_invoice_profiles (name, is_default) VALUES ('My Company', 1)"
          )
        changed = True
      except DB_ERRORS:
        pass
  return changed


def get_tax_invoice_profile(profile_id=None):
  conn = get_db()
  changed = ensure_tax_invoice_profiles(conn)
  if profile_id:
    row = conn.execute(
      "SELECT * FROM tax_invoice_profiles WHERE id=?", (profile_id,)
    ).fetchone()
  else:
    row = conn.execute(
      "SELECT * FROM tax_invoice_profiles WHERE is_default=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
      row = conn.execute("SELECT * FROM tax_invoice_profiles ORDER BY id LIMIT 1").fetchone()
  if changed:
    try:
      conn.commit()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_profiles.get_tax_invoice_profile.commit",
        log_key="billing_invoices.db_profiles.get_tax_invoice_profile.commit",
        log_window_seconds=300,
      )
  conn.close()
  return row_to_dict(row) if row else None


def get_all_tax_invoice_profiles():
  conn = get_db()
  changed = ensure_tax_invoice_profiles(conn)
  rows = conn.execute(
    "SELECT * FROM tax_invoice_profiles ORDER BY is_default DESC, name, id"
  ).fetchall()
  if changed:
    try:
      conn.commit()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_profiles.get_all_tax_invoice_profiles.commit",
        log_key="billing_invoices.db_profiles.get_all_tax_invoice_profiles.commit",
        log_window_seconds=300,
      )
  conn.close()
  return [row_to_dict(r) for r in rows]


_INVOICE_PREFIX_RE = re.compile(r"^INV-(\d{8})-$")


def _extract_invoice_date_key(prefix: str) -> str:
  p = str(prefix or "").strip()
  m = _INVOICE_PREFIX_RE.match(p)
  if m:
    return m.group(1)
  return p


def _ensure_invoice_number_counters_table(conn) -> None:
  if not _runtime_schema_bootstrap_enabled():
    return
  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS invoice_number_counters (
      date_key TEXT PRIMARY KEY,
      last_no INTEGER NOT NULL DEFAULT 0,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
  )


def _max_existing_invoice_no_for_prefix(conn, prefix: str) -> int:
  max_no = 0
  try:
    rows = conn.execute(
      "SELECT number FROM invoices WHERE number LIKE ?",
      (f"{prefix}%",),
    ).fetchall()
  except DB_ERRORS:
    return 0

  for row in rows or []:
    try:
      num = row["number"]
    except Exception:
      try:
        num = row[0]
      except Exception:
        num = None
    if not isinstance(num, str) or not num.startswith(prefix):
      continue
    suffix = num[len(prefix) :].strip()
    if not suffix.isdigit():
      continue
    try:
      seq = int(suffix)
    except Exception:
      continue
    if seq > max_no:
      max_no = seq
  return max_no


def next_invoice_number(conn, business_profile_id: int, prefix: str) -> str:
  """Global, day-based invoice numbering (system-wide) with concurrency safety."""
  _ = business_profile_id # Backward-compatible signature; numbering is no longer per business.
  max_attempts = 8
  date_key = _extract_invoice_date_key(prefix)
  if not date_key:
    abort(400, "Invalid invoice number prefix.")

  for attempt in range(max_attempts):
    try:
      conn.execute("BEGIN IMMEDIATE")
      _ensure_invoice_number_counters_table(conn)

      select_sql = "SELECT last_no FROM invoice_number_counters WHERE date_key=?"
      if not _is_sqlite(conn):
        select_sql += " FOR UPDATE"
      row = conn.execute(select_sql, (date_key,)).fetchone()

      if row:
        n = int(row[0] or 0) + 1
        conn.execute(
          "UPDATE invoice_number_counters SET last_no=?, updated_at=CURRENT_TIMESTAMP WHERE date_key=?",
          (n, date_key),
        )
      else:
        seed = _max_existing_invoice_no_for_prefix(conn, prefix)
        n = max(1, int(seed) + 1)
        try:
          conn.execute(
            """
            INSERT INTO invoice_number_counters (date_key, last_no, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (date_key, n),
          )
        except DB_ERRORS as e:
          conn.rollback()
          msg = str(e).lower()
          if (
            "duplicate" in msg or "unique" in msg or "constraint" in msg
          ) and attempt < max_attempts - 1:
            wait_time = (0.03 * (2**attempt)) + random.random() * 0.02
            time.sleep(wait_time)
            continue
          raise

      conn.commit()
      return f"{prefix}{n:04d}"

    except DB_OP_ERRORS as e:
      conn.rollback()
      msg = str(e).lower()
      if (
        "locked" in msg or "deadlock" in msg or "timeout" in msg
      ) and attempt < max_attempts - 1:
        wait_time = (0.03 * (2**attempt)) + random.random() * 0.02
        time.sleep(wait_time)
        continue
      raise
    except (DBAPIError, DatabaseError, ValueError, TypeError) as e:
      conn.rollback()
      msg = str(e).lower()
      if (
        "deadlock" in msg
        or "serialize" in msg
        or "timeout" in msg
        or "could not obtain lock" in msg
      ) and attempt < max_attempts - 1:
        wait_time = (0.03 * (2**attempt)) + random.random() * 0.02
        time.sleep(wait_time)
        continue
      raise

  abort(503, "Invoice numbering is delayed. Please try again shortly.")


def snapshot_of_profile(profile_row) -> str:
  """Business profile snapshot JSON at issue time."""
  return json.dumps(
    {
      "name": profile_row["name"],
      "address": profile_row["address"],
      "email": profile_row["email"],
      "phone": profile_row["phone"],
      "tax_id": profile_row["tax_id"],
      "bank_account": profile_row["bank_account"],
      "logo_path": profile_row["logo_path"],
    }
  )


# ---------------- FX rates cache helpers ---------------- #
