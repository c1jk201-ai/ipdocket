from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.services.billing.utils import accounting_feature_disabled, sql_ci_contains_any

from ..auth import log_audit
from ..db import get_all_business_profiles, get_business_profile, get_db, row_to_dict

bp = Blueprint("expenses", __name__)


@bp.before_request
def _block_if_disabled():
  if accounting_feature_disabled():
    abort(404)


def _safe_int(value, default=None):
  try:
    if value is None:
      return default
    return int(str(value).strip())
  except Exception:
    return default


def _safe_float(value, default=0.0):
  try:
    if value is None:
      return default
    s = str(value).strip().replace(",", "")
    if not s:
      return default
    return float(s)
  except Exception:
    return default


def _parse_date(value, default=None):
  if not value:
    return default
  try:
    return date.fromisoformat(str(value).strip()).isoformat()
  except Exception:
    return default


def _now_iso():
  try:
    return datetime.now().isoformat(timespec="seconds")
  except Exception:
    return datetime.now().isoformat()


def _check_csrf():
  if request.method == "POST":
    try:
      from flask_wtf.csrf import validate_csrf

      validate_csrf(request.form.get("csrf_token"))
    except Exception:
      abort(400, "CSRF  failed.")


@bp.route("", methods=["GET", "POST"])
def list_expenses():
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()

  if request.method == "POST":
    expense_date = _parse_date(request.form.get("expense_date"), date.today().isoformat())
    bp_id = _safe_int(request.form.get("business_profile_id"))
    category_id = _safe_int(request.form.get("category_id"))
    vendor_name = (request.form.get("vendor_name") or "").strip()
    vendor_tax_id = (request.form.get("vendor_tax_id") or "").strip()
    description = (request.form.get("description") or "").strip()
    memo = (request.form.get("memo") or "").strip()

    if not category_id:
      flash(" category select.", "error")
      return redirect(url_for("billing_invoices.expenses.list_expenses"))
    tax_type = (request.form.get("tax_type") or "tax_invoice").strip()
    input_vat_eligible = (
      1 if request.form.get("input_vat_eligible") in ("1", "on", "true") else 0
    )

    net_amount = _safe_float(request.form.get("net_amount"), 0.0)
    vat_amount = _safe_float(request.form.get("vat_amount"), 0.0)
    total_amount = net_amount + vat_amount

    if not bp_id:
      bp_row = get_business_profile()
      bp_id = (bp_row or {}).get("id", 1)
    else:
      bp_row = get_business_profile(bp_id)

    currency = (request.form.get("currency") or (bp_row or {}).get("currency") or "USD").upper()

    if not expense_date:
      flash("days confirm.", "error")
      return redirect(url_for("billing_invoices.expenses.list_expenses"))

    cur.execute(
      """
      INSERT INTO expenses (
        business_profile_id, expense_date, vendor_name, vendor_tax_id, description,
        category_id, currency, net_amount, vat_amount, total_amount, tax_type,
        input_vat_eligible, memo, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        int(bp_id),
        expense_date,
        vendor_name,
        vendor_tax_id,
        description,
        category_id,
        currency,
        net_amount,
        vat_amount,
        total_amount,
        tax_type,
        input_vat_eligible,
        memo,
        _now_iso(),
        _now_iso(),
      ),
    )
    conn.commit()
    expense_id = getattr(cur, "lastrowid", None)
    log_audit("expense.create", "expense", expense_id)
    flash(" Registration.", "success")
    return redirect(url_for("billing_invoices.expenses.list_expenses"))

  # Filters
  start_date = _parse_date(request.args.get("start_date"))
  end_date = _parse_date(request.args.get("end_date"))
  category_filter = _safe_int(request.args.get("category_id"))
  bp_filter = _safe_int(request.args.get("business_profile_id"))
  currency_filter = (request.args.get("currency") or "ALL").strip().upper() or "ALL"
  if currency_filter != "ALL":
    if not currency_filter.isalnum() or len(currency_filter) > 8:
      currency_filter = "ALL"
  q = (request.args.get("q") or "").strip()

  try:
    page = int(request.args.get("page", 1) or 1)
  except Exception:
    page = 1
  page = max(page, 1)
  try:
    per_page = int(request.args.get("per_page", 50) or 50)
  except Exception:
    per_page = 50
  per_page = max(10, min(per_page, 200))

  where = []
  params = []
  if start_date:
    where.append("e.expense_date >= ?")
    params.append(start_date)
  if end_date:
    where.append("e.expense_date <= ?")
    params.append(end_date)
  if category_filter:
    where.append("e.category_id = ?")
    params.append(int(category_filter))
  if bp_filter:
    where.append("e.business_profile_id = ?")
    params.append(int(bp_filter))
  if currency_filter != "ALL":
    where.append("COALESCE(UPPER(e.currency), 'USD') = ?")
    params.append(currency_filter)
  if q:
    search_clause, search_params = sql_ci_contains_any(
      ["e.vendor_name", "e.description", "e.memo"],
      q,
    )
    if search_clause:
      where.append(search_clause)
      params.extend(search_params)

  where_sql = f"WHERE {' AND '.join(where)}" if where else ""

  count_row = cur.execute(f"SELECT COUNT(*) FROM expenses e {where_sql}", params).fetchone()
  total_count = count_row[0] if count_row else 0
  total_pages = max(1, (total_count + per_page - 1) // per_page)
  if page > total_pages:
    page = total_pages
  offset = (page - 1) * per_page

  rows = cur.execute(
    f"""
    SELECT e.*, c.name AS category_name, c.code AS category_code
    FROM expenses e
    LEFT JOIN expense_categories c ON c.id = e.category_id
    {where_sql}
    ORDER BY e.expense_date DESC, e.id DESC
    LIMIT ? OFFSET ?
    """,
    params + [per_page, offset],
  ).fetchall()

  totals_by_currency_rows = cur.execute(
    f"""
    SELECT COALESCE(UPPER(e.currency), 'USD') AS currency,
        COALESCE(SUM(e.net_amount), 0) AS total_net,
        COALESCE(SUM(e.vat_amount), 0) AS total_vat,
        COALESCE(SUM(e.total_amount), 0) AS total_sum
    FROM expenses e
    {where_sql}
    GROUP BY COALESCE(UPPER(e.currency), 'USD')
    ORDER BY COALESCE(UPPER(e.currency), 'USD')
    """,
    params,
  ).fetchall()
  totals_by_currency = [
    {
      "currency": r[0] if r and r[0] else "USD",
      "net": r[1] if r else 0,
      "vat": r[2] if r else 0,
      "sum": r[3] if r else 0,
    }
    for r in totals_by_currency_rows
  ]
  if not totals_by_currency and currency_filter != "ALL":
    totals_by_currency = [{"currency": currency_filter, "net": 0, "vat": 0, "sum": 0}]

  total_net = sum(float(t.get("net") or 0) for t in totals_by_currency)
  total_vat = sum(float(t.get("vat") or 0) for t in totals_by_currency)
  total_sum = sum(float(t.get("sum") or 0) for t in totals_by_currency)

  categories = [
    row_to_dict(r)
    for r in cur.execute(
      "SELECT id, code, name, vat_deductible, is_default FROM expense_categories ORDER BY is_default DESC, name"
    ).fetchall()
  ]
  business_profiles = get_all_business_profiles()
  currencies = set()
  for bp_row in business_profiles:
    cur_code = str((bp_row.get("currency") or "USD")).strip().upper()
    if cur_code:
      currencies.add(cur_code)
  currency_rows = cur.execute(
    "SELECT DISTINCT COALESCE(UPPER(currency), 'USD') FROM expenses ORDER BY 1"
  ).fetchall()
  for crow in currency_rows:
    if crow and crow[0]:
      currencies.add(str(crow[0]).upper())
  currencies = sorted(currencies)

  conn.close()

  return render_template(
    "billing_invoices/expenses_list.html",
    expenses=[row_to_dict(r) for r in rows],
    categories=categories,
    business_profiles=business_profiles,
    start_date=start_date,
    end_date=end_date,
    category_filter=category_filter,
    bp_filter=bp_filter,
    currency_filter=currency_filter,
    currencies=currencies,
    q=q,
    page=page,
    per_page=per_page,
    total_count=total_count,
    total_pages=total_pages,
    total_net=total_net,
    total_vat=total_vat,
    total_sum=total_sum,
    totals_by_currency=totals_by_currency,
  )


@bp.route("/<int:expense_id>/edit", methods=["GET", "POST"])
def edit_expense(expense_id: int):
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)

  if request.method == "POST":
    expense_date = _parse_date(request.form.get("expense_date"), date.today().isoformat())
    bp_id = _safe_int(request.form.get("business_profile_id")) or row["business_profile_id"]
    category_id = _safe_int(request.form.get("category_id"))
    vendor_name = (request.form.get("vendor_name") or "").strip()
    vendor_tax_id = (request.form.get("vendor_tax_id") or "").strip()
    description = (request.form.get("description") or "").strip()
    memo = (request.form.get("memo") or "").strip()

    if not category_id:
      conn.close()
      flash(" category select.", "error")
      return redirect(
        url_for("billing_invoices.expenses.edit_expense", expense_id=expense_id)
      )
    tax_type = (request.form.get("tax_type") or "tax_invoice").strip()
    input_vat_eligible = (
      1 if request.form.get("input_vat_eligible") in ("1", "on", "true") else 0
    )
    net_amount = _safe_float(request.form.get("net_amount"), 0.0)
    vat_amount = _safe_float(request.form.get("vat_amount"), 0.0)
    total_amount = net_amount + vat_amount

    bp_row = get_business_profile(bp_id) if bp_id else None
    currency = (
      request.form.get("currency")
      or (bp_row or {}).get("currency")
      or row["currency"]
      or "USD"
    ).upper()

    cur.execute(
      """
      UPDATE expenses
        SET business_profile_id=?,
          expense_date=?,
          vendor_name=?,
          vendor_tax_id=?,
          description=?,
          category_id=?,
          currency=?,
          net_amount=?,
          vat_amount=?,
          total_amount=?,
          tax_type=?,
          input_vat_eligible=?,
          memo=?,
          updated_at=?
       WHERE id=?
      """,
      (
        int(bp_id or 1),
        expense_date,
        vendor_name,
        vendor_tax_id,
        description,
        category_id,
        currency,
        net_amount,
        vat_amount,
        total_amount,
        tax_type,
        input_vat_eligible,
        memo,
        _now_iso(),
        expense_id,
      ),
    )
    conn.commit()
    conn.close()
    log_audit("expense.update", "expense", expense_id)
    flash(" Edit.", "success")
    return redirect(url_for("billing_invoices.expenses.list_expenses"))

  categories = [
    row_to_dict(r)
    for r in cur.execute(
      "SELECT id, code, name, vat_deductible, is_default FROM expense_categories ORDER BY is_default DESC, name"
    ).fetchall()
  ]
  business_profiles = get_all_business_profiles()
  conn.close()

  return render_template(
    "billing_invoices/expense_edit.html",
    expense=row_to_dict(row),
    categories=categories,
    business_profiles=business_profiles,
  )


@bp.route("/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id: int):
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT id FROM expenses WHERE id=?", (expense_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)
  cur.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
  conn.commit()
  conn.close()
  log_audit("expense.delete", "expense", expense_id)
  flash(" Delete.", "success")
  return redirect(url_for("billing_invoices.expenses.list_expenses"))


@bp.route("/categories", methods=["GET", "POST"])
def list_categories():
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()

  if request.method == "POST":
    code = (request.form.get("code") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    account_id = _safe_int(request.form.get("account_id"))
    vat_deductible = 1 if request.form.get("vat_deductible") in ("1", "on", "true") else 0
    is_default = 1 if request.form.get("is_default") in ("1", "on", "true") else 0

    if not name:
      flash("Enter a category name.", "error")
      return redirect(url_for("billing_invoices.expenses.list_categories"))

    cur.execute(
      """
      INSERT INTO expense_categories (code, name, account_id, vat_deductible, is_default)
      VALUES (?, ?, ?, ?, ?)
      """,
      (code or None, name, account_id, vat_deductible, is_default),
    )
    conn.commit()
    category_id = getattr(cur, "lastrowid", None)
    log_audit("expense_category.create", "expense_category", category_id)
    flash("Category added.", "success")
    return redirect(url_for("billing_invoices.expenses.list_categories"))

  categories = cur.execute(
    """
    SELECT c.*, a.code AS account_code, a.name AS account_name
    FROM expense_categories c
    LEFT JOIN accounts a ON a.id = c.account_id
    ORDER BY c.is_default DESC, c.name
    """
  ).fetchall()
  accounts = cur.execute(
    "SELECT id, code, name, type, is_active FROM accounts ORDER BY type, code"
  ).fetchall()
  conn.close()

  return render_template(
    "billing_invoices/expense_categories.html",
    categories=[row_to_dict(r) for r in categories],
    accounts=[row_to_dict(r) for r in accounts],
  )


@bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
def edit_category(category_id: int):
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM expense_categories WHERE id=?", (category_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)

  if request.method == "POST":
    code = (request.form.get("code") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    account_id = _safe_int(request.form.get("account_id"))
    vat_deductible = 1 if request.form.get("vat_deductible") in ("1", "on", "true") else 0
    is_default = 1 if request.form.get("is_default") in ("1", "on", "true") else 0

    if not name:
      flash("Enter a category name.", "error")
      return redirect(
        url_for("billing_invoices.expenses.edit_category", category_id=category_id)
      )

    cur.execute(
      """
      UPDATE expense_categories
        SET code=?,
          name=?,
          account_id=?,
          vat_deductible=?,
          is_default=?
       WHERE id=?
      """,
      (code or None, name, account_id, vat_deductible, is_default, category_id),
    )
    conn.commit()
    conn.close()
    log_audit("expense_category.update", "expense_category", category_id)
    flash(" category Edit.", "success")
    return redirect(url_for("billing_invoices.expenses.list_categories"))

  accounts = cur.execute(
    "SELECT id, code, name, type, is_active FROM accounts ORDER BY type, code"
  ).fetchall()
  conn.close()

  return render_template(
    "billing_invoices/expense_category_edit.html",
    category=row_to_dict(row),
    accounts=[row_to_dict(r) for r in accounts],
  )


@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id: int):
  _check_csrf()
  conn = get_db()
  cur = conn.cursor()
  row = cur.execute("SELECT id FROM expense_categories WHERE id=?", (category_id,)).fetchone()
  if not row:
    conn.close()
    abort(404)

  used_row = cur.execute(
    "SELECT COUNT(*) FROM expenses WHERE category_id=?", (category_id,)
  ).fetchone()
  used_count = used_row[0] if used_row else 0
  if used_count:
    conn.close()
    flash("  category cannot be deleted.", "error")
    return redirect(url_for("billing_invoices.expenses.list_categories"))

  cur.execute("DELETE FROM expense_categories WHERE id=?", (category_id,))
  conn.commit()
  conn.close()
  log_audit("expense_category.delete", "expense_category", category_id)
  flash(" category Delete.", "success")
  return redirect(url_for("billing_invoices.expenses.list_categories"))
