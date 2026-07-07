from __future__ import annotations

from datetime import date, datetime

from .db import row_to_dict


def safe_int(value, default=None):
  try:
    if value is None:
      return default
    return int(str(value).strip())
  except Exception:
    return default


def safe_float(value, default=0.0):
  try:
    if value is None:
      return default
    text = str(value).strip().replace(",", "")
    if not text:
      return default
    return float(text)
  except Exception:
    return default


def parse_date(value, default=None):
  if not value:
    return default
  try:
    return date.fromisoformat(str(value).strip()).isoformat()
  except Exception:
    return default


def now_iso():
  try:
    return datetime.now().isoformat(timespec="seconds")
  except Exception:
    return datetime.now().isoformat()


def split_signed_amount(amount: float):
  value = float(amount or 0)
  if value >= 0:
    return value, 0.0
  return 0.0, abs(value)


def journal_status(entry) -> str:
  row = row_to_dict(entry)
  if int(row.get("reversed") or 0) == 1:
    return "reversed"
  if int(row.get("posted") or 0) == 1:
    return "posted"
  if int(row.get("approved") or 0) == 1:
    return "approved"
  return "draft"


def journal_status_label(entry) -> str:
  return {
    "draft": "Draft",
    "approved": "",
    "posted": "",
    "reversed": "items",
  }.get(journal_status(entry), "Draft")


def get_closed_period(conn, entry_date: str, business_profile_id: int):
  if not entry_date or not business_profile_id:
    return None
  return conn.execute(
    """
    SELECT *
     FROM accounting_periods
     WHERE business_profile_id = ?
      AND status = 'closed'
      AND start_date <= ?
      AND end_date >= ?
     ORDER BY end_date DESC, id DESC
     LIMIT 1
    """,
    (int(business_profile_id), entry_date, entry_date),
  ).fetchone()


def list_accounting_periods(conn, business_profile_id=None):
  if business_profile_id:
    rows = conn.execute(
      """
      SELECT *
       FROM accounting_periods
       WHERE business_profile_id = ?
       ORDER BY start_date DESC, id DESC
      """,
      (int(business_profile_id),),
    ).fetchall()
  else:
    rows = conn.execute(
      """
      SELECT *
       FROM accounting_periods
       ORDER BY start_date DESC, id DESC
      """
    ).fetchall()
  return [row_to_dict(r) for r in rows]


def close_accounting_period(
  conn,
  business_profile_id: int,
  start_date: str,
  end_date: str,
  user_id=None,
  notes: str = "",
  period_type: str = "custom",
):
  start_date = parse_date(start_date)
  end_date = parse_date(end_date)
  if not start_date or not end_date:
    raise ValueError("deadline Period confirm.")
  if end_date < start_date:
    start_date, end_date = end_date, start_date
  bp_id = safe_int(business_profile_id)
  if not bp_id:
    raise ValueError("Business profile select.")

  overlap = conn.execute(
    """
    SELECT id, start_date, end_date
     FROM accounting_periods
     WHERE business_profile_id = ?
      AND status = 'closed'
      AND start_date <= ?
      AND end_date >= ?
     ORDER BY start_date ASC
     LIMIT 1
    """,
    (bp_id, end_date, start_date),
  ).fetchone()
  if overlap:
    row = row_to_dict(overlap)
    raise ValueError(
      f"Accounting period already closed. ({row.get('start_date')} ~ {row.get('end_date')})"
    )

  pending = conn.execute(
    """
    SELECT COUNT(*)
     FROM journal_entries
     WHERE business_profile_id = ?
      AND entry_date >= ?
      AND entry_date <= ?
      AND COALESCE(posted, 0) = 0
      AND COALESCE(reversed, 0) = 0
    """,
    (bp_id, start_date, end_date),
  ).fetchone()
  if pending and int(pending[0] or 0) > 0:
    raise ValueError("Cannot close period while unposted journal entries exist.")

  existing = conn.execute(
    """
    SELECT id
     FROM accounting_periods
     WHERE business_profile_id = ?
      AND start_date = ?
      AND end_date = ?
     LIMIT 1
    """,
    (bp_id, start_date, end_date),
  ).fetchone()

  closed_at = now_iso()
  if existing:
    period_id = int(existing[0])
    conn.execute(
      """
      UPDATE accounting_periods
        SET period_type = ?,
          status = 'closed',
          notes = ?,
          closed_at = ?,
          closed_by = ?,
          reopened_at = NULL,
          reopened_by = NULL
       WHERE id = ?
      """,
      (period_type, notes or None, closed_at, user_id, period_id),
    )
  else:
    cur = conn.execute(
      """
      INSERT INTO accounting_periods (
        business_profile_id, period_type, start_date, end_date,
        status, notes, closed_at, closed_by
      ) VALUES (?, ?, ?, ?, 'closed', ?, ?, ?)
      """,
      (bp_id, period_type, start_date, end_date, notes or None, closed_at, user_id),
    )
    period_id = int(getattr(cur, "lastrowid", 0) or 0)

  conn.execute(
    """
    UPDATE journal_entries
      SET locked_period = 1,
        locked_period_id = ?
     WHERE business_profile_id = ?
      AND entry_date >= ?
      AND entry_date <= ?
    """,
    (period_id, bp_id, start_date, end_date),
  )
  return period_id


def reopen_accounting_period(conn, period_id: int, user_id=None):
  row = conn.execute(
    "SELECT * FROM accounting_periods WHERE id = ? LIMIT 1",
    (int(period_id),),
  ).fetchone()
  if not row:
    raise ValueError("timesPeriod not found.")
  period = row_to_dict(row)
  conn.execute(
    """
    UPDATE accounting_periods
      SET status = 'open',
        reopened_at = ?,
        reopened_by = ?
     WHERE id = ?
    """,
    (now_iso(), user_id, int(period_id)),
  )
  conn.execute(
    """
    UPDATE journal_entries
      SET locked_period = 0,
        locked_period_id = NULL
     WHERE locked_period_id = ?
    """,
    (int(period_id),),
  )
  return period


def _journal_scope_sql(business_profile_id=None, currency=None, entry_alias="je", line_alias="jl"):
  clauses = [f"COALESCE({entry_alias}.posted, 0) = 1"]
  params = []
  bp_id = safe_int(business_profile_id)
  if bp_id:
    clauses.append(f"{entry_alias}.business_profile_id = ?")
    params.append(bp_id)
  if currency and str(currency).upper() != "ALL":
    clauses.append(f"COALESCE({line_alias}.currency, 'USD') = ?")
    params.append(str(currency).upper())
  return " AND ".join(clauses), params


def fetch_trial_balance_rows(
  conn,
  start_date: str,
  end_date: str,
  business_profile_id=None,
  currency=None,
  include_zero=True,
):
  start_date = parse_date(start_date)
  end_date = parse_date(end_date)
  if not start_date or not end_date:
    raise ValueError("times Period confirm.")
  if end_date < start_date:
    start_date, end_date = end_date, start_date

  scope_sql, scope_params = _journal_scope_sql(
    business_profile_id=business_profile_id,
    currency=currency,
  )
  opening_where = f"{scope_sql} AND je.entry_date < ?"
  period_where = f"{scope_sql} AND je.entry_date >= ? AND je.entry_date <= ?"
  params = list(scope_params) + [start_date] + list(scope_params) + [start_date, end_date]

  rows = conn.execute(
    f"""
    SELECT a.id, a.code, a.name, a.type, a.is_active,
        COALESCE(o.opening_balance, 0) AS opening_balance,
        COALESCE(p.period_debit, 0) AS period_debit,
        COALESCE(p.period_credit, 0) AS period_credit
     FROM accounts a
   LEFT JOIN (
        SELECT jl.account_id,
            COALESCE(SUM(jl.debit - jl.credit), 0) AS opening_balance
         FROM journal_lines jl
         JOIN journal_entries je ON je.id = jl.entry_id
         WHERE {opening_where}
         GROUP BY jl.account_id
      ) o ON o.account_id = a.id
   LEFT JOIN (
        SELECT jl.account_id,
            COALESCE(SUM(jl.debit), 0) AS period_debit,
            COALESCE(SUM(jl.credit), 0) AS period_credit
         FROM journal_lines jl
         JOIN journal_entries je ON je.id = jl.entry_id
         WHERE {period_where}
         GROUP BY jl.account_id
      ) p ON p.account_id = a.id
     ORDER BY a.type, a.code
    """,
    params,
  ).fetchall()

  out = []
  for row in rows:
    item = row_to_dict(row)
    opening_balance = float(item.get("opening_balance") or 0)
    period_debit = float(item.get("period_debit") or 0)
    period_credit = float(item.get("period_credit") or 0)
    ending_balance = opening_balance + period_debit - period_credit
    if (
      not include_zero
      and abs(opening_balance) < 0.0001
      and abs(period_debit) < 0.0001
      and abs(period_credit) < 0.0001
      and abs(ending_balance) < 0.0001
    ):
      continue
    opening_debit, opening_credit = split_signed_amount(opening_balance)
    ending_debit, ending_credit = split_signed_amount(ending_balance)
    item.update(
      {
        "opening_balance": opening_balance,
        "opening_debit": opening_debit,
        "opening_credit": opening_credit,
        "period_debit": period_debit,
        "period_credit": period_credit,
        "ending_balance": ending_balance,
        "ending_debit": ending_debit,
        "ending_credit": ending_credit,
      }
    )
    out.append(item)
  return out


def build_income_statement(rows):
  revenues = []
  expenses = []
  total_revenue = 0.0
  total_expense = 0.0

  for row in rows:
    account_type = (row.get("type") or "").lower()
    if account_type == "revenue":
      amount = float(row.get("period_credit") or 0) - float(row.get("period_debit") or 0)
      if abs(amount) < 0.0001:
        continue
      data = dict(row)
      data["amount"] = amount
      revenues.append(data)
      total_revenue += amount
    elif account_type == "expense":
      amount = float(row.get("period_debit") or 0) - float(row.get("period_credit") or 0)
      if abs(amount) < 0.0001:
        continue
      data = dict(row)
      data["amount"] = amount
      expenses.append(data)
      total_expense += amount

  return {
    "revenues": revenues,
    "expenses": expenses,
    "total_revenue": total_revenue,
    "total_expense": total_expense,
    "net_income": total_revenue - total_expense,
  }


def build_balance_sheet(conn, as_of_date: str, business_profile_id=None, currency=None):
  as_of_date = parse_date(as_of_date)
  if not as_of_date:
    raise ValueError("reference date confirm.")

  scope_sql, scope_params = _journal_scope_sql(
    business_profile_id=business_profile_id,
    currency=currency,
  )
  params = list(scope_params) + [as_of_date]
  rows = conn.execute(
    f"""
    SELECT a.id, a.code, a.name, a.type, a.is_active,
        COALESCE(s.debit_total, 0) AS debit_total,
        COALESCE(s.credit_total, 0) AS credit_total
     FROM accounts a
   LEFT JOIN (
        SELECT jl.account_id,
            COALESCE(SUM(jl.debit), 0) AS debit_total,
            COALESCE(SUM(jl.credit), 0) AS credit_total
         FROM journal_lines jl
         JOIN journal_entries je ON je.id = jl.entry_id
         WHERE {scope_sql}
          AND je.entry_date <= ?
         GROUP BY jl.account_id
      ) s ON s.account_id = a.id
     ORDER BY a.type, a.code
    """,
    params,
  ).fetchall()

  assets = []
  liabilities = []
  equities = []
  current_income = 0.0

  for row in rows:
    item = row_to_dict(row)
    debit_total = float(item.get("debit_total") or 0)
    credit_total = float(item.get("credit_total") or 0)
    raw_balance = debit_total - credit_total
    account_type = (item.get("type") or "").lower()

    if account_type == "asset":
      amount = raw_balance
      if abs(amount) >= 0.0001:
        item["amount"] = amount
        assets.append(item)
    elif account_type == "liability":
      amount = -raw_balance
      if abs(amount) >= 0.0001:
        item["amount"] = amount
        liabilities.append(item)
    elif account_type == "equity":
      amount = -raw_balance
      if abs(amount) >= 0.0001:
        item["amount"] = amount
        equities.append(item)
    elif account_type == "revenue":
      current_income += credit_total - debit_total
    elif account_type == "expense":
      current_income -= debit_total - credit_total

  if abs(current_income) >= 0.0001:
    equities.append(
      {
        "id": None,
        "code": "CURRENT",
        "name": "",
        "type": "equity",
        "amount": current_income,
      }
    )

  total_assets = sum(float(row.get("amount") or 0) for row in assets)
  total_liabilities = sum(float(row.get("amount") or 0) for row in liabilities)
  total_equity = sum(float(row.get("amount") or 0) for row in equities)

  return {
    "assets": assets,
    "liabilities": liabilities,
    "equities": equities,
    "total_assets": total_assets,
    "total_liabilities": total_liabilities,
    "total_equity": total_equity,
    "total_liabilities_and_equity": total_liabilities + total_equity,
    "current_income": current_income,
  }
