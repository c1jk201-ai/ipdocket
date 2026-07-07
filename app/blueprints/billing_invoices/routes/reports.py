from __future__ import annotations

from calendar import monthrange
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.services.billing.utils import accounting_feature_disabled

from ..accounting import (
  build_balance_sheet,
  build_income_statement,
  close_accounting_period,
  fetch_trial_balance_rows,
  list_accounting_periods,
  parse_date,
  reopen_accounting_period,
  safe_int,
)
from ..auth import get_current_user, log_audit
from ..db import get_all_business_profiles, get_business_profile, get_db, row_to_dict

bp = Blueprint("reports", __name__)


@bp.before_request
def _block_if_disabled():
  if accounting_feature_disabled():
    abort(404)


def _reports_endpoint(name: str) -> str:
  if (request.endpoint or "").startswith("business.accounting_"):
    return f"business.accounting_{name}"
  return f"billing_invoices.reports.{name}"


def _resolve_period(year: int, period: str):
  period = (period or "").upper()
  if period in ("1", "2", "3", "4"):
    period = f"Q{period}"
  if period in ("Q1", "Q2", "Q3", "Q4"):
    q = int(period[1])
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    start = date(year, start_month, 1)
    end = date(year, end_month, monthrange(year, end_month)[1])
    return start, end, f"{year} {q}quarter"
  if period == "H1":
    start = date(year, 1, 1)
    end = date(year, 6, 30)
    return start, end, f"{year} first half"
  if period == "H2":
    start = date(year, 7, 1)
    end = date(year, 12, 31)
    return start, end, f"{year} second half"

  today = date.today()
  q = ((today.month - 1) // 3) + 1
  start_month = (q - 1) * 3 + 1
  end_month = start_month + 2
  start = date(today.year, start_month, 1)
  end = date(today.year, end_month, monthrange(today.year, end_month)[1])
  return start, end, f"{today.year} {q}quarter"


def _default_month_range():
  today = date.today()
  start = today.replace(day=1)
  end = today.replace(day=monthrange(today.year, today.month)[1])
  return start, end


def _resolve_statement_range():
  start = parse_date(request.args.get("start_date"))
  end = parse_date(request.args.get("end_date"))
  if not start or not end:
    default_start, default_end = _default_month_range()
    return (
      default_start.isoformat(),
      default_end.isoformat(),
      (f"{default_start.isoformat()} ~ {default_end.isoformat()}"),
    )
  if end < start:
    start, end = end, start
  return start, end, f"{start} ~ {end}"


def _report_context(allow_all_currency=False):
  business_profiles = get_all_business_profiles()
  bp_filter = safe_int(request.args.get("business_profile_id"))
  bp_row = get_business_profile(bp_filter) if bp_filter else get_business_profile()
  currency = (request.args.get("currency") or "").upper()
  if not currency:
    currency = (bp_row or {}).get("currency") or "USD"
  if not allow_all_currency and currency == "ALL":
    currency = (bp_row or {}).get("currency") or "USD"
  try:
    currencies = sorted({(bp.get("currency") or "USD").upper() for bp in business_profiles})
  except Exception:
    currencies = ["USD"]
  return {
    "business_profiles": business_profiles,
    "business_profile_id": bp_filter,
    "bp_filter": bp_filter,
    "bp_row": bp_row,
    "currency": currency,
    "currencies": currencies,
  }


def _vat_multiplier(vat_rate: float) -> float:
  try:
    vr = float(vat_rate or 0)
  except Exception:
    return 0.0
  if vr > 1:
    return vr / 100.0
  return vr


@bp.route("")
@bp.route("/")
def reports_home():
  ctx = _report_context()
  return render_template("billing_invoices/reports_home.html", **ctx)


@bp.route("/trial-balance")
def trial_balance_report():
  ctx = _report_context()
  start_date, end_date, period_label = _resolve_statement_range()

  conn = get_db()
  rows = fetch_trial_balance_rows(
    conn,
    start_date=start_date,
    end_date=end_date,
    business_profile_id=ctx["bp_filter"],
    currency=ctx["currency"],
    include_zero=(request.args.get("include_zero") in ("1", "true", "on")),
  )
  conn.close()

  totals = {
    "opening_debit": sum(float(row.get("opening_debit") or 0) for row in rows),
    "opening_credit": sum(float(row.get("opening_credit") or 0) for row in rows),
    "period_debit": sum(float(row.get("period_debit") or 0) for row in rows),
    "period_credit": sum(float(row.get("period_credit") or 0) for row in rows),
    "ending_debit": sum(float(row.get("ending_debit") or 0) for row in rows),
    "ending_credit": sum(float(row.get("ending_credit") or 0) for row in rows),
  }

  return render_template(
    "billing_invoices/trial_balance.html",
    rows=rows,
    totals=totals,
    period_label=period_label,
    start_date=start_date,
    end_date=end_date,
    include_zero=request.args.get("include_zero") in ("1", "true", "on"),
    **ctx,
  )


@bp.route("/income-statement")
def income_statement_report():
  ctx = _report_context()
  start_date, end_date, period_label = _resolve_statement_range()

  conn = get_db()
  rows = fetch_trial_balance_rows(
    conn,
    start_date=start_date,
    end_date=end_date,
    business_profile_id=ctx["bp_filter"],
    currency=ctx["currency"],
    include_zero=False,
  )
  conn.close()
  report = build_income_statement(rows)

  return render_template(
    "billing_invoices/income_statement.html",
    report=report,
    period_label=period_label,
    start_date=start_date,
    end_date=end_date,
    **ctx,
  )


@bp.route("/balance-sheet")
def balance_sheet_report():
  ctx = _report_context()
  as_of_date = parse_date(request.args.get("as_of_date"), date.today().isoformat())
  conn = get_db()
  report = build_balance_sheet(
    conn,
    as_of_date=as_of_date,
    business_profile_id=ctx["bp_filter"],
    currency=ctx["currency"],
  )
  conn.close()

  return render_template(
    "billing_invoices/balance_sheet.html",
    report=report,
    as_of_date=as_of_date,
    **ctx,
  )


@bp.route("/period-close", methods=["GET", "POST"])
def period_close():
  ctx = _report_context()
  default_start, default_end = _default_month_range()

  if request.method == "POST":
    bp_id = safe_int(request.form.get("business_profile_id"))
    start_date = parse_date(request.form.get("start_date"), default_start.isoformat())
    end_date = parse_date(request.form.get("end_date"), default_end.isoformat())
    notes = (request.form.get("notes") or "").strip()
    period_type = (request.form.get("period_type") or "custom").strip().lower() or "custom"
    user = get_current_user() or {}

    conn = get_db()
    try:
      period_id = close_accounting_period(
        conn,
        business_profile_id=bp_id,
        start_date=start_date,
        end_date=end_date,
        user_id=user.get("id"),
        notes=notes,
        period_type=period_type,
      )
      conn.commit()
    except ValueError as exc:
      conn.rollback()
      flash(str(exc), "error")
      conn.close()
      return redirect(url_for(_reports_endpoint("period_close")))
    conn.close()
    log_audit("accounting_period.close", "accounting_period", period_id)
    flash("Accounting period closed.", "success")
    return redirect(url_for(_reports_endpoint("period_close")))

  conn = get_db()
  periods = list_accounting_periods(conn, ctx["bp_filter"])
  conn.close()

  return render_template(
    "billing_invoices/period_close.html",
    periods=periods,
    default_start=default_start.isoformat(),
    default_end=default_end.isoformat(),
    **ctx,
  )


@bp.route("/period-close/<int:period_id>/reopen", methods=["POST"])
def period_reopen(period_id: int):
  conn = get_db()
  user = get_current_user() or {}
  try:
    period = reopen_accounting_period(conn, period_id, user.get("id"))
    conn.commit()
  except ValueError as exc:
    conn.rollback()
    conn.close()
    flash(str(exc), "error")
    return redirect(url_for(_reports_endpoint("period_close")))
  conn.close()
  log_audit("accounting_period.reopen", "accounting_period", period_id)
  flash(
    f"{period.get('start_date')} ~ {period.get('end_date')} Period column.",
    "success",
  )
  return redirect(url_for(_reports_endpoint("period_close")))


@bp.route("/vat")
def vat_report():
  today = date.today()
  year = safe_int(request.args.get("year"), today.year)
  period = (request.args.get("period") or "").strip()

  basis_raw = (request.args.get("basis") or "issue_date").strip().lower()
  if basis_raw in ("tax_issued", "tax_issued_at", "taxissued"):
    basis = "tax_issued"
  elif basis_raw == "issued":
    basis = "tax_issued"
  elif basis_raw == "settlement":
    basis = "issue_date"
  else:
    basis = "issue_date"

  status_scope_raw = (request.args.get("status_scope") or "issued").strip().lower()
  if status_scope_raw in ("completed", "done"):
    status_scope = "issued"
  elif status_scope_raw in ("paid",):
    status_scope = "paid"
  elif status_scope_raw in ("all", "any"):
    status_scope = "all"
  else:
    status_scope = "issued"

  start_arg = request.args.get("start_date")
  end_arg = request.args.get("end_date")
  if start_arg and end_arg:
    try:
      start_date = date.fromisoformat(start_arg)
      end_date = date.fromisoformat(end_arg)
      if end_date < start_date:
        start_date, end_date = end_date, start_date
      period_label = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
    except Exception:
      start_date, end_date, period_label = _resolve_period(year, period)
  else:
    start_date, end_date, period_label = _resolve_period(year, period)

  ctx = _report_context(allow_all_currency=True)
  bp_filter = ctx["bp_filter"]
  currency = ctx["currency"]

  date_expr = "issue_date"
  if basis == "tax_issued":
    date_expr = "substr(tax_issued_at, 1, 10)"

  inv_where = [f"{date_expr} >= ?", f"{date_expr} <= ?"]
  inv_params = [start_date.isoformat(), end_date.isoformat()]
  if basis == "tax_issued":
    inv_where.append("tax_issued_at IS NOT NULL")

  status_expr = "COALESCE(billing_status, status)"
  if status_scope == "issued":
    inv_where.append(f"{status_expr} IN ('tax_issued','cash_issued','processed')")
  elif status_scope == "paid":
    inv_where.append("payment_status = 'paid'")
  else:
    inv_where.append(f"({status_expr} IS NULL OR {status_expr} NOT IN ('draft','void'))")

  if bp_filter:
    inv_where.append("business_profile_id = ?")
    inv_params.append(int(bp_filter))
  if currency and currency != "ALL":
    inv_where.append("currency = ?")
    inv_params.append(currency)
  inv_where_sql = " AND ".join(inv_where)

  exp_where = ["expense_date >= ?", "expense_date <= ?"]
  exp_params = [start_date.isoformat(), end_date.isoformat()]
  if bp_filter:
    exp_where.append("business_profile_id = ?")
    exp_params.append(int(bp_filter))
  if currency and currency != "ALL":
    exp_where.append("currency = ?")
    exp_params.append(currency)
  exp_where_sql = " AND ".join(exp_where)

  conn = get_db()
  cur = conn.cursor()

  invoices = cur.execute(
    f"""
    SELECT tax, subtotal, total, currency
     FROM invoices
     WHERE {inv_where_sql}
    """,
    inv_params,
  ).fetchall()

  sales_taxable = 0.0
  sales_vat = 0.0
  sales_total = 0.0
  for row in invoices:
    tax = float(row[0] or 0)
    taxable = float(row[1] or 0)
    sales_taxable += taxable
    sales_vat += tax
    sales_total += taxable + tax

  exp_totals = cur.execute(
    f"""
    SELECT COALESCE(SUM(net_amount), 0),
        COALESCE(SUM(vat_amount), 0),
        COALESCE(SUM(total_amount), 0),
        COALESCE(SUM(CASE WHEN input_vat_eligible = 1 THEN vat_amount ELSE 0 END), 0)
     FROM expenses
     WHERE {exp_where_sql}
    """,
    exp_params,
  ).fetchone()

  exp_by_category = cur.execute(
    f"""
    SELECT COALESCE(c.name, 'Uncategorized') AS category_name,
        COALESCE(SUM(e.net_amount), 0) AS net_amount,
        COALESCE(SUM(e.vat_amount), 0) AS vat_amount,
        COALESCE(SUM(e.total_amount), 0) AS total_amount,
        COALESCE(SUM(CASE WHEN e.input_vat_eligible = 1 THEN e.vat_amount ELSE 0 END), 0) AS input_vat
     FROM expenses e
   LEFT JOIN expense_categories c ON c.id = e.category_id
     WHERE {exp_where_sql}
   GROUP BY COALESCE(c.name, 'Uncategorized')
   ORDER BY total_amount DESC
    """,
    exp_params,
  ).fetchall()
  conn.close()

  exp_net = exp_totals[0] if exp_totals else 0.0
  exp_vat = exp_totals[1] if exp_totals else 0.0
  exp_total = exp_totals[2] if exp_totals else 0.0
  exp_input_vat = exp_totals[3] if exp_totals else 0.0
  report = {
    "sales_taxable": sales_taxable,
    "sales_vat": sales_vat,
    "sales_total": sales_total,
    "expense_net": exp_net,
    "expense_vat": exp_vat,
    "expense_total": exp_total,
    "input_vat": exp_input_vat,
    "vat_payable": sales_vat - exp_input_vat,
  }

  return render_template(
    "billing_invoices/vat_report.html",
    report=report,
    period_label=period_label,
    year=year,
    period=(period or "").upper(),
    basis=basis,
    status_scope=status_scope,
    start_date=start_date.isoformat(),
    end_date=end_date.isoformat(),
    expense_categories=[row_to_dict(r) for r in exp_by_category],
    **ctx,
  )
