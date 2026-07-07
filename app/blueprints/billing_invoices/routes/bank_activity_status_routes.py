from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import abort, current_app, jsonify, request

from app.utils.coercion import coerce_int
from app.utils.error_logging import report_swallowed_exception

from ..db import DB_ERRORS, _get_column_names, get_db
from .bank_activity import (
  _app_tz,
  _compute_effective_tax_invoice,
  _extract_invoice_number_from_memo,
  _fetch_unique_invoice_row_by_number,
  _fetch_unique_invoice_row_by_payment_tid,
  _format_kst_timestamp,
  _resolve_unique_invoice_row_for_transaction,
  _transaction_date_expr,
  bp,
)

_DB_VALUE_ERRORS = DB_ERRORS + (TypeError, ValueError)
_DB_RUNTIME_ERRORS = DB_ERRORS + (RuntimeError,)


@bp.get("/coverage")
def coverage():
  accounts_param = (request.args.get("accounts") or "").strip()
  acct_pairs: list[tuple[str, str]] = []
  if accounts_param:
    for tok in accounts_param.split(","):
      tok = tok.strip()
      if not tok:
        continue
      if "|" in tok:
        bank_code, account_number = tok.split("|", 1)
        acct_pairs.append((bank_code.strip(), account_number.strip()))
  if not acct_pairs:
    abort(400, "accounts is required")

  conn = get_db()
  pair_ors = []
  params: list[str] = []
  for bank_code, account_number in acct_pairs:
    pair_ors.append("(bank_code=? AND account_number=?)")
    params += [bank_code, account_number]
  where_accounts = "(" + " OR ".join(pair_ors) + ")"

  date_expr = _transaction_date_expr(conn)
  row = conn.execute(
    f"SELECT MIN({date_expr}), MAX({date_expr}) FROM bank_transactions WHERE {where_accounts}",
    params,
  ).fetchone()
  min_trdate = row[0] if row else None
  max_trdate = row[1] if row else None

  tx_row = conn.execute(
    f"SELECT MAX(updated_at) FROM bank_transactions WHERE {where_accounts}",
    params,
  ).fetchone()
  db_max_updated_at = tx_row[0] if tx_row else None
  db_max_updated_at_kst = _format_kst_timestamp(db_max_updated_at)

  last_job = None
  last_job_tx = None
  jrow = conn.execute(
    f"""
    SELECT bank_code, account_number, sdate, edate, job_id, job_state, error_code,
        error_reason, job_start_dt, job_end_dt, reg_dt, created_at, updated_at
    FROM bank_import_jobs
    WHERE {where_accounts}
    ORDER BY COALESCE(updated_at, created_at) DESC
    LIMIT 1
    """,
    params,
  ).fetchone()
  if jrow:
    last_job = {
      "bankCode": jrow[0],
      "accountNumber": jrow[1],
      "sdate": jrow[2],
      "edate": jrow[3],
      "jobId": jrow[4],
      "jobState": jrow[5],
      "errorCode": jrow[6],
      "errorReason": jrow[7],
      "jobStartDT": jrow[8],
      "jobEndDT": jrow[9],
      "regDT": jrow[10],
      "createdAt": jrow[11],
      "createdAtKst": _format_kst_timestamp(jrow[11]),
      "updatedAt": jrow[12],
      "updatedAtKst": _format_kst_timestamp(jrow[12]),
    }
    last_job_tx = _load_job_transaction_summary(conn, date_expr, jrow)
  else:
    last_job = _infer_last_job_from_transactions(conn, date_expr, where_accounts, params)

  return jsonify(
    {
      "dbMinTrdate": min_trdate,
      "dbMaxTrdate": max_trdate,
      "dbMaxUpdatedAt": db_max_updated_at,
      "dbMaxUpdatedAtKst": db_max_updated_at_kst,
      "lastJob": last_job,
      "lastJobTx": last_job_tx,
      "accountStatuses": _load_account_statuses(conn, acct_pairs, date_expr),
    }
  )


def _load_job_transaction_summary(conn, date_expr: str, job_row) -> dict[str, Any] | None:
  try:
    if job_row[4]:
      row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt,
            MAX({date_expr}) AS max_trdate,
            MAX(trdt) AS max_trdt,
            MAX(updated_at) AS max_updated
        FROM bank_transactions
        WHERE bank_code=? AND account_number=? AND job_id=?
        """,
        [job_row[0], job_row[1], job_row[4]],
      ).fetchone()
    else:
      row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt,
            MAX({date_expr}) AS max_trdate,
            MAX(trdt) AS max_trdt,
            MAX(updated_at) AS max_updated
        FROM bank_transactions
        WHERE bank_code=? AND account_number=?
         AND {date_expr} >= ? AND {date_expr} <= ?
        """,
        [job_row[0], job_row[1], job_row[2], job_row[3]],
      ).fetchone()
    if not row:
      return None
    return {
      "count": int(row[0] or 0),
      "maxTrdate": row[1],
      "maxTrdt": row[2],
      "maxUpdatedAt": row[3],
      "maxUpdatedAtKst": _format_kst_timestamp(row[3]),
    }
  except _DB_VALUE_ERRORS:
    return None


def _infer_last_job_from_transactions(conn, date_expr: str, where_accounts: str, params: list[str]):
  try:
    row = conn.execute(
      f"""
      SELECT
       MAX(updated_at) AS max_updated,
       MAX({date_expr}) AS max_trdate,
       MIN({date_expr}) AS min_trdate
      FROM bank_transactions
      WHERE {where_accounts}
      """,
      params,
    ).fetchone()
    if not row or not (row[0] or row[1]):
      return None
    return {
      "sdate": row[2] or row[1],
      "edate": row[1] or row[2],
      "jobId": None,
      "jobState": None,
      "errorCode": None,
      "errorReason": None,
      "jobStartDT": None,
      "jobEndDT": None,
      "regDT": None,
      "updatedAt": row[0],
      "updatedAtKst": _format_kst_timestamp(row[0]),
    }
  except DB_ERRORS as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.bank_activity.health.last_job",
      log_key="billing_invoices.bank_activity.health.last_job",
      log_window_seconds=300,
    )
    return None


def _load_account_statuses(
  conn,
  acct_pairs: list[tuple[str, str]],
  date_expr: str,
) -> list[dict[str, Any]]:
  statuses = []
  for bank_code, account_number in acct_pairs:
    try:
      job_row = conn.execute(
        """
        SELECT sdate, edate, job_id, job_state, error_code, error_reason,
            job_start_dt, job_end_dt, reg_dt, created_at, updated_at
        FROM bank_import_jobs
        WHERE bank_code=? AND account_number=?
        ORDER BY COALESCE(updated_at, created_at) DESC
        LIMIT 1
        """,
        [bank_code, account_number],
      ).fetchone()
      job = None
      job_tx = None
      if job_row:
        job = {
          "sdate": job_row[0],
          "edate": job_row[1],
          "jobId": job_row[2],
          "jobState": job_row[3],
          "errorCode": job_row[4],
          "errorReason": job_row[5],
          "jobStartDT": job_row[6],
          "jobEndDT": job_row[7],
          "regDT": job_row[8],
          "createdAt": job_row[9],
          "updatedAt": job_row[10],
          "updatedAtKst": _format_kst_timestamp(job_row[10]),
        }
        job_tx = _load_account_job_transaction_summary(
          conn, date_expr, bank_code, account_number, job_row
        )
      statuses.append(
        {
          "bankCode": bank_code,
          "accountNumber": account_number,
          "lastJob": job,
          "lastJobTx": job_tx,
        }
      )
    except DB_ERRORS:
      statuses.append(
        {
          "bankCode": bank_code,
          "accountNumber": account_number,
          "lastJob": None,
          "lastJobTx": None,
        }
      )
  return statuses


def _load_account_job_transaction_summary(
  conn,
  date_expr: str,
  bank_code: str,
  account_number: str,
  job_row,
) -> dict[str, Any] | None:
  try:
    if job_row[2]:
      row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt,
            MAX({date_expr}) AS max_trdate,
            MAX(trdt) AS max_trdt
        FROM bank_transactions
        WHERE bank_code=? AND account_number=? AND job_id=?
        """,
        [bank_code, account_number, job_row[2]],
      ).fetchone()
    else:
      row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt,
            MAX({date_expr}) AS max_trdate,
            MAX(trdt) AS max_trdt
        FROM bank_transactions
        WHERE bank_code=? AND account_number=?
         AND {date_expr} >= ? AND {date_expr} <= ?
        """,
        [bank_code, account_number, job_row[0], job_row[1]],
      ).fetchone()
    if not row:
      return None
    return {"count": int(row[0] or 0), "maxTrdate": row[1], "maxTrdt": row[2]}
  except _DB_VALUE_ERRORS:
    return None


@bp.get("/scheduler_status")
def scheduler_status():
  try:
    from ..scheduler import get_scheduler_status

    return jsonify(get_scheduler_status())
  except _DB_RUNTIME_ERRORS as exc:
    return jsonify({"error": True, "message": str(exc)}), 500


@bp.post("/tax_invoice")
def set_tax_invoice_issued():
  data = request.get_json(silent=True) or {}
  tid = (data.get("tid") or "").strip()
  issued_raw = data.get("issued")
  if not tid:
    abort(400, "tid is required")

  issued = 1 if bool(issued_raw) else 0
  now_kst = datetime.now(_app_tz()).isoformat(timespec="seconds")
  invoice_number = None
  invoice_updated = False

  try:
    conn = get_db()
    _guard_tax_invoice_transaction_type(conn, tid=tid, issued=issued)
    _set_transaction_tax_invoice_override(conn, tid=tid, issued=issued, issued_at=now_kst)

    row = conn.execute("SELECT memo FROM bank_transactions WHERE tid=?", (tid,)).fetchone()
    memo = (row[0] if row else "") or ""
    invoice_number = _extract_invoice_number_from_memo(memo)
    inv_row = None
    inv_ambiguous = False

    if invoice_number:
      inv_row, inv_ambiguous = _resolve_unique_invoice_row_for_transaction(
        conn, tid=tid, memo_invoice_number=invoice_number
      )
      if inv_ambiguous:
        _log_ambiguous_tax_invoice_number(invoice_number, tid)
      elif inv_row:
        invoice_updated = _sync_invoice_tax_status(
          conn,
          invoice_id=int(inv_row[0]),
          invoice_status=(inv_row[2] or "").strip().lower(),
          issued=issued,
          issued_at=now_kst,
        )

    conn.commit()
    return jsonify(
      {
        "ok": True,
        "tid": tid,
        "issued": int(issued),
        "issuedAt": (now_kst if issued else None),
        "invoiceNumber": invoice_number,
        "invoiceUpdated": bool(invoice_updated),
        "invoiceAmbiguous": bool(inv_ambiguous) if invoice_number else False,
        "invoiceId": int(inv_row[0]) if (invoice_number and inv_row) else None,
      }
    )
  except DB_ERRORS as exc:
    return jsonify({"error": True, "message": str(exc)}), 500


def _guard_tax_invoice_transaction_type(conn, *, tid: str, issued: int) -> None:
  row = conn.execute("SELECT acc_out FROM bank_transactions WHERE tid=?", (tid,)).fetchone()
  if row is None:
    return
  acc_out = coerce_int(row[0], 0) or 0
  if issued and acc_out > 0:
    abort(400, "cannot mark a withdrawal transaction as tax recorded")


def _set_transaction_tax_invoice_override(conn, *, tid: str, issued: int, issued_at: str) -> None:
  if issued:
    cur = conn.execute(
      "UPDATE bank_transactions SET tax_invoice_issued=1, tax_invoice_override=1, tax_invoice_issued_at=COALESCE(tax_invoice_issued_at, ?), updated_at=CURRENT_TIMESTAMP WHERE tid=?",
      (issued_at, tid),
    )
  else:
    cur = conn.execute(
      "UPDATE bank_transactions SET tax_invoice_issued=0, tax_invoice_override=0, tax_invoice_issued_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE tid=?",
      (tid,),
    )
  if cur.rowcount != 0:
    return
  if issued:
    conn.execute(
      "INSERT INTO bank_transactions (tid, tax_invoice_issued, tax_invoice_override, tax_invoice_issued_at, created_at, updated_at) VALUES (?, 1, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING",
      (tid, issued_at),
    )
  else:
    conn.execute(
      "INSERT INTO bank_transactions (tid, tax_invoice_issued, tax_invoice_override, tax_invoice_issued_at, created_at, updated_at) VALUES (?, 0, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING",
      (tid,),
    )


def _log_ambiguous_tax_invoice_number(invoice_number: str, tid: str) -> None:
  try:
    current_app.logger.warning(
      "bank_activity.tax_invoice: Ambiguous invoice number (number=%s, tid=%s); skipping invoice sync",
      invoice_number,
      tid,
    )
  except RuntimeError as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.bank_activity.tax_invoice.ambiguous_invoice.log_warning",
      log_key="billing_invoices.bank_activity.tax_invoice.ambiguous_invoice.log_warning",
      log_window_seconds=300,
    )


def _sync_invoice_tax_status(
  conn,
  *,
  invoice_id: int,
  invoice_status: str,
  issued: int,
  issued_at: str,
) -> bool:
  try:
    cols = _get_column_names(conn, "invoices")
  except DB_ERRORS:
    cols = {"billing_status", "tax_issued_at"}

  has_tax_issue_cols = {"tax_issue_type", "tax_issue_source", "tax_issue_note"}.issubset(
    set(cols)
  )
  if issued:
    _mark_invoice_tax_issued(
      conn,
      invoice_id=invoice_id,
      issued_at=issued_at,
      cols=cols,
      has_tax_issue_cols=has_tax_issue_cols,
    )
    return True
  if invoice_status != "tax_issued":
    return False
  _clear_invoice_tax_issued(
    conn,
    invoice_id=invoice_id,
    cols=cols,
    has_tax_issue_cols=has_tax_issue_cols,
  )
  return True


def _mark_invoice_tax_issued(
  conn,
  *,
  invoice_id: int,
  issued_at: str,
  cols: set[str],
  has_tax_issue_cols: bool,
) -> None:
  if "billing_status" in cols and "tax_issued_at" in cols:
    if has_tax_issue_cols:
      conn.execute(
        """
        UPDATE invoices
        SET status=?, billing_status=?, tax_issued_at=?,
          tax_issue_type='tax_invoice', tax_issue_source='bank_activity'
        WHERE id=?
        """,
        ("tax_issued", "tax_issued", issued_at, invoice_id),
      )
    else:
      conn.execute(
        "UPDATE invoices SET status=?, billing_status=?, tax_issued_at=? WHERE id=?",
        ("tax_issued", "tax_issued", issued_at, invoice_id),
      )
  elif "tax_issued_at" in cols:
    conn.execute(
      "UPDATE invoices SET status=?, tax_issued_at=? WHERE id=?",
      ("tax_issued", issued_at, invoice_id),
    )
  else:
    conn.execute("UPDATE invoices SET status=? WHERE id=?", ("tax_issued", invoice_id))


def _clear_invoice_tax_issued(
  conn,
  *,
  invoice_id: int,
  cols: set[str],
  has_tax_issue_cols: bool,
) -> None:
  if "billing_status" in cols and "tax_issued_at" in cols:
    if has_tax_issue_cols:
      conn.execute(
        """
        UPDATE invoices
        SET status=?, billing_status=?, tax_issued_at=NULL,
          tax_issue_type=NULL, tax_issue_source=NULL, tax_issue_note=NULL
        WHERE id=?
        """,
        ("sent", "sent", invoice_id),
      )
    else:
      conn.execute(
        "UPDATE invoices SET status=?, billing_status=?, tax_issued_at=NULL WHERE id=?",
        ("sent", "sent", invoice_id),
      )
  elif "tax_issued_at" in cols and "billing_status" in cols:
    conn.execute(
      "UPDATE invoices SET status=?, billing_status=?, tax_issued_at=NULL WHERE id=?",
      ("sent", "sent", invoice_id),
    )
  elif "tax_issued_at" in cols:
    conn.execute(
      "UPDATE invoices SET status=?, tax_issued_at=NULL WHERE id=?",
      ("sent", invoice_id),
    )
  elif "billing_status" in cols:
    conn.execute(
      "UPDATE invoices SET status=?, billing_status=? WHERE id=?",
      ("sent", "sent", invoice_id),
    )
  else:
    conn.execute("UPDATE invoices SET status=? WHERE id=?", ("sent", invoice_id))


@bp.post("/memo")
def save_memo():
  data = request.get_json(silent=True) or {}
  tid = (data.get("tid") or "").strip()
  memo = (data.get("memo") or "").strip()
  if not tid:
    abort(400, "tid is required")

  try:
    conn = get_db()
    prev = conn.execute("SELECT memo FROM bank_transactions WHERE tid=?", (tid,)).fetchone()
    prev_memo = (prev[0] if prev else "") or ""
    prev_inv = _extract_invoice_number_from_memo(prev_memo)
    next_inv = _extract_invoice_number_from_memo(memo)
    inv_changed = prev_inv != next_inv

    if prev:
      if inv_changed:
        conn.execute(
          "UPDATE bank_transactions SET memo=?, tax_invoice_override=NULL, tax_invoice_issued=0, tax_invoice_issued_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE tid=?",
          (memo, tid),
        )
      else:
        conn.execute(
          "UPDATE bank_transactions SET memo=?, updated_at=CURRENT_TIMESTAMP WHERE tid=?",
          (memo, tid),
        )
    else:
      conn.execute(
        "INSERT INTO bank_transactions (tid, memo, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING",
        (tid, memo),
      )

    invoice_status_map, inv_ambiguous = _invoice_status_map_for_memo(conn, tid, next_inv)
    row = conn.execute(
      "SELECT memo, tax_invoice_issued, tax_invoice_issued_at, tax_invoice_override FROM bank_transactions WHERE tid=?",
      (tid,),
    ).fetchone()
    eff = 0
    eff_at = None
    inv_no = None
    if row:
      eff, eff_at, inv_no = _compute_effective_tax_invoice(
        row[3], row[1], row[2], row[0], invoice_status_map
      )

    conn.commit()
    return jsonify(
      {
        "ok": True,
        "taxInvoiceIssued": int(eff),
        "taxInvoiceIssuedAt": eff_at,
        "invoiceNumber": inv_no,
        "invoiceAmbiguous": bool(inv_ambiguous) if next_inv else False,
      }
    )
  except DB_ERRORS as exc:
    return jsonify({"error": True, "message": str(exc)}), 500


def _invoice_status_map_for_memo(
  conn, tid: str, invoice_number: str | None
) -> tuple[dict[str, str], bool]:
  invoice_status_map: dict[str, str] = {}
  if not invoice_number:
    return invoice_status_map, False
  try:
    tid_row, tid_ambiguous = _fetch_unique_invoice_row_by_payment_tid(conn, tid)
    if tid_ambiguous:
      return {}, True
    if tid_row and (tid_row[1] or "").strip() == str(invoice_number).strip():
      return {str(invoice_number): str(tid_row[2] or "")}, False
    number_row, number_ambiguous = _fetch_unique_invoice_row_by_number(
      conn, str(invoice_number)
    )
    if number_ambiguous:
      return {}, True
    if number_row:
      invoice_status_map[str(invoice_number)] = str(number_row[2] or "")
  except DB_ERRORS:
    return {}, False
  return invoice_status_map, False
