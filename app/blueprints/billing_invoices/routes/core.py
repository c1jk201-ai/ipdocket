from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

from flask import (
  Blueprint,
  Response,
  abort,
  current_app,
  flash,
  jsonify,
  redirect,
  render_template,
  request,
  send_from_directory,
  session,
  url_for,
)

from app.utils.error_logging import report_swallowed_exception
from app.utils.network_access import check_admin_or_internal_access
from app.utils.url_helpers import safe_next_url, safe_referrer_path

from ..auth import role_required
from ..db import get_all_business_profiles, get_business_profile, get_db, row_to_dict
from ..settlement import is_default_settlement_split

bp = Blueprint("core", __name__)
CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")


def _normalize_currency_code(value: str | None, default: str = "USD") -> str:
  code = (value or "").strip().upper()
  if CURRENCY_CODE_RE.fullmatch(code):
    return code
  return default


def _safe_next_url(value: str):
  return safe_next_url(value)


def _parse_business_profile_filter(
  value: str, allowed_currencies: set[str] | None = None
) -> tuple[str, int | None, str | None]:
  """
  Normalize `business_profile_id` query args.

  Returns (normalized_value, bp_id_int, currency_code).
  - Numeric ID: ("123", 123, None)
  - Currency group: ("C:USD", None, "USD")
  - Invalid/empty: ("", None, None)
  """

  raw = (value or "").strip()
  if not raw:
    return ("", None, None)

  raw_upper = raw.upper()
  if raw_upper.startswith("C:"):
    currency_code = raw_upper.split(":", 1)[1].strip().upper()
    if not currency_code:
      return ("", None, None)
    if not currency_code.isalpha() or len(currency_code) > 10:
      return ("", None, None)
    if allowed_currencies and currency_code not in allowed_currencies:
      return ("", None, None)
    return (f"C:{currency_code}", None, currency_code)

  try:
    bp_id_int = int(raw)
  except Exception:
    return ("", None, None)
  if bp_id_int <= 0:
    return ("", None, None)
  return (str(bp_id_int), bp_id_int, None)


@bp.app_errorhandler(400)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(500)
def _err(e):
  # Previous page URL (if not available )
  back_url = safe_referrer_path(request) or url_for("billing_invoices.core.dashboard")
  return (
    render_template("billing_invoices/billing_error.html", e=e, back_url=back_url),
    getattr(e, "code", 500),
  )


@bp.route("/")
@role_required("staff")
def dashboard():
  if request.endpoint == "billing_invoices.core.dashboard":
    return redirect(url_for("business.index", **request.args))

  bp_id_raw = request.args.get("business_profile_id", "")
  period = request.args.get("period", "1month").strip()
  # metric toggle: 'estimated' (paid+tax/cash/processed) or 'issued' (total issued)
  metric = (request.args.get("metric") or "estimated").strip().lower()
  if metric not in ("estimated", "issued"):
    metric = "estimated"
  # basis toggle: 'settlement' (Settlement , ) or 'issued' (Issued )
  basis = (request.args.get("basis") or "settlement").strip().lower()
  if basis not in ("settlement", "issued"):
    basis = "settlement"
  all_profiles = get_all_business_profiles()
  # Build distinct currencies for group selection
  try:
    currencies = sorted({((p["currency"] or "USD").upper()) for p in all_profiles})
  except Exception:
    currencies = ["USD"]

  bp_id, bp_id_int, bp_currency_code = _parse_business_profile_filter(
    bp_id_raw, allowed_currencies=set(currencies) if currencies else None
  )
  # If filtering by currency group like 'C:USD', do not resolve a single business profile
  if bp_currency_code:
    bp_row = None
  else:
    if bp_id_int is not None:
      bp_row = get_business_profile(bp_id_int)
    else:
      bp_row = get_business_profile()

  conn = get_db()
  today = date.today()
  y = today.year
  # Default: This month~
  start_date_dt = today.replace(day=1)
  end_date_dt = today
  period_label = "This month"

  if period == "3months":
    start_date_dt = today - timedelta(days=90)
    period_label = "Last 3 months"
  elif period == "6months":
    start_date_dt = today - timedelta(days=180)
    period_label = "Last 6 months"
  elif period == "12months":
    start_date_dt = today - timedelta(days=365)
    period_label = "Last 12 months"
  elif period == "Q1":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 3, 31)
    period_label = f"{y} 1quarter"
  elif period == "Q2":
    start_date_dt = date(y, 4, 1)
    end_date_dt = date(y, 6, 30)
    period_label = f"{y} 2quarter"
  elif period == "Q3":
    start_date_dt = date(y, 7, 1)
    end_date_dt = date(y, 9, 30)
    period_label = f"{y} 3quarter"
  elif period == "Q4":
    start_date_dt = date(y, 10, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} 4quarter"
  elif period == "H1":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 6, 30)
    period_label = f"{y} first half"
  elif period == "H2":
    start_date_dt = date(y, 7, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} second half"
  elif period == "1year":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} All"

  # User Period() Apply
  start_arg = (request.args.get("start_date") or "").strip()
  end_arg = (request.args.get("end_date") or "").strip()
  if start_arg and end_arg:
    try:
      sdt = date.fromisoformat(start_arg)
      edt = date.fromisoformat(end_arg)
      if edt < sdt:
        sdt, edt = edt, sdt
      start_date_dt = sdt
      end_date_dt = edt
      period_label = f"{sdt.isoformat()} ~ {edt.isoformat()}"
    except ValueError:
      pass

  # column 
  start_date = start_date_dt.isoformat()
  end_date = end_date_dt.isoformat()

  params = [start_date, end_date]
  where_bp = ""
  if bp_currency_code:
    where_bp = " AND invoices.currency = ?"
    params.append(bp_currency_code)
  elif bp_id_int is not None:
    where_bp = " AND business_profile_id = ?"
    params.append(bp_id_int)

  cur = conn.cursor()

  # ------------------------- : Invoice USD times -------------------------
  # Settlement  Period Invoice .
  # basis='settlement'  Issuing business profile(bp_id) Filters ,
  # Settlement from Business profile  bp_id Apply.
  if basis == "settlement":
    if bp_currency_code:
      # Currency Filters (: C:USD) Apply
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND currency = ?""",
        (start_date, end_date, bp_currency_code),
      ).fetchall()
    else:
      # Business profile Issued All Invoice 
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ?""",
        (start_date, end_date),
      ).fetchall()

  else:
    # Issued reference date Existing Issuing business profile/Currency to Filters
    if bp_currency_code:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND currency = ?""",
        (start_date, end_date, bp_currency_code),
      ).fetchall()
    elif bp_id_int is not None:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND business_profile_id = ?""",
        (start_date, end_date, bp_id_int),
      ).fetchall()
    else:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ?""",
        (start_date, end_date),
      ).fetchall()

  # Convert Row objects to dicts for string key access
  base_invoices = [row_to_dict(row) for row in base_invoices]

  # ------------------------- line_items Total  (N+1 ) -------------------------
  # Period Invoice , invoice_id SUM  .
  # GROUP BY admin/foreign( taxable) Total  times.
  line_where = ""
  line_params = [start_date, end_date]
  if bp_currency_code:
    line_where = " AND invoices.currency = ?"
    line_params.append(bp_currency_code)
  elif basis != "settlement" and bp_id_int is not None:
    # Issued reference date Issuing business profile Filters Apply
    line_where = " AND invoices.business_profile_id = ?"
    line_params.append(bp_id_int)

  _totals_by_invoice = {}
  try:
    rows = cur.execute(
      f"""
      SELECT line_items.invoice_id AS invoice_id,
          COALESCE(SUM(
            CASE
             WHEN line_items.item_type = 'admin'
             AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
             THEN (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
             ELSE 0
            END
          ), 0) AS admin_total,
          COALESCE(SUM(
            CASE
             WHEN line_items.item_type = 'foreign'
             AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
             THEN
              CASE
               WHEN COALESCE(line_items.fx_rate_used, 0) > 0 THEN
                (COALESCE(line_items.fx_fee,0) + COALESCE(line_items.fx_gov,0))
                * COALESCE(line_items.fx_rate_used, 0)
                * (1 + COALESCE(line_items.fx_markup,0)/100.0)
               ELSE
                (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
              END
             ELSE 0
            END
          ), 0) AS foreign_total
       FROM line_items
       JOIN invoices ON invoices.id = line_items.invoice_id
       WHERE invoices.issue_date >= ? AND invoices.issue_date <= ?{line_where}
       GROUP BY line_items.invoice_id
      """,
      line_params,
    ).fetchall()
    for r in rows:
      try:
        iid = int(r["invoice_id"])
      except Exception:
        continue
      _totals_by_invoice[iid] = {
        "admin_total": float(r["admin_total"] or 0.0),
        "foreign_total": float(r["foreign_total"] or 0.0),
      }
  except Exception:
    _totals_by_invoice = {}

  # Official fee/Foreign expense Total times  ( value )
  def _admin_total_for_invoice(invoice_id: int) -> float:
    try:
      return float(
        (_totals_by_invoice.get(int(invoice_id)) or {}).get("admin_total", 0.0) or 0.0
      )
    except Exception:
      return 0.0

  def _foreign_total_for_invoice(invoice_id: int) -> float:
    try:
      return float(
        (_totals_by_invoice.get(int(invoice_id)) or {}).get("foreign_total", 0.0) or 0.0
      )
    except Exception:
      return 0.0

  def _service_amount_for_invoice(inv_row) -> float:
    inv_row = row_to_dict(inv_row)
    try:
      inv_subtotal = float(inv_row.get("subtotal") or 0.0)
    except Exception:
      inv_subtotal = 0.0
    return max(
      inv_subtotal
      - _admin_total_for_invoice(inv_row.get("id"))
      - _foreign_total_for_invoice(inv_row.get("id")),
      0.0,
    )

  def _iter_settlement_shares(inv_row):
    """ Invoice Service Revenue(Taxable amount, Sales tax ; Official fee/Foreign expense ) Settlement to (bp_id, currency, amount, is_outstanding, is_estimated) yield."""
    inv_row = row_to_dict(inv_row)
    service_amount = _service_amount_for_invoice(inv_row)

    billing_status = inv_row.get("billing_status")
    payment_status = inv_row.get("payment_status")
    currency = _normalize_currency_code(inv_row.get("currency"))

    # Outstanding balance : (billing sent) + Payment 
    is_outstanding = billing_status == "sent" and payment_status in (
      "unpaid",
      "pending",
    )
    # Revenue : payment paid billing tax_issued/cash_issued/processed
    is_estimated = payment_status == "paid" or billing_status in (
      "tax_issued",
      "cash_issued",
      "processed",
    )

    meta_s = inv_row.get("settlement_meta")

    if not meta_s:
      # Settlement if not available Issuing business profile 100%
      bp_target = inv_row.get("business_profile_id")
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    try:
      parsed = json.loads(meta_s)
    except Exception:
      parsed = None
    if not isinstance(parsed, list) or is_default_settlement_split(
      parsed,
      inv_row.get("business_profile_id"),
    ):
      bp_target = inv_row["business_profile_id"]
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    pct_sum = 0.0
    records = []
    for rec in parsed:
      try:
        bpv = int(rec.get("business_profile_id"))
        pctv = float(rec.get("percent"))
      except Exception:
        continue
      if pctv <= 0:
        continue
      records.append((bpv, pctv))
      pct_sum += pctv

    if not records or pct_sum <= 0:
      bp_target = inv_row["business_profile_id"]
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    for bpv, pctv in records:
      share = service_amount * (pctv / pct_sum)
      # Settlement business profile Currency Filters  , USD Invoice Currency 
      yield (bpv, currency, share, is_outstanding, is_estimated)

  def _settlement_display_amounts(inv_row, target_bp: int) -> dict[str, float]:
    """Return the selected business profile's proportional display amounts for a row."""
    inv_row = row_to_dict(inv_row)
    service_base = _service_amount_for_invoice(inv_row)
    service_share = 0.0

    for bpv, _cur_code, amount, _is_outstanding, _is_estimated in _iter_settlement_shares(
      inv_row
    ):
      try:
        if int(bpv) != int(target_bp):
          continue
      except Exception:
        continue
      try:
        service_share += float(amount or 0.0)
      except (TypeError, ValueError):
        continue

    if service_share <= 0:
      return {
        "service_total": 0.0,
        "admin_total": 0.0,
        "foreign_total": 0.0,
        "tax": 0.0,
        "total": 0.0,
      }

    ratio = service_share / service_base if service_base > 0 else 0.0
    ratio = max(0.0, min(1.0, ratio))
    admin_share = _admin_total_for_invoice(inv_row.get("id")) * ratio
    foreign_share = _foreign_total_for_invoice(inv_row.get("id")) * ratio
    try:
      tax_share = float(inv_row.get("tax") or 0.0) * ratio
    except (TypeError, ValueError):
      tax_share = 0.0

    return {
      "service_total": service_share,
      "admin_total": admin_share,
      "foreign_total": foreign_share,
      "tax": tax_share,
      "total": service_share + admin_share + foreign_share + tax_share,
    }

  # ------------------------- KPI (basis ) -------------------------
  total_by_currency = {}
  outstanding_by_currency = {}
  estimated_by_currency = {}

  if basis == "issued":
    # Issued : Invoice subtotal(Taxable amount)from Official fee/Foreign expense "Service Revenue" (Sales tax )
    for inv in base_invoices:
      inv_id = inv.get("id")
      if not inv_id:
        continue

      currency = (inv.get("currency") or "USD").upper()
      try:
        subtotal = float(inv.get("subtotal") or 0.0)
      except Exception:
        subtotal = 0.0

      amount = max(
        subtotal - _admin_total_for_invoice(inv_id) - _foreign_total_for_invoice(inv_id),
        0.0,
      )

      billing_status = inv.get("billing_status")
      payment_status = inv.get("payment_status")
      is_outstanding = billing_status == "sent" and payment_status in ("unpaid", "pending")
      is_estimated = payment_status == "paid" or billing_status in (
        "tax_issued",
        "cash_issued",
        "processed",
      )

      total_by_currency[currency] = total_by_currency.get(currency, 0.0) + amount
      if is_outstanding:
        outstanding_by_currency[currency] = (
          outstanding_by_currency.get(currency, 0.0) + amount
        )
      if is_estimated:
        estimated_by_currency[currency] = estimated_by_currency.get(currency, 0.0) + amount

  else:
    # Settlement : settlement_meta to Business profile  Currency Total 
    for inv in base_invoices:
      for (
        bpv,
        cur_code,
        amount,
        is_outstanding,
        is_estimated,
      ) in _iter_settlement_shares(inv):
        # Business profile Filters items ID : Settlement business profile 
        if bp_id_int is not None:
          try:
            if int(bpv) != bp_id_int:
              continue
          except Exception:
            continue
        # Currency Filters(C:USD ) base_invoices from  Filters

        total_by_currency[cur_code] = total_by_currency.get(cur_code, 0.0) + amount
        if is_outstanding:
          outstanding_by_currency[cur_code] = (
            outstanding_by_currency.get(cur_code, 0.0) + amount
          )
        if is_estimated:
          estimated_by_currency[cur_code] = (
            estimated_by_currency.get(cur_code, 0.0) + amount
          )

  # Existing  total_period / outstanding_total / estimated_revenue
  total_period = sum(total_by_currency.values())
  outstanding_total = sum(outstanding_by_currency.values())
  estimated_revenue = sum(estimated_by_currency.values())

  clients_count = cur.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
  # Period Invoice / Paid items (Issued to )
  if bp_id:
    invoice_count_period = cur.execute(
      f"SELECT COUNT(*) FROM invoices WHERE issue_date >= ? AND issue_date <= ?{where_bp}",
      params,
    ).fetchone()[0]
    paid_count_period = cur.execute(
      f"SELECT COUNT(*) FROM invoices WHERE payment_status='paid' AND issue_date >= ? AND issue_date <= ?{where_bp}",
      params,
    ).fetchone()[0]
  else:
    invoice_count_period = cur.execute(
      "SELECT COUNT(*) FROM invoices WHERE issue_date >= ? AND issue_date <= ?",
      (start_date, end_date),
    ).fetchone()[0]
    paid_count_period = cur.execute(
      "SELECT COUNT(*) FROM invoices WHERE payment_status='paid' AND issue_date >= ? AND issue_date <= ?",
      (start_date, end_date),
    ).fetchone()[0]

  # Rows per page, default 20 with 50/100 item options.
  try:
    per_page = int(request.args.get("per_page", 20))
  except Exception:
    per_page = 20
  per_page = min(max(per_page, 10), 200) # 10~200 

  # selected Period Invoice Display (Period Filters + items Apply)
  # basis='settlement'  Business profile : Settlement to Recent Invoice 
  invoices = []
  if basis == "settlement" and bp_id_int is not None:
    target_bp = bp_id_int

    # client_name  times
    try:
      _crows = cur.execute("SELECT id, name FROM clients").fetchall()
      client_name_map = {row_to_dict(r)["id"]: row_to_dict(r)["name"] for r in _crows}
    except Exception:
      client_name_map = {}

    # base_invoices  Period( Currency )to Filters Status 
    tmp = []
    for inv in base_invoices:
      # Business profile   Invoice Select
      display_amounts = _settlement_display_amounts(inv, target_bp)

      if display_amounts["service_total"] <= 0:
        continue

      tmp.append(
        {
          "id": inv["id"],
          "number": inv["number"],
          "internal_reference": inv["internal_reference"],
          "client_id": inv["client_id"],
          "client_name": client_name_map.get(inv["client_id"]),
          "issue_date": inv["issue_date"],
          "due_date": inv["due_date"],
          "currency": (inv["currency"] or "USD"),
          "status": inv["status"],
          # Settlement  Display Amount(Service Revenue  )
          "total": display_amounts["total"],
          "service_total": display_amounts["service_total"],
          "admin_total": display_amounts["admin_total"],
          "foreign_total": display_amounts["foreign_total"],
          "tax": display_amounts["tax"],
        }
      )

    # Issue date/ID to Sort per_page 
    tmp.sort(key=lambda r: (r.get("issue_date") or "", r["id"]), reverse=True)
    invoices = tmp[:per_page]

  else:
    #  : Existing Issued  
    if bp_currency_code:
      cur_code = bp_currency_code
      invoices = cur.execute(
        """SELECT invoices.*, clients.name AS client_name,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'service'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as service_total,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'admin'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as admin_total,
          (SELECT COALESCE(SUM(
            CASE
             WHEN COALESCE(fx_rate_used, 0) > 0 THEN
              (COALESCE(fx_fee,0) + COALESCE(fx_gov,0))
              * COALESCE(fx_rate_used, 0)
              * (1 + COALESCE(fx_markup,0)/100.0)
             ELSE
              (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          ), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'foreign'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as foreign_total
          FROM invoices JOIN clients ON clients.id=invoices.client_id
          WHERE invoices.issue_date >= ? AND invoices.issue_date <= ? AND invoices.currency = ?
          ORDER BY invoices.issue_date DESC, invoices.id DESC LIMIT ?""",
        (start_date, end_date, cur_code, per_page),
      ).fetchall()
    elif bp_id_int is not None:
      invoices = cur.execute(
        """SELECT invoices.*, clients.name AS client_name,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'service'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as service_total,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'admin'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as admin_total,
          (SELECT COALESCE(SUM(
            CASE
             WHEN COALESCE(fx_rate_used, 0) > 0 THEN
              (COALESCE(fx_fee,0) + COALESCE(fx_gov,0))
              * COALESCE(fx_rate_used, 0)
              * (1 + COALESCE(fx_markup,0)/100.0)
             ELSE
              (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          ), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'foreign'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as foreign_total
          FROM invoices JOIN clients ON clients.id=invoices.client_id
          WHERE invoices.issue_date >= ? AND invoices.issue_date <= ? AND invoices.business_profile_id = ?
          ORDER BY invoices.issue_date DESC, invoices.id DESC LIMIT ?""",
        (start_date, end_date, bp_id_int, per_page),
      ).fetchall()
    else:
      invoices = cur.execute(
        """SELECT invoices.*, clients.name AS client_name,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'service'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as service_total,
          (SELECT COALESCE(SUM(qty * unit_price * (1 - COALESCE(discount,0)/100.0)), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'admin'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as admin_total,
          (SELECT COALESCE(SUM(
            CASE
             WHEN COALESCE(fx_rate_used, 0) > 0 THEN
              (COALESCE(fx_fee,0) + COALESCE(fx_gov,0))
              * COALESCE(fx_rate_used, 0)
              * (1 + COALESCE(fx_markup,0)/100.0)
             ELSE
              (qty * unit_price * (1 - COALESCE(discount,0)/100.0))
            END
          ), 0)
          FROM line_items
          WHERE line_items.invoice_id = invoices.id
           AND line_items.item_type = 'foreign'
           AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)) as foreign_total
          FROM invoices JOIN clients ON clients.id=invoices.client_id
          WHERE invoices.issue_date >= ? AND invoices.issue_date <= ?
          ORDER BY invoices.issue_date DESC, invoices.id DESC LIMIT ?""",
        (start_date, end_date, per_page),
      ).fetchall()

  # Final pass: Ensure all numeric fields are float to prevent Template TypeError causing (float + Decimal)
  cleaned_invoices = []
  for row in invoices:
    inv = row_to_dict(row)
    for k in ("total", "subtotal", "tax", "service_total", "admin_total", "foreign_total"):
      try:
        inv[k] = float(inv.get(k) or 0.0)
      except (TypeError, ValueError):
        inv[k] = 0.0
    cleaned_invoices.append(inv)
  invoices = cleaned_invoices

  # ------------------------- Revenue (Currency) -------------------------
  # Period , Period days 
  delta_days = (end_date_dt - start_date_dt).days
  group_by = "month" if delta_days > 92 else "day"

  #  
  if group_by == "day":
    trend_labels = []
    cur_d = start_date_dt
    while cur_d <= end_date_dt:
      trend_labels.append(cur_d.isoformat())
      cur_d = cur_d + timedelta(days=1)
  else:
    trend_labels = []
    y1, m1 = start_date_dt.year, start_date_dt.month
    y2, m2 = end_date_dt.year, end_date_dt.month
    y, m = y1, m1
    while (y < y2) or (y == y2 and m <= m2):
      trend_labels.append(f"{y:04d}-{m:02d}")
      if m == 12:
        y += 1
        m = 1
      else:
        m += 1

  # Helper to build trend series by criteria
  def _build_series(include_status_filter: bool, *, cumulative: bool):
    # basis='settlement' : settlement_meta to from 
    if basis == "settlement":
      issued_map = {}
      estimated_map = {}

      # base_invoices  Period( Currency )to Filters Status
      for inv in base_invoices:
        issue = (inv["issue_date"] or "").strip()
        if not issue:
          continue
        if group_by == "day":
          bucket = issue
        else:
          bucket = issue[:7]
        if bucket not in trend_labels:
          continue

        for (
          bpv,
          cur_code,
          amount,
          is_outstanding,
          is_estimated,
        ) in _iter_settlement_shares(inv):
          # Business profile : Settlement business profile 
          if bp_id_int is not None:
            try:
              if int(bpv) != bp_id_int:
                continue
            except Exception:
              continue
          key = (bucket, cur_code)
          try:
            val = float(amount or 0.0)
          except Exception:
            val = 0.0
          issued_map[key] = issued_map.get(key, 0.0) + val
          if is_estimated:
            estimated_map[key] = estimated_map.get(key, 0.0) + val

      base_map = estimated_map if include_status_filter else issued_map

      currencies = {k[1] for k in base_map.keys()}
      if not currencies:
        try:
          default_curr = (bp_row["currency"] or "USD") if bp_row is not None else "USD"
        except Exception:
          default_curr = "USD"
        currencies = {default_curr}
      series = {c: [] for c in currencies}

      for b in trend_labels:
        for c in series.keys():
          v = base_map.get((b, c), 0.0)
          try:
            series[c].append(round(float(v or 0.0), 2))
          except Exception:
            series[c].append(0.0)

      if cumulative:
        for c, arr in series.items():
          running = 0.0
          for i, v in enumerate(arr):
            try:
              running += float(v or 0.0)
            except (TypeError, ValueError):
              running += 0.0
            arr[i] = round(running, 2)
      return series

    # basis='issued' : Invoice subtotal(Taxable amount)from Official fee/Foreign expense Service Revenue from  (Sales tax )
    issued_map = {}
    estimated_map = {}

    for inv in base_invoices:
      issue = (inv.get("issue_date") or "").strip()
      if not issue:
        continue

      if group_by == "day":
        bucket = issue
      else:
        bucket = issue[:7]
      if bucket not in trend_labels:
        continue

      inv_id = inv.get("id")
      if not inv_id:
        continue

      currency = (inv.get("currency") or "USD").upper()
      try:
        subtotal = float(inv.get("subtotal") or 0.0)
      except Exception:
        subtotal = 0.0

      amount = max(
        subtotal - _admin_total_for_invoice(inv_id) - _foreign_total_for_invoice(inv_id),
        0.0,
      )

      key = (bucket, currency)
      issued_map[key] = issued_map.get(key, 0.0) + float(amount or 0.0)

      billing_status = inv.get("billing_status")
      payment_status = inv.get("payment_status")
      is_estimated = payment_status == "paid" or billing_status in (
        "tax_issued",
        "cash_issued",
        "processed",
      )
      if is_estimated:
        estimated_map[key] = estimated_map.get(key, 0.0) + float(amount or 0.0)

    base_map = estimated_map if include_status_filter else issued_map

    currencies = {k[1] for k in base_map.keys()}
    if not currencies:
      try:
        default_curr = (bp_row["currency"] or "USD") if bp_row is not None else "USD"
      except Exception:
        default_curr = "USD"
      currencies = {default_curr}

    series = {c: [] for c in currencies}
    for b in trend_labels:
      for c in series.keys():
        v = base_map.get((b, c), 0.0)
        try:
          series[c].append(round(float(v or 0.0), 2))
        except Exception:
          series[c].append(0.0)

    if cumulative:
      for c, arr in series.items():
        running = 0.0
        for i, v in enumerate(arr):
          try:
            running += float(v or 0.0)
          except (TypeError, ValueError):
            running += 0.0
          arr[i] = round(running, 2)
    return series

  trend_series_estimated = _build_series(include_status_filter=True, cumulative=True)
  trend_series_issued = _build_series(include_status_filter=False, cumulative=True)
  trend_period_series_estimated = _build_series(include_status_filter=True, cumulative=False)
  trend_period_series_issued = _build_series(include_status_filter=False, cumulative=False)
  # Backward compatibility: initial selection
  trend_series = trend_series_estimated if metric == "estimated" else trend_series_issued

  conn.close()

  dashboard_endpoint = request.endpoint or "billing_invoices.core.dashboard"
  invoice_theme = request.endpoint != "business.index"
  trend_bucket_label = "days" if group_by == "day" else ""
  return render_template(
    "dashboard.html",
    bp=bp_row,
    all_profiles=all_profiles,
    currencies=currencies,
    period=period,
    period_label=period_label,
    start_date=start_date,
    end_date=end_date,
    metric=metric,
    basis=basis,
    total_period=total_period,
    outstanding_total=outstanding_total,
    estimated_revenue=estimated_revenue,
    total_by_currency=total_by_currency,
    outstanding_by_currency=outstanding_by_currency,
    estimated_by_currency=estimated_by_currency,
    clients_count=clients_count,
    invoices=invoices,
    per_page=per_page,
    trend_labels=trend_labels,
    trend_series=trend_series,
    trend_series_estimated=trend_series_estimated,
    trend_series_issued=trend_series_issued,
    trend_period_series_estimated=trend_period_series_estimated,
    trend_period_series_issued=trend_period_series_issued,
    trend_bucket_label=trend_bucket_label,
    invoice_count_period=int(invoice_count_period or 0),
    paid_count_period=int(paid_count_period or 0),
    dashboard_endpoint=dashboard_endpoint,
    invoice_theme=invoice_theme,
  )


@bp.get("/api/summary")
def api_summary():
  bp_id_raw = request.args.get("business_profile_id", "")
  period = request.args.get("period", "1month").strip()
  metric = (request.args.get("metric") or "estimated").strip().lower()
  if metric not in ("estimated", "issued"):
    metric = "estimated"
  basis = (request.args.get("basis") or "settlement").strip().lower()
  if basis not in ("settlement", "issued"):
    basis = "settlement"

  try:
    all_profiles = get_all_business_profiles()
    currencies = sorted({((p["currency"] or "USD").upper()) for p in all_profiles})
  except Exception:
    currencies = ["USD"]

  bp_id, bp_id_int, bp_currency_code = _parse_business_profile_filter(
    bp_id_raw, allowed_currencies=set(currencies) if currencies else None
  )

  today = date.today()
  y = today.year
  start_date_dt = today.replace(day=1)
  end_date_dt = today
  period_label = "This month"

  if period == "3months":
    start_date_dt = today - timedelta(days=90)
    period_label = "Last 3 months"
  elif period == "6months":
    start_date_dt = today - timedelta(days=180)
    period_label = "Last 6 months"
  elif period == "12months":
    start_date_dt = today - timedelta(days=365)
    period_label = "Last 12 months"
  elif period == "Q1":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 3, 31)
    period_label = f"{y} 1quarter"
  elif period == "Q2":
    start_date_dt = date(y, 4, 1)
    end_date_dt = date(y, 6, 30)
    period_label = f"{y} 2quarter"
  elif period == "Q3":
    start_date_dt = date(y, 7, 1)
    end_date_dt = date(y, 9, 30)
    period_label = f"{y} 3quarter"
  elif period == "Q4":
    start_date_dt = date(y, 10, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} 4quarter"
  elif period == "H1":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 6, 30)
    period_label = f"{y} first half"
  elif period == "H2":
    start_date_dt = date(y, 7, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} second half"
  elif period == "1year":
    start_date_dt = date(y, 1, 1)
    end_date_dt = date(y, 12, 31)
    period_label = f"{y} All"

  start_arg = (request.args.get("start_date") or "").strip()
  end_arg = (request.args.get("end_date") or "").strip()
  if start_arg and end_arg:
    try:
      sdt = date.fromisoformat(start_arg)
      edt = date.fromisoformat(end_arg)
      if edt < sdt:
        sdt, edt = edt, sdt
      start_date_dt = sdt
      end_date_dt = edt
      period_label = f"{sdt.isoformat()} ~ {edt.isoformat()}"
    except ValueError:
      pass

  start_date = start_date_dt.isoformat()
  end_date = end_date_dt.isoformat()

  conn = get_db()
  cur = conn.cursor()

  if basis == "settlement":
    if bp_currency_code:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND currency = ?""",
        (start_date, end_date, bp_currency_code),
      ).fetchall()
    else:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ?""",
        (start_date, end_date),
      ).fetchall()
  else:
    if bp_currency_code:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND currency = ?""",
        (start_date, end_date, bp_currency_code),
      ).fetchall()
    elif bp_id_int is not None:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ? AND business_profile_id = ?""",
        (start_date, end_date, bp_id_int),
      ).fetchall()
    else:
      base_invoices = cur.execute(
        """SELECT * FROM invoices
          WHERE issue_date >= ? AND issue_date <= ?""",
        (start_date, end_date),
      ).fetchall()

  base_invoices = [row_to_dict(row) for row in base_invoices]

  line_where = ""
  line_params = [start_date, end_date]
  if bp_currency_code:
    line_where = " AND invoices.currency = ?"
    line_params.append(bp_currency_code)
  elif basis != "settlement" and bp_id_int is not None:
    line_where = " AND invoices.business_profile_id = ?"
    line_params.append(bp_id_int)

  _totals_by_invoice = {}
  try:
    rows = cur.execute(
      f"""
      SELECT line_items.invoice_id AS invoice_id,
          COALESCE(SUM(
            CASE
             WHEN line_items.item_type = 'admin'
             AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
             THEN (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
             ELSE 0
            END
          ), 0) AS admin_total,
          COALESCE(SUM(
            CASE
             WHEN line_items.item_type = 'foreign'
             AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
             THEN
              CASE
               WHEN COALESCE(line_items.fx_rate_used, 0) > 0 THEN
                (COALESCE(line_items.fx_fee,0) + COALESCE(line_items.fx_gov,0))
                * COALESCE(line_items.fx_rate_used, 0)
                * (1 + COALESCE(line_items.fx_markup,0)/100.0)
               ELSE
                (line_items.qty * line_items.unit_price * (1 - COALESCE(line_items.discount,0)/100.0))
              END
             ELSE 0
            END
          ), 0) AS foreign_total
       FROM line_items
       JOIN invoices ON invoices.id = line_items.invoice_id
       WHERE invoices.issue_date >= ? AND invoices.issue_date <= ?{line_where}
       GROUP BY line_items.invoice_id
      """,
      line_params,
    ).fetchall()
    for r in rows:
      try:
        iid = int(r["invoice_id"])
      except Exception:
        continue
      _totals_by_invoice[iid] = {
        "admin_total": float(r["admin_total"] or 0.0),
        "foreign_total": float(r["foreign_total"] or 0.0),
      }
  except Exception:
    _totals_by_invoice = {}

  def _admin_total_for_invoice(invoice_id: int) -> float:
    try:
      return float(
        (_totals_by_invoice.get(int(invoice_id)) or {}).get("admin_total", 0.0) or 0.0
      )
    except Exception:
      return 0.0

  def _foreign_total_for_invoice(invoice_id: int) -> float:
    try:
      return float(
        (_totals_by_invoice.get(int(invoice_id)) or {}).get("foreign_total", 0.0) or 0.0
      )
    except Exception:
      return 0.0

  def _service_amount_for_invoice(inv_row):
    inv_row = row_to_dict(inv_row)
    admin_total = _admin_total_for_invoice(inv_row.get("id"))
    foreign_total = _foreign_total_for_invoice(inv_row.get("id"))
    try:
      inv_subtotal = float(inv_row.get("subtotal") or 0.0)
    except Exception:
      inv_subtotal = 0.0
    return max(inv_subtotal - admin_total - foreign_total, 0.0)

  def _iter_issued_shares(inv_row):
    inv_row = row_to_dict(inv_row)
    service_amount = _service_amount_for_invoice(inv_row)
    billing_status = inv_row.get("billing_status")
    payment_status = inv_row.get("payment_status")
    currency = _normalize_currency_code(inv_row.get("currency"))
    is_outstanding = billing_status == "sent" and payment_status in (
      "unpaid",
      "pending",
    )
    is_estimated = payment_status == "paid" or billing_status in (
      "tax_issued",
      "cash_issued",
      "processed",
    )
    yield (currency, service_amount, is_outstanding, is_estimated)

  def _iter_settlement_shares(inv_row):
    inv_row = row_to_dict(inv_row)
    service_amount = _service_amount_for_invoice(inv_row)
    billing_status = inv_row.get("billing_status")
    payment_status = inv_row.get("payment_status")
    currency = _normalize_currency_code(inv_row.get("currency"))
    is_outstanding = billing_status == "sent" and payment_status in (
      "unpaid",
      "pending",
    )
    is_estimated = payment_status == "paid" or billing_status in (
      "tax_issued",
      "cash_issued",
      "processed",
    )

    meta_s = inv_row.get("settlement_meta")
    if not meta_s:
      bp_target = inv_row.get("business_profile_id")
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    try:
      parsed = json.loads(meta_s)
    except Exception:
      parsed = None
    if not isinstance(parsed, list) or is_default_settlement_split(
      parsed,
      inv_row.get("business_profile_id"),
    ):
      bp_target = inv_row.get("business_profile_id")
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    pct_sum = 0.0
    records = []
    for rec in parsed:
      try:
        bpv = int(rec.get("business_profile_id"))
        pctv = float(rec.get("percent"))
      except Exception:
        continue
      if pctv <= 0:
        continue
      records.append((bpv, pctv))
      pct_sum += pctv

    if not records or pct_sum <= 0:
      bp_target = inv_row.get("business_profile_id")
      if not bp_target:
        return
      yield (bp_target, currency, service_amount, is_outstanding, is_estimated)
      return

    for bpv, pctv in records:
      share = service_amount * (pctv / pct_sum)
      yield (bpv, currency, share, is_outstanding, is_estimated)

  total_by_currency = {}
  outstanding_by_currency = {}
  estimated_by_currency = {}

  if basis == "issued":
    for inv in base_invoices:
      for cur_code, amount, is_outstanding, is_estimated in _iter_issued_shares(inv):
        total_by_currency[cur_code] = total_by_currency.get(cur_code, 0.0) + amount
        if is_outstanding:
          outstanding_by_currency[cur_code] = (
            outstanding_by_currency.get(cur_code, 0.0) + amount
          )
        if is_estimated:
          estimated_by_currency[cur_code] = (
            estimated_by_currency.get(cur_code, 0.0) + amount
          )
  else:
    for inv in base_invoices:
      for (
        bpv,
        cur_code,
        amount,
        is_outstanding,
        is_estimated,
      ) in _iter_settlement_shares(inv):
        if bp_id_int is not None:
          try:
            if int(bpv) != bp_id_int:
              continue
          except Exception:
            continue
        total_by_currency[cur_code] = total_by_currency.get(cur_code, 0.0) + amount
        if is_outstanding:
          outstanding_by_currency[cur_code] = (
            outstanding_by_currency.get(cur_code, 0.0) + amount
          )
        if is_estimated:
          estimated_by_currency[cur_code] = (
            estimated_by_currency.get(cur_code, 0.0) + amount
          )

  count_where = ""
  count_params = [start_date, end_date]
  if bp_currency_code:
    count_where = " AND currency = ?"
    count_params.append(bp_currency_code)
  elif bp_id_int is not None:
    count_where = " AND business_profile_id = ?"
    count_params.append(bp_id_int)

  issued_counts = {}
  try:
    rows = cur.execute(
      f"""SELECT currency, COUNT(*) as cnt
        FROM invoices
        WHERE issue_date >= ? AND issue_date <= ?{count_where}
        GROUP BY currency""",
      count_params,
    ).fetchall()
    for row in rows:
      r = row_to_dict(row)
      issued_counts[_normalize_currency_code(r.get("currency"))] = int(r["cnt"] or 0)
  except Exception:
    issued_counts = {}

  outstanding_counts = {}
  try:
    rows = cur.execute(
      f"""SELECT currency, COUNT(*) as cnt
        FROM invoices
        WHERE (billing_status='sent' AND payment_status IN ('unpaid','pending'))
         AND issue_date >= ? AND issue_date <= ?{count_where}
        GROUP BY currency""",
      count_params,
    ).fetchall()
    for row in rows:
      r = row_to_dict(row)
      outstanding_counts[_normalize_currency_code(r.get("currency"))] = int(r["cnt"] or 0)
  except Exception:
    outstanding_counts = {}

  def _parse_ymd(s):
    try:
      return date.fromisoformat(s) if s else None
    except Exception:
      return None

  def _parse_deposit(meta, cur_code):
    if not meta:
      return 0.0
    try:
      if isinstance(meta, str):
        meta = json.loads(meta)
    except Exception:
      return 0.0
    if not isinstance(meta, dict):
      return 0.0
    dep_str = (meta.get("deposit") if meta else "") or "0"
    s = str(dep_str).replace(",", "").strip()
    try:
      if cur_code in ("USD", "JPY"):
        return float(int(s or "0"))
      return float(s or "0")
    except Exception:
      return 0.0

  as_of_date = end_date_dt if (start_arg and end_arg) else today
  ar_params = []
  ar_where = ["(payment_status IN ('unpaid','pending') OR billing_status = 'pre_overdue')"]
  if bp_currency_code:
    ar_where.append("UPPER(currency) = ?")
    ar_params.append(bp_currency_code)
  elif bp_id_int is not None:
    ar_where.append("business_profile_id = ?")
    ar_params.append(bp_id_int)
  ar_where_sql = " WHERE " + " AND ".join(ar_where)
  ar_rows = cur.execute(
    f"""SELECT id, issue_date, due_date, total, currency, billing_status,
          payment_status, payment_meta
      FROM invoices
      {ar_where_sql}""",
    ar_params,
  ).fetchall()

  buckets_by_currency = {}

  def _ensure_bucket(cur_code):
    if cur_code not in buckets_by_currency:
      buckets_by_currency[cur_code] = {
        "current": 0.0,
        "d1_30": 0.0,
        "d31_60": 0.0,
        "d61_90": 0.0,
        "d91_plus": 0.0,
        "total": 0.0,
      }

  for row in ar_rows:
    r = row_to_dict(row)
    cur_code = _normalize_currency_code(r.get("currency"))
    try:
      total_amt = float(r.get("total") or 0.0)
    except Exception:
      total_amt = 0.0
    dep = _parse_deposit(r.get("payment_meta"), cur_code)
    amt = max(0.0, total_amt - float(dep or 0.0))
    base_date = _parse_ymd(r.get("due_date")) or _parse_ymd(r.get("issue_date")) or as_of_date
    days_over = (as_of_date - base_date).days
    if (r.get("billing_status") or "").lower() == "pre_overdue" and days_over <= 0:
      days_over = 1
    bucket = (
      "current"
      if days_over <= 0
      else (
        "d1_30"
        if days_over <= 30
        else (
          "d31_60" if days_over <= 60 else ("d61_90" if days_over <= 90 else "d91_plus")
        )
      )
    )
    _ensure_bucket(cur_code)
    buckets_by_currency[cur_code][bucket] += amt
    buckets_by_currency[cur_code]["total"] += amt

  outstanding_total_by_currency = {
    cur_code: float(buckets.get("total") or 0.0)
    for cur_code, buckets in buckets_by_currency.items()
  }

  dash_args = {
    "period": period,
    "metric": metric,
    "basis": basis,
  }
  if bp_id:
    dash_args["business_profile_id"] = bp_id
  if start_arg and end_arg:
    dash_args["start_date"] = start_arg
    dash_args["end_date"] = end_arg

  invoices_args = {
    "status": "sent_unpaid_or_pending",
    "basis": basis,
  }
  if bp_id:
    invoices_args["business_profile_id"] = bp_id
  tax_issue_args = {
    "status": "paid_no_tax",
    "basis": basis,
  }
  if bp_id:
    tax_issue_args["business_profile_id"] = bp_id

  aging_args = {"as_of": as_of_date.isoformat()}
  if bp_id and not bp_id.upper().startswith("C:"):
    bp_id_str = str(bp_id).strip()
    if bp_id_str.isdigit():
      aging_args["business_profile_id"] = int(bp_id_str)

  payload = {
    "ok": True,
    "value": {
      "range": {
        "start_date": start_date,
        "end_date": end_date,
        "label": period_label,
      },
      "filters": {
        "business_profile_id": bp_id or "",
        "period": period,
        "basis": basis,
        "metric": metric,
      },
      "revenue": {
        "service_revenue_by_currency": total_by_currency,
        "estimated_service_revenue_by_currency": estimated_by_currency,
        "outstanding_service_revenue_by_currency": outstanding_by_currency,
      },
      "ar": {
        "outstanding_total_by_currency": outstanding_total_by_currency,
        "buckets_by_currency": buckets_by_currency,
      },
      "counts": {
        "issued_invoices_by_currency": issued_counts,
        "outstanding_invoices_by_currency": outstanding_counts,
      },
      "links": {
        "invoice_dashboard": url_for("billing_invoices.core.dashboard", **dash_args),
        "invoices_outstanding": url_for(
          "billing_invoices.invoices.list_invoices", **invoices_args
        ),
        "tax_issue": url_for("billing_invoices.invoices.list_invoices", **tax_issue_args),
        "aging": url_for("billing_invoices.aging.aging_report", **aging_args),
      },
    },
  }

  try:
    conn.close()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.core.dashboard_data.close_conn",
      log_key="billing_invoices.core.dashboard_data.close_conn",
      log_window_seconds=300,
    )

  return jsonify(payload)


@bp.route("/lang/<lang_code>")
@role_required("staff")
def switch_language(lang_code):
  if lang_code in ["en"]:
    session["lang"] = lang_code
  return redirect(safe_referrer_path(request) or url_for("billing_invoices.core.dashboard"))


@bp.route("/settings", methods=["GET", "POST"])
@role_required("staff")
def settings():
  from app.services.billing.utils import save_logo

  profile_id = request.args.get("profile_id") or request.form.get("profile_id")
  all_profiles = get_all_business_profiles()
  if profile_id and str(profile_id).isdigit():
    bp_row = get_business_profile(int(profile_id))
  else:
    bp_row = get_business_profile()

  if request.method == "POST":
    conn = get_db()
    logo_path = bp_row["logo_path"]
    if "logo_file" in request.files:
      try:
        logo_path = save_logo(request.files["logo_file"], logo_path)
      except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect(url_for("billing_invoices.core.settings", profile_id=bp_row["id"]))

    conn.execute(
      """UPDATE business_profile
        SET name=?, address=?, email=?, phone=?, tax_id=?, currency=?, vat_rate=?, next_invoice_no=?, logo_path=?, bank_account=?
        WHERE id=?""",
      (
        request.form.get("name"),
        request.form.get("address"),
        request.form.get("email"),
        request.form.get("phone"),
        request.form.get("tax_id"),
        _normalize_currency_code(request.form.get("currency")),
        float(request.form.get("vat_rate") or 0),
        int(request.form.get("next_invoice_no") or 1),
        logo_path,
        request.form.get("bank_account"),
        bp_row["id"],
      ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("billing_invoices.core.settings", profile_id=bp_row["id"]))

  return render_template("settings.html", bp=bp_row, all_profiles=all_profiles)


@bp.route("/uploads/<filename>")
@role_required("staff")
def uploaded_file(filename):
  safe_name = os.path.basename(str(filename or ""))
  if not safe_name or safe_name != filename:
    abort(404)

  try:
    from app.services.billing.utils import allowed_file

    if not allowed_file(safe_name):
      abort(404)
  except Exception:
    abort(404)

  logo_prefix = "/accounting/invoice-system/uploads/"
  legacy_prefix = "/uploads/"
  conn = get_db()
  try:
    row = conn.execute(
      """
      SELECT 1
      FROM business_profile
      WHERE logo_path = ? OR logo_path = ? OR logo_path = ?
      LIMIT 1
      """,
      (
        f"{logo_prefix}{safe_name}",
        f"{legacy_prefix}{safe_name}",
        safe_name,
      ),
    ).fetchone()
  except Exception:
    current_app.logger.warning(
      "Logo access check failed (filename=%s)", safe_name, exc_info=True
    )
    abort(404)
  finally:
    try:
      conn.close()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.core.logo_access_check.close_conn",
        log_key="billing_invoices.core.logo_access_check.close_conn",
        log_window_seconds=300,
      )

  if not row:
    abort(404)

  upload_dir = current_app.config.get("UPLOAD_FOLDER")
  if not upload_dir:
    abort(404)
  return send_from_directory(upload_dir, safe_name)


@bp.route("/robots.txt")
def robots_txt():
  return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@bp.route("/healthz")
def healthcheck():
  """
   

   Link,   Status Confirm.
   from  exists.
  """
  from datetime import datetime

  allowed, reason = check_admin_or_internal_access()
  if not allowed:
    message = "forbidden"
    if reason == "blocked_country":
      message = "blocked_country"
    return jsonify({"status": "forbidden", "error": message}), 403

  health_status = {
    "status": "healthy",
    "timestamp": datetime.utcnow().isoformat() + "Z",
    "version": "1.0.0",
    "checks": {},
  }

  # 1. Link Confirm
  try:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    result = cur.fetchone()
    conn.close()

    if result and result[0] == 1:
      health_status["checks"]["database"] = {
        "status": "healthy",
        "message": "Database connection OK",
      }
    else:
      raise Exception("Invalid query result")

  except Exception as e:
    health_status["status"] = "unhealthy"
    health_status["checks"]["database"] = {
      "status": "unhealthy",
      "message": f"Database error: {str(e)}",
    }

  # 2.  Confirm
  try:
    conn = get_db()
    cur = conn.cursor()
    required_tables = [
      "invoices",
      "clients",
      "business_profile",
      "audit_log",
    ]

    from app.blueprints.billing_invoices.db import _table_exists

    missing_tables = [t for t in required_tables if not _table_exists(conn, t)]
    conn.close()

    if not missing_tables:
      health_status["checks"]["schema"] = {
        "status": "healthy",
        "message": "All required tables exist",
      }
    else:
      health_status["status"] = "degraded"
      health_status["checks"]["schema"] = {
        "status": "degraded",
        "message": f"Missing tables: {', '.join(missing_tables)}",
      }

  except Exception as e:
    health_status["status"] = "unhealthy"
    health_status["checks"]["schema"] = {
      "status": "unhealthy",
      "message": f"Schema check error: {str(e)}",
    }

  # 3. deadline_engine Confirm (/Deadline )
  try:
    from deadline_engine.annuities import compute_annual_fee_deadlines # noqa: F401

    health_status["checks"]["deadline_engine"] = {
      "status": "healthy",
      "message": "deadline_engine import OK",
    }
  except Exception as e:
    try:
      from app.utils.vendor_paths import ensure_deadline_engine_path

      ensure_deadline_engine_path(current_app.config.get("BASE_DIR"))
      from deadline_engine.annuities import compute_annual_fee_deadlines # noqa: F401

      health_status["checks"]["deadline_engine"] = {
        "status": "healthy",
        "message": "deadline_engine import OK (vendored)",
      }
    except Exception:
      health_status["status"] = "unhealthy"
      health_status["checks"]["deadline_engine"] = {
        "status": "unhealthy",
        "message": f"deadline_engine import failed: {str(e)}",
      }

  # 4.  Confirm
  try:
    import shutil

    total, used, free = shutil.disk_usage(current_app.config["BASE_DIR"])
    free_gb = free // (2**30)
    free_percent = (free / total) * 100

    if free_percent < 10:
      health_status["status"] = "degraded"
      health_status["checks"]["disk"] = {
        "status": "warning",
        "message": f"Low disk space: {free_gb}GB ({free_percent:.1f}%) free",
      }
    else:
      health_status["checks"]["disk"] = {
        "status": "healthy",
        "message": f"Disk space OK: {free_gb}GB ({free_percent:.1f}%) free",
      }

  except Exception as e:
    health_status["checks"]["disk"] = {
      "status": "unknown",
      "message": f"Disk check error: {str(e)}",
    }

  # 5. Upload Confirm
  try:
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    if os.path.exists(upload_dir) and os.access(upload_dir, os.W_OK):
      health_status["checks"]["uploads"] = {
        "status": "healthy",
        "message": "Upload directory accessible",
      }
    else:
      health_status["status"] = "degraded"
      health_status["checks"]["uploads"] = {
        "status": "warning",
        "message": "Upload directory not accessible",
      }
  except Exception as e:
    health_status["checks"]["uploads"] = {
      "status": "unknown",
      "message": f"Upload check error: {str(e)}",
    }

  # HTTP Status 
  status_code = 200
  if health_status["status"] == "unhealthy":
    status_code = 503
  elif health_status["status"] == "degraded":
    status_code = 200 #  200 

  from flask import jsonify

  return jsonify(health_status), status_code
