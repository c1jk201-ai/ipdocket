from __future__ import annotations

import json

from flask import abort
from sqlalchemy.exc import DBAPIError
from werkzeug.exceptions import HTTPException

from app.services.billing.db_core import DB_ERRORS, DatabaseError, _execute_insert_returning_id


def get_client_deposit_balance_minor(
  conn, business_profile_id: int | None, client_id: int, currency: str
) -> int:
  currency = (currency or "").strip().upper()
  if not currency:
    abort(400, "Currency .")
  if business_profile_id is None:
    row = conn.execute(
      """
      SELECT COALESCE(SUM(amount_minor), 0) AS bal
      FROM client_deposit_ledger
      WHERE client_id=? AND business_profile_id IS NULL AND currency=?
      """,
      (int(client_id), currency),
    ).fetchone()
  else:
    row = conn.execute(
      """
      SELECT COALESCE(SUM(amount_minor), 0) AS bal
      FROM client_deposit_ledger
      WHERE client_id=? AND business_profile_id=? AND currency=?
      """,
      (int(client_id), int(business_profile_id), currency),
    ).fetchone()
  try:
    return int(row[0] if row is not None else 0)
  except (TypeError, ValueError, IndexError, KeyError):
    return 0


def get_client_deposit_balances_minor(conn, business_profile_id: int | None, client_id: int):
  if business_profile_id is None:
    rows = conn.execute(
      """
      SELECT currency, COALESCE(SUM(amount_minor), 0) AS bal
      FROM client_deposit_ledger
      WHERE client_id=? AND business_profile_id IS NULL
      GROUP BY currency
      """,
      (int(client_id),),
    ).fetchall()
  else:
    rows = conn.execute(
      """
      SELECT currency, COALESCE(SUM(amount_minor), 0) AS bal
      FROM client_deposit_ledger
      WHERE client_id=? AND business_profile_id=?
      GROUP BY currency
      """,
      (int(client_id), int(business_profile_id)),
    ).fetchall()
  out = {}
  for r in rows or []:
    try:
      cur = r[0] if r is not None else ""
    except (TypeError, IndexError):
      cur = ""
    cur = (cur or "").strip().upper()
    if not cur:
      continue
    try:
      out[cur] = int(r[1] if hasattr(r, "__len__") else 0)
    except (TypeError, ValueError, IndexError):
      out[cur] = 0
  return out


def build_client_deposit_audit_meta(
  *,
  entry_id: int = None,
  business_profile_id: int = None,
  client_id: int = None,
  currency: str = None,
  amount_minor: int = None,
  entry_type: str = None,
  memo: str = None,
  related_invoice_id: int = None,
  related_entry_id: int = None,
  related_bank_transaction_id: str = None,
  balance_before_minor: int = None,
  balance_after_minor: int = None,
):
  try:
    payload = {
      "entry_id": entry_id,
      "business_profile_id": business_profile_id,
      "client_id": client_id,
      "currency": (currency or "").strip().upper() if currency else None,
      "amount_minor": amount_minor,
      "entry_type": entry_type,
      "memo": memo,
      "related_invoice_id": related_invoice_id,
      "related_entry_id": related_entry_id,
      "related_bank_transaction_id": related_bank_transaction_id,
      "balance_before_minor": balance_before_minor,
      "balance_after_minor": balance_after_minor,
    }
    return json.dumps(payload, ensure_ascii=False)
  except (TypeError, ValueError):
    return None


def insert_client_deposit_ledger_entry(
  conn,
  business_profile_id: int | None,
  client_id: int,
  currency: str,
  amount_minor: int,
  entry_type: str,
  memo: str = None,
  related_invoice_id: int = None,
  related_entry_id: int = None,
  related_bank_transaction_id: str = None,
  created_by: int = None,
  enforce_non_negative: bool = True,
  begin_immediate: bool = True,
  commit_if_started: bool = True,
):
  if business_profile_id is None:
    bp_id = None
  else:
    try:
      bp_id = int(business_profile_id)
    except (TypeError, ValueError):
      abort(400, "Business profile information is invalid.")
  cid = int(client_id)
  cur = (currency or "").strip().upper()
  if not cur:
    abort(400, "Currency .")

  try:
    amt = int(amount_minor)
  except (TypeError, ValueError):
    abort(400, "Amount is invalid.")
  if amt == 0:
    abort(400, "Amount 0  not available.")

  et = (entry_type or "").strip().lower()
  allowed = {"topup", "apply", "cancel_apply", "refund", "adjust"}
  if et not in allowed:
    abort(400, "Retainer  is invalid.")

  if et == "topup" and amt <= 0:
    abort(400, " Amount .")
  if et == "apply" and amt >= 0:
    abort(400, " Amount .")
  if et == "cancel_apply" and amt <= 0:
    abort(400, "Cancel Amount .")
  if et == "refund" and amt >= 0:
    abort(400, " Amount .")

  if et in {"apply", "cancel_apply"} and not related_invoice_id:
    abort(400, " Invoice required.")

  started = False
  try:
    if begin_immediate and not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")
      started = True

    if related_invoice_id:
      inv = conn.execute(
        "SELECT id, client_id, business_profile_id, currency FROM invoices WHERE id=?",
        (int(related_invoice_id),),
      ).fetchone()
      if not inv:
        abort(400, " Invoice not found.")
      try:
        if int(inv["client_id"]) != cid:
          abort(400, "Invoice Client does not match.")
      except (KeyError, TypeError, ValueError):
        abort(400, "Invoice Client does not match.")
      if bp_id is not None:
        try:
          if int(inv["business_profile_id"] or 1) != bp_id:
            abort(400, "Invoice Business profile information does not match.")
        except (KeyError, TypeError, ValueError):
          abort(400, "Invoice Business profile information does not match.")
      inv_cur = (inv["currency"] or "").strip().upper() if hasattr(inv, "keys") else ""
      if inv_cur and inv_cur != cur:
        abort(400, "Invoice Currency Retainer Currency does not match.")

    related_amount_minor = None
    if related_entry_id:
      rel = conn.execute(
        """
        SELECT id, client_id, business_profile_id, currency, amount_minor, entry_type
        FROM client_deposit_ledger
        WHERE id=?
        """,
        (int(related_entry_id),),
      ).fetchone()
      if not rel:
        abort(400, " Retainer not found.")
      try:
        if int(rel["client_id"]) != cid:
          abort(400, " Retainer Client does not match.")
      except (KeyError, TypeError, ValueError):
        abort(400, " Retainer Client does not match.")
      try:
        rel_bp_id = (
          int(rel["business_profile_id"])
          if rel["business_profile_id"] is not None
          else None
        )
      except Exception:
        rel_bp_id = None
      if rel_bp_id != bp_id:
        abort(400, " Retainer Business profile does not match.")
      rel_cur = (rel["currency"] or "").strip().upper() if hasattr(rel, "keys") else ""
      if rel_cur and rel_cur != cur:
        abort(400, " Retainer Currency does not match.")
      try:
        related_amount_minor = int(rel["amount_minor"])
      except (KeyError, TypeError, ValueError):
        related_amount_minor = None

      if et == "cancel_apply":
        rel_type = (rel["entry_type"] or "").strip().lower()
        if rel_type != "apply":
          abort(400, "Cancel target is invalid.")
        if related_amount_minor is None or amt != (-related_amount_minor):
          abort(400, "Cancel Amount Source transaction does not match.")
        already = conn.execute(
          """
          SELECT 1
          FROM client_deposit_ledger
          WHERE related_entry_id=? AND entry_type='cancel_apply'
          LIMIT 1
          """,
          (int(related_entry_id),),
        ).fetchone()
        if already:
          abort(400, " Cancel .")
    elif et == "cancel_apply":
      abort(400, "Cancel target required.")

    bal_before = get_client_deposit_balance_minor(conn, bp_id, cid, cur)
    bal_after = bal_before + amt
    if enforce_non_negative and bal_after < 0:
      abort(400, "Retainer balance .")

    entry_id = _execute_insert_returning_id(
      conn,
      """
      INSERT INTO client_deposit_ledger (
        business_profile_id,
        client_id,
        currency,
        amount_minor,
        entry_type,
        memo,
        related_invoice_id,
        related_entry_id,
        related_bank_transaction_id,
        created_by
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        bp_id,
        cid,
        cur,
        amt,
        et,
        memo,
        int(related_invoice_id) if related_invoice_id else None,
        int(related_entry_id) if related_entry_id else None,
        (related_bank_transaction_id or None),
        int(created_by) if created_by else None,
      ),
    )

    if started and commit_if_started:
      conn.commit()

    return {
      "entry_id": entry_id,
      "balance_before_minor": bal_before,
      "balance_after_minor": bal_after,
    }

  except (DBAPIError, DatabaseError, HTTPException, ValueError, TypeError, KeyError):
    if started:
      try:
        conn.rollback()
      except DB_ERRORS:
        pass
    raise


def cancel_uncanceled_deposit_applies_for_invoice(
  conn,
  invoice_id: int,
  *,
  memo: str | None = None,
  created_by: int | None = None,
  begin_immediate: bool = True,
  commit_if_started: bool = True,
):
  """Cancel all uncanceled deposit apply entries for the given invoice.

  This is used when voiding/deleting invoices to avoid leaving negative ledger
  entries orphaned from an invoice record.

  - Idempotent: already-canceled apply entries are skipped.
  - Enforces invariants via `insert_client_deposit_ledger_entry`.
  - Returns a list of inserted cancel entries (for audit logging).
  """
  try:
    inv_id = int(invoice_id)
  except Exception:
    abort(400, "Invoice ID is invalid.")

  started = False
  try:
    if begin_immediate and not conn.in_transaction:
      conn.execute("BEGIN IMMEDIATE")
      started = True

    rows = conn.execute(
      """
      SELECT a.id, a.business_profile_id, a.client_id, a.currency, a.amount_minor
      FROM client_deposit_ledger a
      LEFT JOIN client_deposit_ledger c
       ON c.related_entry_id = a.id AND c.entry_type='cancel_apply'
      WHERE a.related_invoice_id=?
       AND a.entry_type='apply'
       AND c.id IS NULL
      ORDER BY a.id ASC
      """,
      (inv_id,),
    ).fetchall()

    out = []
    for r in rows or []:
      try:
        apply_id = int(r["id"])
      except Exception:
        continue
      try:
        bp_id = (
          int(r["business_profile_id"]) if r["business_profile_id"] is not None else None
        )
      except Exception:
        bp_id = None
      try:
        cid = int(r["client_id"])
      except Exception:
        continue
      cur = (r["currency"] or "").strip().upper()
      if not cur:
        continue
      try:
        apply_amt = int(r["amount_minor"])
      except Exception:
        continue

      # apply is negative; cancel must be positive and match exactly.
      cancel_amt = -int(apply_amt)
      if cancel_amt <= 0:
        continue

      res = insert_client_deposit_ledger_entry(
        conn,
        bp_id,
        cid,
        cur,
        int(cancel_amt),
        "cancel_apply",
        memo=memo or "auto_cancel_apply",
        related_invoice_id=inv_id,
        related_entry_id=int(apply_id),
        created_by=(int(created_by) if created_by is not None else None),
        begin_immediate=False, # reuse outer transaction
        commit_if_started=False,
      )
      out.append(
        {
          "apply_entry_id": int(apply_id),
          "cancel_entry_id": res.get("entry_id"),
          "business_profile_id": (int(bp_id) if bp_id is not None else None),
          "client_id": int(cid),
          "currency": cur,
          "amount_minor": int(cancel_amt),
          "related_invoice_id": int(inv_id),
          "balance_before_minor": res.get("balance_before_minor"),
          "balance_after_minor": res.get("balance_after_minor"),
          "memo": memo or "auto_cancel_apply",
        }
      )

    if started and commit_if_started:
      conn.commit()

    return out

  except (DBAPIError, DatabaseError, HTTPException, ValueError, TypeError, KeyError):
    if started:
      try:
        conn.rollback()
      except DB_ERRORS:
        pass
    raise
