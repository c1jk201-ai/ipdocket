from __future__ import annotations

from typing import List

from app.services.billing.utils import invoice_case_link_filter_sql, sql_ci_contains_any
from app.utils.error_logging import report_swallowed_exception

from ..db import get_db, row_to_dict


def fetch_aging_invoices_rows(
  *,
  bp_ids: list[int],
  q: str,
  is_compact_q: bool,
  case_linked: str,
) -> List[dict]:
  conn = get_db()
  params: list = []
  where = [
    "(invoices.payment_status IN ('unpaid','pending') OR invoices.billing_status = 'pre_overdue')"
  ]

  if bp_ids:
    placeholders = ",".join(["?"] * len(bp_ids))
    where.append(f"invoices.business_profile_id IN ({placeholders})")
    params.extend(bp_ids)
  if q and not is_compact_q:
    search_clause, search_params = sql_ci_contains_any(["clients.name", "invoices.number"], q)
    if search_clause:
      where.append(search_clause)
      params.extend(search_params)
  case_link_clause = invoice_case_link_filter_sql(case_linked, "invoices")
  if case_link_clause:
    where.append(case_link_clause)

  where_sql = " WHERE " + " AND ".join(where)

  sql = f"""
   SELECT invoices.id, invoices.client_id, invoices.number, invoices.issue_date, invoices.due_date,
       invoices.total, invoices.tax, invoices.currency, invoices.status,
       invoices.payment_meta, invoices.tax_issued_at,
       clients.name AS client_name,
       (
        SELECT COALESCE(SUM(qty * unit_price * (1 - discount/100.0)), 0)
        FROM line_items
        WHERE line_items.invoice_id = invoices.id
         AND line_items.item_type = 'service'
         AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
       ) AS service_total,
       (
        SELECT COALESCE(SUM(qty * unit_price * (1 - discount/100.0)), 0)
        FROM line_items
        WHERE line_items.invoice_id = invoices.id
         AND line_items.item_type = 'admin'
         AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
       ) AS admin_total,
       (
        SELECT COALESCE(SUM(qty * unit_price * (1 - discount/100.0)), 0)
        FROM line_items
        WHERE line_items.invoice_id = invoices.id
         AND line_items.item_type = 'foreign'
         AND (line_items.is_estimated IS NULL OR line_items.is_estimated = 0)
       ) AS foreign_total
   FROM invoices
   JOIN clients ON clients.id = invoices.client_id
   {where_sql}
   ORDER BY invoices.issue_date DESC, invoices.id DESC
  """

  try:
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]
  finally:
    try:
      conn.close()
    except Exception as exc:
      report_swallowed_exception(
        exc,
        context="billing_invoices.aging_invoices_repo.fetch_aging_invoices_rows.close",
        log_key="billing_invoices.aging_invoices_repo.fetch_aging_invoices_rows.close",
        log_window_seconds=300,
      )
