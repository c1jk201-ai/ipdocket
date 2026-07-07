from __future__ import annotations

from calendar import monthrange
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.services.billing.utils import accounting_feature_disabled

from ..accounting import get_closed_period, journal_status, journal_status_label
from ..accounting import now_iso as _now_iso
from ..accounting import parse_date as _parse_date
from ..accounting import safe_float as _safe_float
from ..accounting import safe_int as _safe_int
from ..auth import get_current_user, log_audit
from ..db import get_all_business_profiles, get_business_profile, get_db, row_to_dict

bp = Blueprint("ledger", __name__)


@bp.before_request
def _block_if_disabled():
  if accounting_feature_disabled():
    abort(404)


def _ledger_endpoint(name: str) -> str:
  if (request.endpoint or "").startswith("business.accounting_"):
    return f"business.accounting_{name}"
  legacy_names = {"ledger_accounts": "accounts", "ledger_toggle_account": "toggle_account"}
  return f"billing_invoices.ledger.{legacy_names.get(name, name)}"


def _default_month_range():
  today = date.today()
  start = today.replace(day=1)
  last = monthrange(today.year, today.month)[1]
  end = today.replace(day=last)
  return start.isoformat(), end.isoformat()


def _entry_sums(cur, entry_id: int):
  row = cur.execute(
    """
    SELECT COUNT(*) AS line_count,
        COALESCE(SUM(debit), 0) AS debit_total,
        COALESCE(SUM(credit), 0) AS credit_total
     FROM journal_lines
     WHERE entry_id = ?
    """,
    (entry_id,),
  ).fetchone()
  data = row_to_dict(row)
  return {
    "line_count": int(data.get("line_count") or 0),
    "debit_total": float(data.get("debit_total") or 0),
    "credit_total": float(data.get("credit_total") or 0),
  }


def _entry_period_lock(conn, entry):
  row = row_to_dict(entry)
  bp_id = _safe_int(row.get("business_profile_id"))
  entry_date = row.get("entry_date")
  return get_closed_period(conn, entry_date, bp_id)


def _decorate_entry(entry):
  item = row_to_dict(entry)
  item["status_code"] = journal_status(item)
  item["status_label"] = journal_status_label(item)
  return item


@bp.route("")
def general_ledger():
  start_date = _parse_date(request.args.get("start_date"))
  end_date = _parse_date(request.args.get("end_date"))
  if not start_date or not end_date:
    start_date, end_date = _default_month_range()

  account_id = _safe_int(request.args.get("account_id"))
  bp_filter = _safe_int(request.args.get("business_profile_id"))

  conn = get_db()
  cur = conn.cursor()

  # Best-effort: pick a sensible display currency when filtering by a business profile.
  # Without a bp filter, ledger can mix currencies, so we keep the legacy display default (USD).
  ledger_currency = "USD"
  if bp_filter:
    try:
      row = cur.execute(
        "SELECT currency FROM business_profile WHERE id=?",
        (int(bp_filter),),
      ).fetchone()
      if row:
        ledger_currency = (row_to_dict(row).get("currency") or ledger_currency).upper()
    except Exception:
      ledger_currency = "USD"

  accounts = cur.execute(
    "SELECT id, code, name, type, is_active FROM accounts ORDER BY type, code"
  ).fetchall()

  account = None
  ledger_rows = []
  opening_balance = 0.0

  if account_id:
    row = cur.execute(
      "SELECT id, code, name, type, is_active FROM accounts WHERE id=?",
      (account_id,),
    ).fetchone()
    if not row:
      conn.close()
      abort(404)
    account = row_to_dict(row)

    params = [account_id]
    where = ["jl.account_id = ?"]
    if bp_filter:
      where.append("je.business_profile_id = ?")
      params.append(int(bp_filter))
    where.append("COALESCE(jl.currency, 'USD') = ?")
    params.append(str(ledger_currency))

    where_sql = " AND ".join(where)

    open_row = cur.execute(
      f"""
      SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
      FROM journal_lines jl
      JOIN journal_entries je ON je.id = jl.entry_id
      WHERE {where_sql} AND COALESCE(je.posted, 0) = 1 AND je.entry_date < ?
      """,
      params + [start_date],
    ).fetchone()
    opening_balance = open_row[0] if open_row else 0.0

    rows = cur.execute(
      f"""
      SELECT je.id AS entry_id, je.entry_date, je.memo, jl.debit, jl.credit, jl.currency, jl.description
      FROM journal_lines jl
      JOIN journal_entries je ON je.id = jl.entry_id
      WHERE {where_sql}
       AND COALESCE(je.posted, 0) = 1
       AND je.entry_date >= ?
       AND je.entry_date <= ?
      ORDER BY je.entry_date ASC, jl.id ASC
      """,
      params + [start_date, end_date],
    ).fetchall()

    running = opening_balance
    for r in rows:
      debit = float(r[3] or 0)
      credit = float(r[4] or 0)
      running += debit - credit
      ledger_rows.append(
        {
          "entry_id": r[0],
          "entry_date": r[1],
          "memo": r[2],
          "debit": debit,
          "credit": credit,
          "currency": r[5] or "USD",
          "description": r[6],
          "balance": running,
        }
      )

  else:
    # Account summary for the period. Keep all accounts visible even if they have no entries
    # (business_profile filter should not turn the LEFT JOIN into an inner join).
    params = [start_date, end_date]
    where = ["je.entry_date >= ?", "je.entry_date <= ?"]
    if bp_filter:
      where.append("je.business_profile_id = ?")
      params.append(int(bp_filter))
    where.append("COALESCE(jl.currency, 'USD') = ?")
    params.append(str(ledger_currency))
    where_sql = " AND ".join(where)

    rows = cur.execute(
      f"""
      SELECT a.id, a.code, a.name, a.type, a.is_active,
          COALESCE(s.debit, 0) AS debit,
          COALESCE(s.credit, 0) AS credit
       FROM accounts a
     LEFT JOIN (
        SELECT jl.account_id AS account_id,
            COALESCE(SUM(jl.debit), 0) AS debit,
            COALESCE(SUM(jl.credit), 0) AS credit
         FROM journal_lines jl
         JOIN journal_entries je ON je.id = jl.entry_id
         WHERE {where_sql}
          AND COALESCE(je.posted, 0) = 1
         GROUP BY jl.account_id
      ) s ON s.account_id = a.id
     ORDER BY a.type, a.code
      """,
      params,
    ).fetchall()
    ledger_rows = [row_to_dict(r) for r in rows]

  business_profiles = get_all_business_profiles()
  conn.close()

  return render_template(
    "billing_invoices/ledger_general.html",
    accounts=[row_to_dict(r) for r in accounts],
    account=account,
    rows=ledger_rows,
    opening_balance=opening_balance,
    start_date=start_date,
    end_date=end_date,
    business_profiles=business_profiles,
    bp_filter=bp_filter,
    business_profile_id=bp_filter,
    ledger_currency=ledger_currency,
  )


@bp.route("/accounts", methods=["GET", "POST"])
def accounts():
  conn = get_db()
  cur = conn.cursor()

  if request.method == "POST":
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    acc_type = (request.form.get("type") or "").strip().lower()
    is_active = 1 if request.form.get("is_active") in ("1", "on", "true") else 0

    if (
      not code
      or not name
      or acc_type not in ("asset", "liability", "equity", "revenue", "expense")
    ):
      flash("Account confirm.", "error")
      return redirect(url_for(_ledger_endpoint("ledger_accounts")))

    cur.execute(
      "INSERT INTO accounts (code, name, type, is_active) VALUES (?, ?, ?, ?)",
      (code, name, acc_type, is_active),
    )
    conn.commit()
    account_id = getattr(cur, "lastrowid", None)
    log_audit("account.create", "account", account_id)
    flash("Account Add.", "success")
    return redirect(url_for(_ledger_endpoint("ledger_accounts")))

  rows = cur.execute(
    "SELECT id, code, name, type, is_active FROM accounts ORDER BY type, code"
  ).fetchall()
  conn.close()

  return render_template(
    "billing_invoices/ledger_accounts.html",
    accounts=[row_to_dict(r) for r in rows],
  )


@bp.route("/accounts/<int:account_id>/toggle", methods=["POST"])
def toggle_account(account_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT id, is_active FROM accounts WHERE id=?", (account_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  new_flag = 0 if int(row["is_active"] or 0) == 1 else 1
  cur.execute("UPDATE accounts SET is_active=? WHERE id=?", (new_flag, account_id))
  conn.commit()
  conn.close()
  log_audit("account.toggle", "account", account_id)
  return redirect(url_for(_ledger_endpoint("ledger_accounts")))


@bp.route("/journal")
def journal():
  start_date = _parse_date(request.args.get("start_date"))
  end_date = _parse_date(request.args.get("end_date"))
  if not start_date or not end_date:
    start_date, end_date = _default_month_range()
  bp_filter = _safe_int(request.args.get("business_profile_id"))
  status_filter = (request.args.get("status") or "").strip().lower()

  conn = get_db()
  cur = conn.cursor()
  where = ["je.entry_date >= ?", "je.entry_date <= ?"]
  params = [start_date, end_date]
  if bp_filter:
    where.append("je.business_profile_id = ?")
    params.append(int(bp_filter))
  if status_filter == "draft":
    where.append(
      "COALESCE(je.approved, 0) = 0 AND COALESCE(je.posted, 0) = 0 AND COALESCE(je.reversed, 0) = 0"
    )
  elif status_filter == "approved":
    where.append(
      "COALESCE(je.approved, 0) = 1 AND COALESCE(je.posted, 0) = 0 AND COALESCE(je.reversed, 0) = 0"
    )
  elif status_filter == "posted":
    where.append("COALESCE(je.posted, 0) = 1 AND COALESCE(je.reversed, 0) = 0")
  elif status_filter == "reversed":
    where.append("COALESCE(je.reversed, 0) = 1")

  rows = cur.execute(
    f"""
    SELECT je.*,
        bp.name AS business_profile_name,
        COALESCE(t.debit_total, 0) AS debit_total,
        COALESCE(t.credit_total, 0) AS credit_total
     FROM journal_entries je
   LEFT JOIN business_profile bp ON bp.id = je.business_profile_id
   LEFT JOIN (
        SELECT entry_id,
            COALESCE(SUM(debit), 0) AS debit_total,
            COALESCE(SUM(credit), 0) AS credit_total
         FROM journal_lines
         GROUP BY entry_id
      ) t ON t.entry_id = je.id
     WHERE {" AND ".join(where)}
   ORDER BY je.entry_date DESC, je.id DESC
    """,
    params,
  ).fetchall()
  business_profiles = get_all_business_profiles()

  conn.close()
  return render_template(
    "billing_invoices/journal_list.html",
    entries=[_decorate_entry(r) for r in rows],
    start_date=start_date,
    end_date=end_date,
    business_profiles=business_profiles,
    business_profile_id=bp_filter,
    status_filter=status_filter,
  )


@bp.route("/journal/new", methods=["GET", "POST"])
def journal_new():
  conn = get_db()
  cur = conn.cursor()

  accounts = cur.execute(
    "SELECT id, code, name, type, is_active FROM accounts WHERE is_active=1 ORDER BY type, code"
  ).fetchall()
  business_profiles = get_all_business_profiles()
  default_bp = get_business_profile()
  default_date = date.today().isoformat()

  if request.method == "POST":
    entry_date = _parse_date(request.form.get("entry_date"), date.today().isoformat())
    memo = (request.form.get("memo") or "").strip()
    bp_id = _safe_int(request.form.get("business_profile_id")) or (default_bp or {}).get(
      "id", 1
    )
    currency = (
      request.form.get("currency") or (default_bp or {}).get("currency") or "USD"
    ).upper()

    account_ids = request.form.getlist("account_id")
    debits = request.form.getlist("debit")
    credits = request.form.getlist("credit")
    descriptions = request.form.getlist("line_description")

    lines = []
    for idx, acc in enumerate(account_ids):
      acc_id = _safe_int(acc)
      if not acc_id:
        continue
      debit = _safe_float(debits[idx] if idx < len(debits) else 0, 0.0)
      credit = _safe_float(credits[idx] if idx < len(credits) else 0, 0.0)
      desc = (descriptions[idx] if idx < len(descriptions) else "").strip()
      if debit > 0 and credit > 0:
        flash("/  enter.", "error")
        conn.close()
        return redirect(url_for(_ledger_endpoint("journal_new")))
      if debit <= 0 and credit <= 0:
        continue
      lines.append(
        {
          "account_id": acc_id,
          "debit": debit,
          "credit": credit,
          "description": desc,
        }
      )

    if len(lines) < 2:
      flash(" 2 table required.", "error")
      conn.close()
      return redirect(url_for(_ledger_endpoint("journal_new")))

    debit_sum = sum(l["debit"] for l in lines)
    credit_sum = sum(l["credit"] for l in lines)
    if abs(debit_sum - credit_sum) > 0.01:
      flash(" Total does not match.", "error")
      conn.close()
      return redirect(url_for(_ledger_endpoint("journal_new")))

    if get_closed_period(conn, entry_date, int(bp_id)):
      flash("Cannot save journal entry in a closed accounting period.", "error")
      conn.close()
      return redirect(url_for(_ledger_endpoint("journal_new")))

    user = get_current_user() or {}
    cur.execute(
      """
      INSERT INTO journal_entries (entry_date, memo, business_profile_id, source_type, created_by, created_at)
      VALUES (?, ?, ?, 'manual', ?, ?)
      """,
      (entry_date, memo, int(bp_id), user.get("id"), _now_iso()),
    )
    entry_id = getattr(cur, "lastrowid", None)

    for line in lines:
      cur.execute(
        """
        INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
          int(entry_id),
          int(line["account_id"]),
          line["debit"],
          line["credit"],
          currency,
          line["description"],
          _now_iso(),
        ),
      )

    conn.commit()
    conn.close()
    log_audit("journal.create", "journal_entry", entry_id)
    flash("table Draft Save.  Ledger .", "success")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))

  conn.close()
  return render_template(
    "billing_invoices/journal_new.html",
    accounts=[row_to_dict(r) for r in accounts],
    business_profiles=business_profiles,
    default_bp=default_bp,
    default_date=default_date,
  )


@bp.route("/journal/<int:entry_id>")
def journal_view(entry_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute(
    """
    SELECT je.*, bp.name AS business_profile_name
     FROM journal_entries je
   LEFT JOIN business_profile bp ON bp.id = je.business_profile_id
     WHERE je.id = ?
    """,
    (entry_id,),
  ).fetchone()
  if not row:
    conn.close()
    abort(404)
  lines = cur.execute(
    """
    SELECT jl.*, a.code AS account_code, a.name AS account_name
     FROM journal_lines jl
     JOIN accounts a ON a.id = jl.account_id
     WHERE jl.entry_id = ?
     ORDER BY jl.id
    """,
    (entry_id,),
  ).fetchall()
  entry = _decorate_entry(row)
  entry.update(_entry_sums(cur, entry_id))
  entry["is_balanced"] = (
    entry["line_count"] >= 2 and abs(entry["debit_total"] - entry["credit_total"]) <= 0.01
  )
  closed_period = _entry_period_lock(conn, entry)
  entry["closed_period"] = row_to_dict(closed_period) if closed_period else None
  if entry.get("reversed_by_entry_id"):
    reversal = cur.execute(
      "SELECT id, entry_date, memo FROM journal_entries WHERE id = ?",
      (int(entry["reversed_by_entry_id"]),),
    ).fetchone()
    entry["reversed_by_entry"] = row_to_dict(reversal)
  else:
    entry["reversed_by_entry"] = None
  if entry.get("reversal_of_entry_id"):
    original = cur.execute(
      "SELECT id, entry_date, memo FROM journal_entries WHERE id = ?",
      (int(entry["reversal_of_entry_id"]),),
    ).fetchone()
    entry["reversal_of_entry"] = row_to_dict(original)
  else:
    entry["reversal_of_entry"] = None
  entry["can_approve"] = (
    entry["status_code"] == "draft" and entry["closed_period"] is None and entry["is_balanced"]
  )
  entry["can_post"] = (
    entry["status_code"] == "approved"
    and entry["closed_period"] is None
    and entry["is_balanced"]
  )
  entry["can_reverse"] = entry["status_code"] == "posted" and entry["is_balanced"]
  conn.close()
  return render_template(
    "billing_invoices/journal_view.html",
    entry=entry,
    lines=[row_to_dict(r) for r in lines],
    today_iso=date.today().isoformat(),
  )


@bp.route("/journal/<int:entry_id>/approve", methods=["POST"])
def journal_approve(entry_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  entry = row_to_dict(row)
  if journal_status(entry) != "draft":
    conn.close()
    flash("Draft Status table  exists.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  if _entry_period_lock(conn, entry):
    conn.close()
    flash("Cannot approve a journal entry in a closed accounting period.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  sums = _entry_sums(cur, entry_id)
  if sums["line_count"] < 2 or abs(sums["debit_total"] - sums["credit_total"]) > 0.01:
    conn.close()
    flash(" table  not available.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  user = get_current_user() or {}
  cur.execute(
    """
    UPDATE journal_entries
      SET approved = 1,
        approved_at = ?,
        approved_by = ?
     WHERE id = ?
    """,
    (_now_iso(), user.get("id"), entry_id),
  )
  conn.commit()
  conn.close()
  log_audit("journal.approve", "journal_entry", entry_id)
  flash("Journal entry approved.", "success")
  return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))


@bp.route("/journal/<int:entry_id>/post", methods=["POST"])
def journal_post(entry_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  entry = row_to_dict(row)
  if journal_status(entry) != "approved":
    conn.close()
    flash(" table  exists.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  if _entry_period_lock(conn, entry):
    conn.close()
    flash("Cannot post a journal entry in a closed accounting period.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  sums = _entry_sums(cur, entry_id)
  if sums["line_count"] < 2 or abs(sums["debit_total"] - sums["credit_total"]) > 0.01:
    conn.close()
    flash(" table  not available.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  user = get_current_user() or {}
  cur.execute(
    """
    UPDATE journal_entries
      SET posted = 1,
        posted_at = ?,
        posted_by = ?
     WHERE id = ?
    """,
    (_now_iso(), user.get("id"), entry_id),
  )
  conn.commit()
  conn.close()
  log_audit("journal.post", "journal_entry", entry_id)
  flash("Journal entry posted. Ledger updated.", "success")
  return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))


@bp.route("/journal/<int:entry_id>/reverse", methods=["POST"])
def journal_reverse(entry_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  entry = row_to_dict(row)
  if journal_status(entry) != "posted":
    conn.close()
    flash(" table items exists.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))

  reversal_date = _parse_date(request.form.get("reversal_date"), date.today().isoformat())
  if get_closed_period(conn, reversal_date, int(entry.get("business_profile_id") or 0)):
    conn.close()
    flash("Cannot reverse into a closed accounting period.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))

  lines = cur.execute(
    """
    SELECT account_id, debit, credit, currency, description
     FROM journal_lines
     WHERE entry_id = ?
     ORDER BY id
    """,
    (entry_id,),
  ).fetchall()
  if len(lines) < 2:
    conn.close()
    flash(" table items not available.", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))

  user = get_current_user() or {}
  now = _now_iso()
  manual_reason = (request.form.get("reversal_memo") or "").strip()
  base_memo = (entry.get("memo") or "").strip()
  reversal_memo = manual_reason or f"items #{entry_id}"
  if base_memo:
    reversal_memo = f"{reversal_memo} | {base_memo}"

  cur.execute(
    """
    INSERT INTO journal_entries (
      entry_date, memo, business_profile_id, source_type, source_id, created_by,
      approved, approved_at, approved_by,
      posted, posted_at, posted_by,
      reversal_of_entry_id, created_at
    ) VALUES (?, ?, ?, 'reversal', ?, ?, 1, ?, ?, 1, ?, ?, ?, ?)
    """,
    (
      reversal_date,
      reversal_memo,
      int(entry.get("business_profile_id") or 1),
      entry_id,
      user.get("id"),
      now,
      user.get("id"),
      now,
      user.get("id"),
      entry_id,
      now,
    ),
  )
  reversal_id = getattr(cur, "lastrowid", None)

  for line in lines:
    data = row_to_dict(line)
    cur.execute(
      """
      INSERT INTO journal_lines (entry_id, account_id, debit, credit, currency, description, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      """,
      (
        int(reversal_id),
        int(data["account_id"]),
        float(data.get("credit") or 0),
        float(data.get("debit") or 0),
        (data.get("currency") or "USD").upper(),
        data.get("description"),
        now,
      ),
    )

  cur.execute(
    """
    UPDATE journal_entries
      SET reversed = 1,
        reversed_at = ?,
        reversed_by = ?,
        reversed_by_entry_id = ?
     WHERE id = ?
    """,
    (now, user.get("id"), int(reversal_id), entry_id),
  )
  conn.commit()
  conn.close()
  log_audit("journal.reverse", "journal_entry", entry_id)
  flash("Journal entry reversed.", "success")
  return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=reversal_id))


@bp.route("/journal/<int:entry_id>/delete", methods=["POST"])
def journal_delete(entry_id: int):
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  entry = row_to_dict(row)
  if journal_status(entry) != "draft":
    conn.close()
    flash("Draft Status table Delete exists. table items .", "error")
    return redirect(url_for(_ledger_endpoint("journal_view"), entry_id=entry_id))
  cur.execute("DELETE FROM journal_lines WHERE entry_id=?", (entry_id,))
  cur.execute("DELETE FROM journal_entries WHERE id=?", (entry_id,))
  conn.commit()
  conn.close()
  log_audit("journal.delete", "journal_entry", entry_id)
  flash("table Draft Delete.", "success")
  return redirect(url_for(_ledger_endpoint("journal")))
