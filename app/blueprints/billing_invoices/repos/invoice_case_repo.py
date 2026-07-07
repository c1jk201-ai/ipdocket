from __future__ import annotations

from app.utils.error_logging import report_swallowed_exception


def _not_deleted_sql(column_expr: str) -> str:
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def _query_matter_rows(
  conn,
  *,
  field: str,
  value: str,
  case_insensitive_trim: bool = False,
  limit: int = 6,
) -> list[tuple[str, str]]:
  if field not in {"matter_id", "our_ref", "old_our_ref", "your_ref"}:
    return []
  try:
    if case_insensitive_trim:
      sql = (
        f"SELECT matter_id, COALESCE(our_ref, '') AS our_ref "
        f"FROM matter "
        f"WHERE UPPER(TRIM(COALESCE({field}, ''))) = UPPER(TRIM(?)) "
        f"LIMIT ?"
      )
    else:
      sql = (
        f"SELECT matter_id, COALESCE(our_ref, '') AS our_ref "
        f"FROM matter "
        f"WHERE {field} = ? "
        f"LIMIT ?"
      )
    rows = conn.execute(sql, (value, int(limit))).fetchall()
  except Exception:
    rows = []
  out: list[tuple[str, str]] = []
  seen: set[str] = set()
  for r in rows or []:
    try:
      mid = str(r[0] or "").strip()
    except Exception:
      mid = ""
    if not mid or mid in seen:
      continue
    seen.add(mid)
    try:
      oref = str(r[1] or "").strip()
    except Exception:
      oref = ""
    out.append((mid, oref))
  return out


def resolve_matter_identifier(conn, raw: str) -> dict:
  """
  Resolve a matter from a free-text identifier safely.

  Priority:
  1) exact matter_id
  2) exact our_ref
  3) exact old_our_ref
  4) exact your_ref
  5) case-insensitive+trim variants of 1~4

  Returns:
   {
    "status": "ok"|"empty"|"not_found"|"ambiguous",
    "matter_id": str|None,
    "our_ref": str|None,
    "source": str|None,
    "input": str,
    "matches": [{"matter_id": "...", "our_ref": "..."}]
   }
  """
  token = str(raw or "").strip()
  base = {
    "status": "not_found",
    "matter_id": None,
    "our_ref": None,
    "source": None,
    "input": token,
    "matches": [],
  }
  if not token:
    base["status"] = "empty"
    return base

  plans: list[tuple[str, str, bool]] = [
    ("matter_id_exact", "matter_id", False),
    ("our_ref_exact", "our_ref", False),
    ("old_our_ref_exact", "old_our_ref", False),
    ("your_ref_exact", "your_ref", False),
    ("matter_id_ci", "matter_id", True),
    ("our_ref_ci", "our_ref", True),
    ("old_our_ref_ci", "old_our_ref", True),
    ("your_ref_ci", "your_ref", True),
  ]

  for source, field, ci in plans:
    rows = _query_matter_rows(conn, field=field, value=token, case_insensitive_trim=ci, limit=6)
    if not rows:
      continue
    if len(rows) == 1:
      mid, oref = rows[0]
      return {
        "status": "ok",
        "matter_id": mid,
        "our_ref": oref or mid,
        "source": source,
        "input": token,
        "matches": [{"matter_id": mid, "our_ref": oref or mid}],
      }
    return {
      "status": "ambiguous",
      "matter_id": None,
      "our_ref": None,
      "source": source,
      "input": token,
      "matches": [{"matter_id": mid, "our_ref": oref or mid} for mid, oref in rows[:5]],
    }
  return base


def fetch_matter_ref(conn, matter_id: str) -> tuple[str, str] | None:
  if not matter_id:
    return None
  row = conn.execute(
    "SELECT matter_id, our_ref FROM matter WHERE matter_id=?",
    (matter_id,),
  ).fetchone()
  if not row:
    return None
  return (str(row[0]), str(row[1] or ""))


def sync_invoice_primary_case(conn, invoice_id: int) -> None:
  row = None
  try:
    row = conn.execute(
      f"""
      SELECT l.matter_id, COALESCE(m.our_ref, l.our_ref) AS our_ref
      FROM external_invoice_case_map l
      LEFT JOIN matter m ON m.matter_id = l.matter_id
      WHERE l.external_invoice_id=?
       AND {_not_deleted_sql("l.is_deleted")}
      ORDER BY l.id DESC
      LIMIT 1
      """,
      (int(invoice_id),),
    ).fetchone()
  except Exception:
    row = None

  try:
    if row:
      conn.execute(
        "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
        (row[0], row[1], int(invoice_id)),
      )
    else:
      conn.execute(
        "UPDATE invoices SET ipm_case_id=NULL, ipm_case_ref=NULL WHERE id=?",
        (int(invoice_id),),
      )
    conn.commit()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_case_repo.sync_invoice_primary_case",
      log_key="billing_invoices.invoice_case_repo.sync_invoice_primary_case",
      log_window_seconds=300,
    )
    try:
      rollback = getattr(conn, "rollback", None)
      if callable(rollback):
        rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_case_repo.sync_invoice_primary_case.rollback",
        log_key="billing_invoices.invoice_case_repo.sync_invoice_primary_case.rollback",
        log_window_seconds=300,
      )


def _sync_external_invoice_case_link(
  conn,
  *,
  invoice_id: int,
  matter_id: str,
  our_ref: str,
  linked: bool,
) -> None:
  """
  Keep the legacy 1:1 link snapshot aligned with the N:N map used by
  the matching UI. The case ledger still reads this table as a compatibility
  fallback, so stale active rows can make an unlinked invoice reappear.
  """
  try:
    if linked:
      row = conn.execute(
        "SELECT number FROM invoices WHERE id=?",
        (int(invoice_id),),
      ).fetchone()
      invoice_number = ""
      if row:
        try:
          invoice_number = str(row["number"] or "")
        except Exception:
          invoice_number = str(row[0] or "")
      conn.execute(
        """
        INSERT INTO external_invoice_case_link (
          matter_id,
          our_ref,
          external_invoice_id,
          external_invoice_number,
          is_deleted,
          deleted_at,
          deleted_by,
          delete_reason
        )
        VALUES (?, ?, ?, ?, FALSE, NULL, NULL, NULL)
        ON CONFLICT (external_invoice_id) DO UPDATE SET
          matter_id=EXCLUDED.matter_id,
          our_ref=EXCLUDED.our_ref,
          external_invoice_number=COALESCE(
            NULLIF(EXCLUDED.external_invoice_number, ''),
            external_invoice_case_link.external_invoice_number
          ),
          is_deleted=FALSE,
          deleted_at=NULL,
          deleted_by=NULL,
          delete_reason=NULL,
          updated_at=CURRENT_TIMESTAMP
        """,
        (str(matter_id), str(our_ref or ""), int(invoice_id), invoice_number),
      )
    else:
      conn.execute(
        """
        UPDATE external_invoice_case_link
          SET is_deleted=TRUE,
            deleted_at=CURRENT_TIMESTAMP
         WHERE matter_id=?
          AND external_invoice_id=?
        """,
        (str(matter_id), int(invoice_id)),
      )
    conn.commit()
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoice_case_repo.sync_external_invoice_case_link",
      log_key="billing_invoices.invoice_case_repo.sync_external_invoice_case_link",
      log_window_seconds=300,
    )
    try:
      rollback = getattr(conn, "rollback", None)
      if callable(rollback):
        rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoice_case_repo.sync_external_invoice_case_link.rollback",
        log_key="billing_invoices.invoice_case_repo.sync_external_invoice_case_link.rollback",
        log_window_seconds=300,
      )


def link_case_to_invoice(conn, *, invoice_id: int, matter_id: str) -> bool:
  ref_row = fetch_matter_ref(conn, matter_id)
  if not ref_row:
    return False
  conn.execute(
    """
    INSERT INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id)
    VALUES (?,?,?)
    ON CONFLICT DO NOTHING
    """,
    (ref_row[0], ref_row[1], int(invoice_id)),
  )
  conn.execute(
    """
    UPDATE external_invoice_case_map
      SET our_ref=?,
        is_deleted=FALSE,
        deleted_at=NULL,
        deleted_by=NULL,
        delete_reason=NULL
     WHERE matter_id=?
      AND external_invoice_id=?
    """,
    (ref_row[1], ref_row[0], int(invoice_id)),
  )
  conn.commit()
  _sync_external_invoice_case_link(
    conn,
    invoice_id=int(invoice_id),
    matter_id=ref_row[0],
    our_ref=ref_row[1],
    linked=True,
  )
  sync_invoice_primary_case(conn, int(invoice_id))
  return True


def unlink_case_from_invoice(conn, *, invoice_id: int, matter_id: str) -> None:
  conn.execute(
    "DELETE FROM external_invoice_case_map WHERE matter_id=? AND external_invoice_id=?",
    (matter_id, int(invoice_id)),
  )
  conn.commit()
  _sync_external_invoice_case_link(
    conn,
    invoice_id=int(invoice_id),
    matter_id=str(matter_id),
    our_ref="",
    linked=False,
  )
  sync_invoice_primary_case(conn, int(invoice_id))
