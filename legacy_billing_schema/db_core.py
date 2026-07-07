from __future__ import annotations

import json
import re
from typing import Any, Optional

from flask import current_app, g, has_request_context
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import DBAPIError, OperationalError

from app.extensions import db
from app.utils.error_logging import report_swallowed_exception


class _NoCloseConnection:
  def __init__(self, conn):
    self._conn = conn

  def close(self):
    return None

  def __getattr__(self, item):
    return getattr(self._conn, item)


class DatabaseError(Exception):
  pass


class DatabaseOperationalError(DatabaseError):
  pass


DB_ERRORS = (DBAPIError, DatabaseError)
DB_OP_ERRORS = (OperationalError, DatabaseOperationalError)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _is_safe_identifier(value: str) -> bool:
  return bool(value and _IDENTIFIER_RE.match(value))


def _get_engine():
  try:
    return db.get_engine(current_app)
  except Exception:
    try:
      return db.engine
    except Exception:
      db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
      if not db_uri:
        path = current_app.config.get("DB_PATH") or current_app.config.get(
          "INVOICE_MODULE_DB_PATH"
        )
        if path:
          db_uri = f"sqlite:///{path}"
      if not db_uri:
        raise RuntimeError("Database URI is not configured.")
      return create_engine(db_uri)


def _get_sa_connection(conn):
  if hasattr(conn, "sa_connection"):
    return conn.sa_connection
  inner = getattr(conn, "_conn", None)
  if inner is not None:
    if hasattr(inner, "sa_connection"):
      return inner.sa_connection
    return inner
  return conn


def _get_dialect_name(conn) -> str:
  name = ""
  if hasattr(conn, "dialect_name"):
    try:
      name = conn.dialect_name or ""
    except Exception:
      name = ""
  if not name:
    sa_conn = _get_sa_connection(conn)
    try:
      name = sa_conn.engine.dialect.name or ""
    except Exception:
      try:
        name = sa_conn.dialect.name or ""
      except Exception:
        name = ""
  return name.lower()


def _is_sqlite(conn) -> bool:
  return _get_dialect_name(conn).startswith("sqlite")


def _is_postgres(conn) -> bool:
  return _get_dialect_name(conn).startswith("postgres")


def _rewrite_begin_immediate(sql: str) -> str:
  return re.sub(r"(?i)\bBEGIN\s+IMMEDIATE\b", "BEGIN", sql)


def _rewrite_autoincrement(sql: str, dialect: str) -> str:
  if not sql:
    return sql
  out = sql
  if dialect.startswith("postgres"):
    out = re.sub(
      r"(?i)\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
      "SERIAL PRIMARY KEY",
      out,
    )
  out = re.sub(r"(?i)\bAUTOINCREMENT\b", "", out)
  return out


def _rewrite_insert_or_ignore(sql: str, dialect: str) -> str:
  if not sql or dialect.startswith("sqlite"):
    return sql
  pattern = r"(?is)\binsert\s+or\s+ignore\s+into\b"
  if not re.search(pattern, sql):
    return sql
  out = re.sub(pattern, "INSERT INTO", sql, count=1)
  if re.search(r"(?is)\bon\s+conflict\b", out):
    return out
  trimmed = out.rstrip()
  if trimmed.endswith(";"):
    trimmed = trimmed.rstrip(";").rstrip()
  return trimmed + " ON CONFLICT DO NOTHING"


def _split_insert_columns(columns_sql: str) -> list[str]:
  columns: list[str] = []
  for raw_column in columns_sql.split(","):
    column = raw_column.strip()
    if column:
      columns.append(column)
  return columns


def _normalized_identifier(identifier: str) -> str:
  return identifier.strip().strip('"').lower()


def _quote_qualified_identifier(identifier: str) -> str | None:
  parts = [part.strip().strip('"') for part in identifier.split(".")]
  if not parts or any(not _is_safe_identifier(part) for part in parts):
    return None
  return ".".join(f'"{part}"' for part in parts)


def _rewrite_insert_or_replace(sql: str, dialect: str) -> str:
  if not sql or dialect.startswith("sqlite"):
    return sql
  pattern = r"(?is)\binsert\s+or\s+replace\s+into\b"
  if not re.search(pattern, sql):
    return sql
  if re.search(r"(?is)\bon\s+conflict\b", sql):
    return re.sub(pattern, "INSERT INTO", sql, count=1)

  match = re.match(
    r"(?is)^(\s*)insert\s+or\s+replace\s+into\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*"
    r"\((?P<columns>[^)]+)\)\s*"
    r"values\s*\((?P<values>.+)\)\s*(?P<tail>;?\s*)$",
    sql,
  )
  if not match:
    return re.sub(pattern, "INSERT INTO", sql, count=1)

  columns = _split_insert_columns(match.group("columns"))
  if not columns:
    return re.sub(pattern, "INSERT INTO", sql, count=1)

  conflict_column = next(
    (column for column in columns if _normalized_identifier(column) == "id"),
    columns[0],
  )
  update_columns = [column for column in columns if column != conflict_column]
  statement = (
    f"{match.group(1)}INSERT INTO {match.group('table')} "
    f"({match.group('columns')}) VALUES ({match.group('values')}) "
    f"ON CONFLICT ({conflict_column})"
  )
  if update_columns:
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    statement = f"{statement} DO UPDATE SET {assignments}"
  else:
    statement = f"{statement} DO NOTHING"
  return statement + match.group("tail")


def _parse_insert_table(sql: str) -> str | None:
  match = re.match(
    r"(?is)^\s*insert\s+into\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\b",
    sql or "",
  )
  if not match:
    return None
  return match.group("table")


def _postgres_table_has_id(conn, table_name: str) -> bool:
  parts = [part.strip().strip('"') for part in table_name.split(".")]
  if not parts or any(not _is_safe_identifier(part) for part in parts):
    return False
  schema = parts[0] if len(parts) > 1 else "public"
  table = parts[-1]
  try:
    return bool(
      conn.exec_driver_sql(
        """
        SELECT EXISTS (
          SELECT 1
           FROM information_schema.columns
           WHERE table_schema = %s
            AND table_name = %s
            AND column_name = 'id'
        )
        """,
        (schema, table),
      ).scalar()
    )
  except Exception:
    return False


def _postgres_sync_insert_id(conn, table_name: str) -> int | None:
  quoted_table = _quote_qualified_identifier(table_name)
  if not quoted_table or not _postgres_table_has_id(conn, table_name):
    return None

  try:
    last_id = conn.exec_driver_sql(f"SELECT MAX(id) FROM {quoted_table}").scalar()
  except Exception:
    return None

  try:
    sequence_name = conn.exec_driver_sql(
      "SELECT pg_get_serial_sequence(%s, 'id')",
      (table_name,),
    ).scalar()
    if sequence_name and last_id is not None:
      conn.exec_driver_sql(
        "SELECT setval(%s::regclass, %s, true)",
        (sequence_name, int(last_id)),
      )
  except Exception:
    return int(last_id) if last_id is not None else None
  return int(last_id) if last_id is not None else None


def _rewrite_collation(sql: str, dialect: str) -> str:
  if not sql:
    return sql
  if dialect.startswith("sqlite"):
    return sql
  return re.sub(r"(?i)\s+collate\s+nocase\b", "", sql)


def _convert_qmark_to_percent(sql: str) -> str:
  if not sql or "?" not in sql:
    return sql
  out = []
  in_single = False
  in_double = False
  in_line_comment = False
  in_block_comment = False
  i = 0
  length = len(sql)
  while i < length:
    ch = sql[i]
    nxt = sql[i + 1] if i + 1 < length else ""
    if in_line_comment:
      out.append(ch)
      if ch == "\n":
        in_line_comment = False
      i += 1
      continue
    if in_block_comment:
      out.append(ch)
      if ch == "*" and nxt == "/":
        out.append(nxt)
        i += 2
        in_block_comment = False
        continue
      i += 1
      continue
    if not in_single and not in_double:
      if ch == "-" and nxt == "-":
        in_line_comment = True
        out.append(ch)
        out.append(nxt)
        i += 2
        continue
      if ch == "/" and nxt == "*":
        in_block_comment = True
        out.append(ch)
        out.append(nxt)
        i += 2
        continue
    if ch == "'" and not in_double:
      out.append(ch)
      if in_single and nxt == "'":
        out.append(nxt)
        i += 2
        continue
      in_single = not in_single
      i += 1
      continue
    if ch == '"' and not in_single:
      out.append(ch)
      in_double = not in_double
      i += 1
      continue
    if ch == "?" and not in_single and not in_double:
      out.append("%s")
      i += 1
      continue
    out.append(ch)
    i += 1
  return "".join(out)


def _escape_percent_for_pyformat(sql: str) -> str:
  if not sql or "%" not in sql:
    return sql
  return re.sub(r"%(?![s%])", "%%", sql)


def _split_sql_script(sql_script: str) -> list:
  if not sql_script:
    return []
  statements = []
  current = []
  in_single = False
  in_double = False
  in_line_comment = False
  in_block_comment = False
  i = 0
  length = len(sql_script)
  while i < length:
    ch = sql_script[i]
    nxt = sql_script[i + 1] if i + 1 < length else ""
    if in_line_comment:
      current.append(ch)
      if ch == "\n":
        in_line_comment = False
      i += 1
      continue
    if in_block_comment:
      current.append(ch)
      if ch == "*" and nxt == "/":
        current.append(nxt)
        i += 2
        in_block_comment = False
        continue
      i += 1
      continue
    if not in_single and not in_double:
      if ch == "-" and nxt == "-":
        in_line_comment = True
        current.append(ch)
        current.append(nxt)
        i += 2
        continue
      if ch == "/" and nxt == "*":
        in_block_comment = True
        current.append(ch)
        current.append(nxt)
        i += 2
        continue
    if ch == "'" and not in_double:
      current.append(ch)
      if in_single and nxt == "'":
        current.append(nxt)
        i += 2
        continue
      in_single = not in_single
      i += 1
      continue
    if ch == '"' and not in_single:
      current.append(ch)
      in_double = not in_double
      i += 1
      continue
    if ch == ";" and not in_single and not in_double:
      statement = "".join(current).strip()
      if statement:
        statements.append(statement)
      current = []
      i += 1
      continue
    current.append(ch)
    i += 1
  tail = "".join(current).strip()
  if tail:
    statements.append(tail)
  return statements


def _adapt_sql(sql: str, dialect: str) -> Optional[str]:
  if not sql:
    return sql
  out = _rewrite_invoice_sql(sql)
  dialect = (dialect or "").lower()
  if not dialect or dialect.startswith("sqlite"):
    return out
  if re.match(r"(?is)^\s*pragma\b", out):
    return None
  out = _rewrite_begin_immediate(out)
  out = _rewrite_autoincrement(out, dialect)
  out = _rewrite_insert_or_ignore(out, dialect)
  out = _rewrite_insert_or_replace(out, dialect)
  out = _rewrite_collation(out, dialect)
  out = _convert_qmark_to_percent(out)
  out = _escape_percent_for_pyformat(out)
  return out


def row_to_dict(row) -> dict:
  if row is None:
    return {}
  # If already a dict, return as-is
  if isinstance(row, dict):
    return row
  try:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
      return dict(mapping)
  except Exception:
    mapping = None
  try:
    if hasattr(row, "keys"):
      return {k: row[k] for k in row.keys()}
  except Exception:
    mapping = None
  try:
    return dict(row)
  except Exception:
    return {}


def row_get(row, key: str, index: Optional[int] = None, default=None):
  if row is None:
    return default
  if isinstance(row, dict):
    return row.get(key, default)
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
      return row[index]
    except Exception:
      return default
  return default


def safe_json_parse(data, default=None):
  """Safely parse JSON data that may already be parsed by PostgreSQL driver.

  PostgreSQL's psycopg2 driver automatically parses JSON/JSONB columns into
  Python dicts/lists, while SQLite returns JSON as strings. This helper
  handles both cases consistently.
  """
  if data is None:
    return default
  if isinstance(data, (dict, list)):
    return data
  if isinstance(data, str):
    try:
      return json.loads(data)
    except Exception:
      return default
  return default


def _normalize_params(params):
  if params is None:
    return None
  if isinstance(params, dict):
    return params
  if isinstance(params, tuple):
    return params
  if isinstance(params, list):
    return tuple(params)
  return (params,)


def _normalize_many_params(seq):
  if seq is None:
    return None
  if isinstance(seq, (list, tuple)):
    out = []
    for item in seq:
      if isinstance(item, dict):
        out.append(item)
      elif isinstance(item, tuple):
        out.append(item)
      elif isinstance(item, list):
        out.append(tuple(item))
      else:
        out.append((item,))
    return out
  return seq


def _execute_insert_returning_id(conn, sql: str, params, id_column: str = "id"):
  if _is_postgres(conn):
    statement = sql.rstrip().rstrip(";")
    statement = f"{statement} RETURNING {id_column}"
    row = conn.execute(statement, params).fetchone()
    if not row:
      return None
    try:
      mapping = getattr(row, "_mapping", None)
    except Exception:
      mapping = None
    if mapping and id_column in mapping:
      return mapping[id_column]
    try:
      if hasattr(row, "keys") and id_column in row.keys():
        return row[id_column]
    except Exception:
      mapping = None
    try:
      return row[0]
    except Exception:
      return None
  result = conn.execute(sql, params)
  return getattr(result, "lastrowid", None)


def get_db():
  """ Link (SQLAlchemy Engine )"""
  if has_request_context():
    try:
      raw = getattr(g, "_invoice_db_raw", None)
      wrapped = getattr(g, "_invoice_db_wrapped", None)
      if raw is not None and wrapped is not None:
        return wrapped
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_core.get_db.read_g_cache",
        log_key="billing_invoices.db_core.get_db.read_g_cache",
        log_window_seconds=300,
      )

  engine = _get_engine()
  conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
  base = _PrefixedConnection(conn, dialect_name=engine.dialect.name)

  if _is_sqlite(base):
    for stmt in [
      "PRAGMA journal_mode=WAL;",
      "PRAGMA synchronous=NORMAL;",
      "PRAGMA wal_autocheckpoint=1000;",
      "PRAGMA busy_timeout=30000;",
      "PRAGMA foreign_keys=ON;",
    ]:
      try:
        base.execute(stmt)
      except DB_ERRORS:
        pass

  if not has_request_context():
    return base

  out = _NoCloseConnection(base)
  try:
    g._invoice_db_raw = conn
    g._invoice_db_wrapped = out
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_core.get_db.write_g_cache",
      log_key="billing_invoices.db_core.get_db.write_g_cache",
      log_window_seconds=300,
    )
  return out


_INVOICE_TABLES = (
  "business_profile",
  "tax_invoice_profiles",
  "tax_invoice_drafts",
  "invoice_number_counters",
  "clients",
  "invoices",
  "invoice_revisions",
  "line_items",
  "invoice_templates",
  "template_items",
  "audit_log",
  "client_deposit_ledger",
  "client_merge_log",
  "invoice_attachments",
  "client_attachments",
  "bank_import_jobs",
  "bank_transactions",
  "fx_rates_cache",
  "invoice_case_map",
  "invoice_payments",
  "invoice_integrations",
  "accounts",
  "expense_categories",
  "expenses",
  "journal_entries",
  "journal_lines",
)

_UNIFIED_CLIENTS_WARNED = False


def _invoice_table_prefix() -> str:
  prefix = (current_app.config.get("INVOICEAPP_TABLE_PREFIX") or "").strip()
  if prefix and not _is_safe_identifier(prefix):
    try:
      current_app.logger.warning("Unsafe INVOICEAPP_TABLE_PREFIX ignored.")
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_core._invoice_table_prefix.log_warning",
        log_key="billing_invoices.db_core._invoice_table_prefix.log_warning",
        log_window_seconds=300,
      )
    return ""
  return prefix


def _check_unified_clients_schema() -> None:
  """
  Check if clients table has the required columns for unified clients integration.
  Logs a warning if columns are missing.
  """
  try:
    conn = get_db()
    # _get_column_names will eventually call unified_clients_enabled(),
    # but since _UNIFIED_CLIENTS_WARNED is already True, it won't recurse infinitely.
    cols = _get_column_names(conn, "clients")
    required = {"ipm_party_id", "ipm_client_id"}
    missing = required - cols
    if missing:
      current_app.logger.warning(
        f"INVOICEAPP_UNIFIED_CLIENTS is enabled but 'clients' table is missing columns: {missing}. "
        "Ensure CRM clients schema matches billing requirements."
      )
  except Exception as e:
    # Validation shouldn't crash the app
    current_app.logger.debug(f"Failed to check unified clients schema: {e}")


def _warn_unified_clients_enabled() -> None:
  global _UNIFIED_CLIENTS_WARNED
  if _UNIFIED_CLIENTS_WARNED:
    return
  # Set flag immediately to prevent recursion during DB checks
  _UNIFIED_CLIENTS_WARNED = True
  try:
    if current_app.config.get("INVOICEAPP_UNIFIED_CLIENTS"):
      _check_unified_clients_schema()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.db_core._warn_unified_clients_enabled",
      log_key="billing_invoices.db_core._warn_unified_clients_enabled",
      log_window_seconds=300,
    )


def unified_clients_enabled() -> bool:
  enabled = bool(current_app.config.get("INVOICEAPP_UNIFIED_CLIENTS"))
  if enabled:
    _warn_unified_clients_enabled()
  return enabled


def _actual_table_name(table: str) -> str:
  prefix = _invoice_table_prefix()
  if current_app.config.get("INVOICEAPP_INTEGRATED") and prefix and table in _INVOICE_TABLES:
    if unified_clients_enabled() and table == "clients":
      return table
    actual = f"{prefix}{table}"
    return actual if _is_safe_identifier(actual) else table
  return table


def _rewrite_invoice_sql(sql: str) -> str:
  if not sql:
    return sql
  prefix = _invoice_table_prefix()
  if not prefix:
    return sql

  # Only rewrite in integrated mode
  if not current_app.config.get("INVOICEAPP_INTEGRATED"):
    return sql

  out = sql
  unified_clients = unified_clients_enabled()
  for t in _INVOICE_TABLES:
    if unified_clients and t == "clients":
      continue
    pref = f"{prefix}{t}"
    escaped = re.escape(t)
    # FROM / JOIN (preserve existing qualifiers like `invoices.id` by aliasing)
    # e.g. `FROM invoices` -> `FROM billing_invoices AS invoices`
    # If an alias already exists, keep it: `FROM invoices i` -> `FROM billing_invoices i`
    out = re.sub(
      rf"(?i)\b(from|join)\s+{escaped}\b(\s+(?:as\s+)?(?!on\b|where\b|left\b|right\b|inner\b|outer\b|join\b|group\b|order\b|limit\b|having\b|union\b|cross\b)[A-Za-z_][A-Za-z0-9_]*)?",
      lambda m, _pref=pref, _t=t: (
        f"{m.group(1)} {_pref}{m.group(2)}"
        if m.group(2)
        else f"{m.group(1)} {_pref} AS {_t}"
      ),
      out,
    )

    # UPDATE / INSERT INTO
    out = re.sub(
      rf"(?i)\b(update|into)\s+{escaped}\b",
      lambda m, _pref=pref: f"{m.group(1)} {_pref}",
      out,
    )

    # DELETE FROM
    out = re.sub(
      rf"(?i)\b(delete\s+from)\s+{escaped}\b",
      lambda m, _pref=pref: f"{m.group(1)} {_pref}",
      out,
    )
    # ALTER TABLE
    out = re.sub(
      rf"(?i)\b(alter\s+table)\s+{escaped}\b",
      lambda m, _pref=pref: f"{m.group(1)} {_pref}",
      out,
    )
    # CREATE TABLE
    out = re.sub(
      rf"(?i)\b(create\s+table\s+(?:if\s+not\s+exists\s+)?)({escaped})(\b)",
      lambda m, _pref=pref: f"{m.group(1)}{_pref}{m.group(3)}",
      out,
    )
    # DROP TABLE
    out = re.sub(
      rf"(?i)\b(drop\s+table\s+(?:if\s+exists\s+)?)({escaped})(\b)",
      lambda m, _pref=pref: f"{m.group(1)}{_pref}{m.group(3)}",
      out,
    )
    # RENAME TO
    out = re.sub(
      rf"(?i)(\brename\s+to\s+)({escaped})(\b)",
      lambda m, _pref=pref: f"{m.group(1)}{_pref}{m.group(3)}",
      out,
    )
    # REFERENCES
    out = re.sub(
      rf"(?i)(\breferences\s+)({escaped})(\b)",
      lambda m, _pref=pref: f"{m.group(1)}{_pref}{m.group(3)}",
      out,
    )
    # CREATE INDEX ... ON <table>(
    out = re.sub(
      rf"(?i)(\bon\s+)({escaped})(\s*\()",
      lambda m, _pref=pref: f"{m.group(1)}{_pref}{m.group(3)}",
      out,
    )
  return out


def _table_exists(conn, table_name: str) -> bool:
  actual = _actual_table_name(table_name)
  if _is_sqlite(conn):
    row = conn.execute(
      "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
      (actual,),
    ).fetchone()
    return row is not None
  sa_conn = _get_sa_connection(conn)
  try:
    inspector = inspect(sa_conn)
    return bool(inspector.has_table(actual))
  except Exception:
    try:
      row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name=?",
        (actual,),
      ).fetchone()
      return row is not None
    except Exception:
      return False


def _get_column_names(conn, table_name: str) -> set[str]:
  """
  Return set of column names for a given logical table (auto-applies invoice prefix rules).
  Works for both SQLite and PostgreSQL.
  """
  actual = _actual_table_name(table_name)
  cols: set[str] = set()
  if not actual:
    return cols

  # SQLite
  if _is_sqlite(conn):
    if not _is_safe_identifier(actual):
      return cols
    rows = []
    try:
      # Use PRAGMA only when sqlite; _adapt_sql drops PRAGMA for non-sqlite.
      sql = f"PRAGMA table_info({_quote_ident(actual)})"
      rows = conn.execute(sql).fetchall()
    except Exception:
      rows = []
    for r in rows or []:
      name = row_get(r, "name", 1)
      if name:
        cols.add(str(name))
    return cols

  # PostgreSQL / other SQLAlchemy-backed DBs
  sa_conn = _get_sa_connection(conn)
  try:
    inspector = inspect(sa_conn)
    for c in inspector.get_columns(actual):
      n = (c or {}).get("name")
      if n:
        cols.add(str(n))
    if cols:
      return cols
  except Exception:
    inspector = None

  # Fallback: information_schema
  try:
    rows = conn.execute(
      "SELECT column_name FROM information_schema.columns WHERE table_name=?",
      (actual,),
    ).fetchall()
  except Exception:
    rows = []
  for r in rows or []:
    name = row_get(r, "column_name", 0)
    if name:
      cols.add(str(name))
  return cols


def _ensure_column(conn, table: str, col: str, col_type: str) -> bool:
  """
  Best-effort add-column for existing invoice tables.
  Returns True if a column was added, False otherwise.
  """
  try:
    # Check table existence first
    if not _table_exists(conn, table):
      return False

    actual = _actual_table_name(table)
    cols = _get_column_names(conn, table)
    if col in cols:
      return False

    quoted_table = _quote_ident(actual)
    quoted_col = _quote_ident(col)

    # Postgres: Use IF NOT EXISTS (native idempotency)
    if _is_postgres(conn):
      sql = f"ALTER TABLE {quoted_table} ADD COLUMN IF NOT EXISTS {quoted_col} {col_type}"
      try:
        conn.execute(sql)
        return True
      except Exception as exc:
        # Fallback if IF NOT EXISTS isn't supported (very old PG) or other error
        if "already exists" in str(exc):
          return False
        raise exc

    if not (_is_safe_identifier(actual) and _is_safe_identifier(col)):
      return False

    sql = f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_col} {col_type}"
    conn.execute(sql)
    return True
  except Exception:
    # Swallow errors (best-effort migration)
    return False


def _create_index_if_possible(conn, index_name: str, table: str, column: str) -> None:
  """
  Create an index only if the target table+column exist.
  Safe to run repeatedly (best-effort).
  """
  try:
    if not _table_exists(conn, table):
      return
    cols = _get_column_names(conn, table)
    if column not in cols:
      return
    actual = _actual_table_name(table)
    if not (
      _is_safe_identifier(actual)
      and _is_safe_identifier(column)
      and _is_safe_identifier(index_name)
    ):
      return

    # Try IF NOT EXISTS first (SQLite + modern Postgres)
    try:
      sql = (
        f"CREATE INDEX IF NOT EXISTS {_quote_ident(index_name)} "
        f"ON {_quote_ident(actual)}({_quote_ident(column)})"
      )
      conn.execute(sql)
      return
    except Exception as exc:
      # Expected on older Postgres that doesn't support IF NOT EXISTS.
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_core._create_index_if_possible.if_not_exists",
        log_key="billing_invoices.db_core._create_index_if_possible.if_not_exists",
        log_window_seconds=300,
      )

    # Fallback for older Postgres: check pg_indexes then create
    try:
      if _is_postgres(conn):
        exists = conn.execute(
          "SELECT 1 FROM pg_indexes WHERE indexname=?",
          (index_name,),
        ).fetchone()
        if exists:
          return
      sql = (
        f"CREATE INDEX {_quote_ident(index_name)} "
        f"ON {_quote_ident(actual)}({_quote_ident(column)})"
      )
      conn.execute(sql)
    except Exception:
      return
  except Exception:
    return


def migrate_db() -> None:
  """
  Invoice module schema migration (idempotent, best-effort).

  Purpose:
   - Support older DBs created before ipm integration columns existed.
   - Add indexes for ipm integration columns (see init_db comment).

  NOTE: This function is intentionally conservative: it only adds columns/indexes
  if missing and never drops/renames anything.
  """
  conn = get_db()
  try:
    # --- Ensure ipm integration columns exist (older DB compatibility) ---
    # clients
    _ensure_column(conn, "clients", "ipm_party_id", "TEXT")
    _ensure_column(conn, "clients", "ipm_client_id", "INTEGER")

    # invoices
    _ensure_column(conn, "invoices", "internal_reference", "TEXT")
    _ensure_column(conn, "invoices", "ipm_case_id", "TEXT")
    _ensure_column(conn, "invoices", "ipm_case_ref", "TEXT")
    _ensure_column(conn, "invoices", "ipm_invoice_id", "TEXT")

    # --- Indexes for ipm integration columns ---
    _create_index_if_possible(conn, "idx_clients_ipm_party_id", "clients", "ipm_party_id")
    _create_index_if_possible(conn, "idx_clients_ipm_client_id", "clients", "ipm_client_id")

    _create_index_if_possible(conn, "idx_invoices_ipm_case_id", "invoices", "ipm_case_id")
    _create_index_if_possible(conn, "idx_invoices_ipm_case_ref", "invoices", "ipm_case_ref")
    _create_index_if_possible(
      conn, "idx_invoices_ipm_invoice_id", "invoices", "ipm_invoice_id"
    )
  finally:
    try:
      conn.close()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.db_core.migrate_db.close",
        log_key="billing_invoices.db_core.migrate_db.close",
        log_window_seconds=300,
      )


class _EmptyResult:
  def fetchone(self):
    return None

  def fetchall(self):
    return []

  def scalar(self):
    return None

  @property
  def lastrowid(self):
    return None

  @property
  def rowcount(self):
    return -1

  def __iter__(self):
    return iter(())


class _CompatRow:
  def __init__(self, row: Any):
    self._row = row
    self._cached_mapping = None
    self._mapping_cached = False

  def _get_mapping(self):
    if self._mapping_cached:
      return self._cached_mapping
    self._mapping_cached = True

    try:
      mapping = getattr(self._row, "_mapping", None)
    except Exception:
      mapping = None
    if mapping is not None:
      self._cached_mapping = mapping
      return self._cached_mapping

    keys_attr = getattr(self._row, "keys", None)
    if callable(keys_attr):
      try:
        self._cached_mapping = {k: self._row[k] for k in self._row.keys()}
        return self._cached_mapping
      except Exception:
        self._cached_mapping = None

    self._cached_mapping = None
    return None

  @property
  def _mapping(self):
    return self._get_mapping()

  def keys(self):
    m = self._get_mapping()
    if m is not None:
      return m.keys()
    try:
      keys_attr = getattr(self._row, "keys", None)
      if callable(keys_attr):
        return self._row.keys()
    except Exception:
      return []
    return []

  def get(self, key, default=None):
    try:
      return self[key]
    except Exception:
      return default

  def __contains__(self, item):
    m = self._get_mapping()
    if m is not None:
      try:
        return item in m
      except Exception:
        return False
    try:
      return item in self.keys()
    except Exception:
      return False

  def __getitem__(self, key):
    if isinstance(key, str):
      m = self._get_mapping()
      if m is not None:
        return m[key]
    return self._row[key]

  def __iter__(self):
    return iter(self._row)

  def __len__(self):
    return len(self._row)

  def __getattr__(self, item):
    return getattr(self._row, item)


def _should_wrap_row(row: Any) -> bool:
  if row is None:
    return False
  if isinstance(row, _CompatRow):
    return False
  if isinstance(row, dict):
    return False
  try:
    mapping = getattr(row, "_mapping", None)
  except Exception:
    mapping = None
  if mapping is not None:
    return True
  try:
    return callable(getattr(row, "keys", None))
  except Exception:
    return False


def _coerce_row(row: Any):
  if _should_wrap_row(row):
    return _CompatRow(row)
  return row


class _SAResult:
  def __init__(self, result, lastrowid=None):
    self._result = result
    self.lastrowid = lastrowid if lastrowid is not None else getattr(result, "lastrowid", None)

  def fetchone(self):
    if self._result is None:
      return None
    return _coerce_row(self._result.fetchone())

  def fetchall(self):
    if self._result is None:
      return []
    return [_coerce_row(r) for r in self._result.fetchall()]

  def scalar(self):
    if self._result is None:
      return None
    return self._result.scalar()

  @property
  def rowcount(self):
    return getattr(self._result, "rowcount", -1)

  def __iter__(self):
    if self._result is None:
      return iter(())
    return (_coerce_row(r) for r in self._result)


class _PrefixedCursor:
  def __init__(self, conn):
    self._conn = conn
    self._result = None

  def execute(self, sql, parameters=()):
    self._result = self._conn._execute(sql, parameters)
    return self

  def executemany(self, sql, seq_of_parameters):
    self._result = self._conn._executemany(sql, seq_of_parameters)
    return self

  def executescript(self, sql_script):
    self._result = self._conn._executescript(sql_script)
    return self

  def fetchone(self):
    if not self._result:
      return None
    return self._result.fetchone()

  def fetchall(self):
    if not self._result:
      return []
    return self._result.fetchall()

  def scalar(self):
    if not self._result:
      return None
    return self._result.scalar()

  @property
  def lastrowid(self):
    return getattr(self._result, "lastrowid", None)

  @property
  def rowcount(self):
    if not self._result:
      return -1
    return getattr(self._result, "rowcount", -1)

  def close(self):
    return None


class _PrefixedConnection:
  def __init__(self, conn, dialect_name: Optional[str] = None):
    self._conn = conn
    self.dialect_name = (dialect_name or _get_dialect_name(conn)).lower()
    self._in_transaction = False

  @property
  def sa_connection(self):
    return self._conn

  @property
  def in_transaction(self):
    return bool(self._in_transaction)

  def _execute(self, sql, parameters=()):
    if sql is None:
      return _EmptyResult()
    raw_sql = sql if isinstance(sql, str) else str(sql)
    adapted = _adapt_sql(raw_sql, self.dialect_name)
    if adapted is None:
      return _EmptyResult()
    normalized = adapted.strip()
    normalized_upper = normalized.upper()
    if normalized_upper.startswith("BEGIN"):
      self._in_transaction = True
      self._conn.exec_driver_sql(normalized)
      return _EmptyResult()
    if normalized_upper.startswith("COMMIT"):
      if self._in_transaction:
        self._conn.exec_driver_sql("COMMIT")
      self._in_transaction = False
      return _EmptyResult()
    if normalized_upper.startswith("ROLLBACK"):
      if self._in_transaction:
        self._conn.exec_driver_sql("ROLLBACK")
      self._in_transaction = False
      return _EmptyResult()
    params = _normalize_params(parameters)
    if params is None or params == ():
      result = self._conn.exec_driver_sql(adapted)
    else:
      result = self._conn.exec_driver_sql(adapted, params)
    lastrowid = None
    if self.dialect_name.startswith("postgres") and normalized_upper.startswith("INSERT"):
      if " RETURNING " not in normalized_upper:
        table_name = _parse_insert_table(adapted)
        if table_name:
          lastrowid = _postgres_sync_insert_id(self._conn, table_name)
    return _SAResult(result, lastrowid=lastrowid)

  def _executemany(self, sql, seq_of_parameters):
    if sql is None:
      return _EmptyResult()
    raw_sql = sql if isinstance(sql, str) else str(sql)
    adapted = _adapt_sql(raw_sql, self.dialect_name)
    if adapted is None:
      return _EmptyResult()
    normalized = _normalize_many_params(seq_of_parameters)
    if not normalized:
      return _EmptyResult()
    result = self._conn.exec_driver_sql(adapted, normalized)
    return _SAResult(result)

  def _executescript(self, sql_script):
    if not sql_script:
      return _EmptyResult()
    result = _EmptyResult()
    for stmt in _split_sql_script(sql_script):
      result = self._execute(stmt)
    return result

  def cursor(self, *args, **kwargs):
    return _PrefixedCursor(self)

  def execute(self, sql, parameters=()):
    return self._execute(sql, parameters)

  def executemany(self, sql, seq_of_parameters):
    return self._executemany(sql, seq_of_parameters)

  def executescript(self, sql_script):
    return self._executescript(sql_script)

  def commit(self):
    if not self._in_transaction:
      return None
    try:
      self._conn.exec_driver_sql("COMMIT")
    finally:
      self._in_transaction = False
    return None

  def rollback(self):
    if not self._in_transaction:
      return None
    try:
      self._conn.exec_driver_sql("ROLLBACK")
    finally:
      self._in_transaction = False
    return None

  def close(self):
    try:
      return self._conn.close()
    finally:
      self._in_transaction = False

  def __getattr__(self, item):
    return getattr(self._conn, item)


def _quote_ident(name: str) -> str:
  s = str(name)
  return '"' + s.replace('"', '""') + '"'
