"""Lightweight bridge helpers for the billing_invoices database.

This module currently offers read-only access to invoice summaries so that
existing Matter/ from Billing  times .
 CRUD Status  days/.

Now uses SQLAlchemy via billing_invoices.db for PostgreSQL compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.services.billing.db_core import get_db as _billing_get_db
from app.services.billing.db_core import unified_clients_enabled
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

logger = logging.getLogger(__name__)

try: # pragma: no cover - used only for type checking
  from app.models.client import Client
  from app.models.ip_records import ExternalInvoiceCaseLink, ExternalInvoiceCaseMap, LegacyInvoice
except Exception: # pragma: no cover
  Client = None # type: ignore
  LegacyInvoice = None # type: ignore
  ExternalInvoiceCaseLink = None # type: ignore
  ExternalInvoiceCaseMap = None # type: ignore


class InvoiceBridgeError(RuntimeError):
  """Raised when the bridge cannot connect to or query the legacy DB."""


# New wrapper classes removed - now using get_db() from billing_invoices.db
# which already handles prefix rewriting and PostgreSQL compatibility


@dataclass
class InvoiceSummary:
  invoice_id: int
  number: Optional[str]
  internal_reference: Optional[str]
  client_name: Optional[str]
  issue_date: Optional[str]
  due_date: Optional[str]
  status: Optional[str]
  billing_status: Optional[str]
  payment_status: Optional[str]
  total: float
  currency: Optional[str]


def _connect_legacy_db():
  """Get database connection via billing_invoices.db (PostgreSQL compatible)."""
  try:
    return _billing_get_db()
  except Exception as exc:
    raise InvoiceBridgeError(f"Invoice DB Link : {exc}") from exc


def _build_invoice_view_url(external_id: int) -> Optional[str]:
  base = (current_app.config.get("INVOICE_MODULE_VIEW_BASE_URL") or "").strip()
  if not base:
    return None
  return f"{base.rstrip('/')}/{external_id}"


def _row_get(row, key: str) -> Optional[str]:
  try:
    if hasattr(row, "keys"):
      keys = row.keys()
    else:
      keys = []
  except Exception:
    keys = []
  if key in keys:
    try:
      return row[key]
    except Exception:
      return None
  return None


def _active_clients_query():
  if Client is None:
    raise InvoiceBridgeError("Client  .")
  return Client.query.filter(or_(Client.is_deleted.is_(False), Client.is_deleted.is_(None)))


def _float_or_zero(value) -> float:
  try:
    return float(value or 0)
  except Exception:
    return 0.0


def _line_item_columns(cur) -> set[str]:
  try:
    cur.execute("SELECT * FROM line_items LIMIT 0")
    return {str(d[0]).lower() for d in (cur.description or []) if d and d[0]}
  except Exception:
    return set()


def _serialize_invoice_row(row) -> Dict[str, Optional[str]]:
  return {
    "id": row["id"],
    "number": row["number"],
    "internal_reference": _row_get(row, "internal_reference"),
    "issue_date": _row_get(row, "issue_date"),
    "due_date": _row_get(row, "due_date"),
    "status": _row_get(row, "status"),
    "billing_status": _row_get(row, "billing_status"),
    "payment_status": _row_get(row, "payment_status"),
    "total": float(row["total"] or 0),
    "currency": _row_get(row, "currency"),
    "client_name": _row_get(row, "client_name"),
    "ipm_case_id": _row_get(row, "ipm_case_id"),
    "ipm_case_ref": _row_get(row, "ipm_case_ref"),
    "ipm_invoice_id": _row_get(row, "ipm_invoice_id"),
    "view_url": _build_invoice_view_url(int(row["id"])),
  }


def _snapshot_legacy_link(row) -> Dict[str, Optional[str]]:
  return {
    "ipm_case_id": _row_get(row, "ipm_case_id"),
    "ipm_case_ref": _row_get(row, "ipm_case_ref"),
    "ipm_invoice_id": _row_get(row, "ipm_invoice_id"),
  }


def _restore_legacy_link(conn, *, invoice_id: int, prior: Dict[str, Optional[str]]) -> bool:
  if not prior:
    return False
  try:
    cur = conn.cursor()
    cur.execute(
      "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=?, ipm_invoice_id=? WHERE id=?",
      (
        prior.get("ipm_case_id"),
        prior.get("ipm_case_ref"),
        prior.get("ipm_invoice_id"),
        int(invoice_id),
      ),
    )
    conn.commit()
    return True
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="invoice_bridge._restore_legacy_link",
      log_key="invoice_bridge._restore_legacy_link",
      log_window_seconds=300,
    )
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge._restore_legacy_link.rollback",
        log_key="invoice_bridge._restore_legacy_link.rollback",
        log_window_seconds=300,
      )
    try:
      logger.warning("Failed to restore legacy invoice link for %s", invoice_id)
    except Exception as log_exc:
      report_swallowed_exception(
        log_exc,
        context="invoice_bridge._restore_legacy_link.logger_warning",
        log_key="invoice_bridge._restore_legacy_link.logger_warning",
        log_window_seconds=300,
      )
    return False


def resolve_external_invoice_id(external_ref: str | int) -> int:
  raw = str(external_ref or "").strip()
  if not raw:
    raise InvoiceBridgeError(" Invoice ID/ required.")
  if raw.isdigit():
    return int(raw)

  conn = _connect_legacy_db()
  cur = conn.cursor()
  rows = cur.execute(
    "SELECT id FROM invoices WHERE number = ? ORDER BY id DESC LIMIT 2",
    (raw,),
  ).fetchall()

  if not rows:
    raise InvoiceBridgeError(f"Invoice from '{raw}' not found.")
  if len(rows) > 1:
    raise InvoiceBridgeError(
      f"Invoice '{raw}' items exists.  Invoice ID ."
    )
  row = rows[0]
  try:
    return int(row["id"])
  except Exception as exc:
    raise InvoiceBridgeError(" Invoice ID failed.") from exc


def fetch_linked_invoices_for_case(
  *, matter_id: str | None, our_ref: str | None, limit: int = 50
) -> List[Dict[str, Optional[str]]]:
  mid = (matter_id or "").strip()
  ref = (our_ref or "").strip()
  if not (mid or ref):
    return []

  # Prefer N:N main DB mapping table if available (authoritative)
  if ExternalInvoiceCaseMap is not None and mid:
    try:
      q = (
        db.session.query(ExternalInvoiceCaseMap)
        .filter(ExternalInvoiceCaseMap.matter_id == mid)
        .filter(
          or_(
            ExternalInvoiceCaseMap.is_deleted == False, # noqa: E712
            ExternalInvoiceCaseMap.is_deleted.is_(None),
          )
        )
        .order_by(ExternalInvoiceCaseMap.id.desc())
      )
      links = q.limit(int(max(1, limit))).all()
      ext_ids = [int(l.external_invoice_id) for l in links if l.external_invoice_id]
      if ext_ids:
        rows = fetch_invoices_by_ids(ext_ids)
        ordered = [rows[i] for i in ext_ids if i in rows]
        return ordered
    except Exception:
      db.session.rollback()

  # Backward-compatible: 1:1 mapping table
  if ExternalInvoiceCaseLink is not None and mid:
    try:
      q = db.session.query(ExternalInvoiceCaseLink).filter(
        ExternalInvoiceCaseLink.matter_id == mid
      )
      q = q.filter(
        or_(
          ExternalInvoiceCaseLink.is_deleted == False, # noqa: E712
          ExternalInvoiceCaseLink.is_deleted.is_(None),
        )
      )
      if ref:
        q = q.order_by(ExternalInvoiceCaseLink.id.desc())
      links = q.limit(int(max(1, limit))).all()
      ext_ids = [int(l.external_invoice_id) for l in links if l.external_invoice_id]
      if ext_ids:
        rows = fetch_invoices_by_ids(ext_ids)
        # keep order by links
        ordered = [rows[i] for i in ext_ids if i in rows]
        return ordered
    except Exception:
      # fallback to legacy invoice DB query
      db.session.rollback()

  where = []
  params: List[str] = []
  if mid:
    where.append("invoices.ipm_case_id = ?")
    params.append(mid)
  if ref:
    where.append("invoices.ipm_case_ref = ?")
    params.append(ref)
  where_sql = " OR ".join(where) if where else "1=0"

  sql = f"""
    SELECT invoices.*, clients.name as client_name
     FROM invoices
     LEFT JOIN clients ON clients.id = invoices.client_id
     WHERE {where_sql}
     ORDER BY
       CASE
         WHEN invoices.issue_date IS NULL OR invoices.issue_date = '' THEN 1
         ELSE 0
       END,
       invoices.issue_date DESC,
       invoices.id DESC
     LIMIT ?
  """

  conn = _connect_legacy_db()
  cur = conn.cursor()
  rows = cur.execute(sql, params + [int(max(1, limit))]).fetchall()
  return [_serialize_invoice_row(r) for r in rows or []]


def fetch_external_invoice_links_for_case(*, matter_id: str) -> List[Dict[str, object]]:
  mid = (matter_id or "").strip()
  if not mid:
    return []

  if ExternalInvoiceCaseMap is not None:
    rows = (
      db.session.query(ExternalInvoiceCaseMap)
      .filter(ExternalInvoiceCaseMap.matter_id == mid)
      .filter(
        or_(
          ExternalInvoiceCaseMap.is_deleted == False, # noqa: E712
          ExternalInvoiceCaseMap.is_deleted.is_(None),
        )
      )
      .order_by(ExternalInvoiceCaseMap.id.desc())
      .all()
    )
    return [
      {
        "id": r.id,
        "matter_id": r.matter_id,
        "our_ref": r.our_ref,
        "external_invoice_id": r.external_invoice_id,
      }
      for r in rows
    ]

  if ExternalInvoiceCaseLink is None:
    return []
  rows = (
    db.session.query(ExternalInvoiceCaseLink)
    .filter(ExternalInvoiceCaseLink.matter_id == mid)
    .filter(
      or_(
        ExternalInvoiceCaseLink.is_deleted == False, # noqa: E712
        ExternalInvoiceCaseLink.is_deleted.is_(None),
      )
    )
    .order_by(ExternalInvoiceCaseLink.id.desc())
    .all()
  )
  return [
    {
      "id": r.id,
      "matter_id": r.matter_id,
      "our_ref": r.our_ref,
      "external_invoice_id": r.external_invoice_id,
      "external_invoice_number": r.external_invoice_number,
      "external_invoice_url": r.external_invoice_url,
      "ipm_invoice_id": r.ipm_invoice_id,
    }
    for r in rows
  ]


def fetch_external_invoice_link_for_invoice_id(
  *, external_invoice_id: int
) -> Optional[Dict[str, object]]:
  try:
    ext_id = int(external_invoice_id)
  except Exception:
    return None

  if ExternalInvoiceCaseMap is not None:
    try:
      row = (
        db.session.query(ExternalInvoiceCaseMap)
        .filter(ExternalInvoiceCaseMap.external_invoice_id == ext_id)
        .filter(
          or_(
            ExternalInvoiceCaseMap.is_deleted == False, # noqa: E712
            ExternalInvoiceCaseMap.is_deleted.is_(None),
          )
        )
        .order_by(ExternalInvoiceCaseMap.id.desc())
        .first()
      )
      if row:
        return {
          "id": row.id,
          "matter_id": row.matter_id,
          "our_ref": row.our_ref,
          "external_invoice_id": row.external_invoice_id,
        }
    except Exception as exc:
      # Best-effort: fall back to legacy 1:1 mapping if main mapping lookup fails.
      report_swallowed_exception(
        exc,
        context="invoice_bridge.fetch_external_invoice_link_for_invoice_id.map_lookup",
        log_key="invoice_bridge.fetch_external_invoice_link_for_invoice_id.map_lookup",
        log_window_seconds=300,
      )

  if ExternalInvoiceCaseLink is None:
    return None
  row = (
    db.session.query(ExternalInvoiceCaseLink)
    .filter(ExternalInvoiceCaseLink.external_invoice_id == ext_id)
    .filter(
      or_(
        ExternalInvoiceCaseLink.is_deleted == False, # noqa: E712
        ExternalInvoiceCaseLink.is_deleted.is_(None),
      )
    )
    .first()
  )
  if not row:
    return None
  return {
    "id": row.id,
    "matter_id": row.matter_id,
    "our_ref": row.our_ref,
    "external_invoice_id": row.external_invoice_id,
    "external_invoice_number": row.external_invoice_number,
    "external_invoice_url": row.external_invoice_url,
    "ipm_invoice_id": row.ipm_invoice_id,
  }


def link_legacy_invoice_to_case(
  *, matter_id: str, our_ref: str | None, external_invoice_ref: str | int
) -> Dict[str, Optional[str]]:
  mid = (matter_id or "").strip()
  if not mid:
    raise InvoiceBridgeError("matter_id required.")
  case_ref = (our_ref or "").strip() or None
  external_id = resolve_external_invoice_id(external_invoice_ref)

  conn = _connect_legacy_db()
  cur = conn.cursor()
  row = cur.execute(
    "SELECT invoices.*, clients.name as client_name "
    "FROM invoices LEFT JOIN clients ON clients.id = invoices.client_id "
    "WHERE invoices.id=?",
    (int(external_id),),
  ).fetchone()

  if not row:
    raise InvoiceBridgeError(f"Invoice from ID {external_id} not found.")

  prior = _snapshot_legacy_link(row)
  cur.execute(
    "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
    (mid, case_ref, int(external_id)),
  )
  try:
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.link_legacy_invoice_to_case.rollback",
        log_key="invoice_bridge.link_legacy_invoice_to_case.rollback",
        log_window_seconds=300,
      )
    raise InvoiceBridgeError("Invoice  failed.") from exc

  updated = cur.execute(
    "SELECT invoices.*, clients.name as client_name "
    "FROM invoices LEFT JOIN clients ON clients.id = invoices.client_id "
    "WHERE invoices.id=?",
    (int(external_id),),
  ).fetchone()

  data = _serialize_invoice_row(updated or row)

  # Write-through to main DB mapping tables
  try:
    if ExternalInvoiceCaseMap is not None:
      db.session.execute(
        text(
          "INSERT INTO external_invoice_case_map(matter_id, our_ref, external_invoice_id) "
          "VALUES (:matter_id, :our_ref, :external_invoice_id) "
          "ON CONFLICT DO NOTHING"
        ),
        {
          "matter_id": mid,
          "our_ref": case_ref,
          "external_invoice_id": int(data.get("id") or external_id),
        },
      )
      db.session.execute(
        text(
          "UPDATE external_invoice_case_map "
          "SET is_deleted=FALSE, deleted_at=NULL, deleted_by=NULL "
          "WHERE matter_id=:matter_id AND external_invoice_id=:external_invoice_id"
        ),
        {
          "matter_id": mid,
          "external_invoice_id": int(data.get("id") or external_id),
        },
      )
    if ExternalInvoiceCaseLink is not None:
      external_id = int(data.get("id") or external_id)
      link = (
        db.session.query(ExternalInvoiceCaseLink)
        .filter(ExternalInvoiceCaseLink.external_invoice_id == external_id)
        .first()
      )
      if not link:
        link = ExternalInvoiceCaseLink(external_invoice_id=external_id)
        db.session.add(link)
      link.matter_id = mid
      link.our_ref = case_ref
      link.external_invoice_number = data.get("number")
      link.external_invoice_url = data.get("view_url")
      link.is_deleted = False
      link.deleted_at = None
      link.deleted_by = None
      # ipm_invoice_id is optional here; set only if we can infer later.
    db.session.commit()
  except SQLAlchemyError as exc:
    db.session.rollback()
    _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError("Invoice Save failed.") from exc
  except Exception as exc:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.link_legacy_invoice_to_case.rollback_main_db",
        log_key="invoice_bridge.link_legacy_invoice_to_case.rollback_main_db",
        log_window_seconds=300,
      )
    _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError("Invoice Save Error .") from exc

  return data


def unlink_legacy_invoice_from_case(
  *,
  external_invoice_ref: str | int,
  matter_id: str | None = None,
  our_ref: str | None = None,
  actor_id: int | None = None,
  soft: bool = True,
) -> None:
  external_id = resolve_external_invoice_id(external_invoice_ref)
  mid = (matter_id or "").strip() or None
  ref = (our_ref or "").strip() or None

  remaining_case: tuple[str | None, str | None] = (None, None)
  if mid and ExternalInvoiceCaseMap is not None:
    try:
      row = (
        db.session.query(ExternalInvoiceCaseMap)
        .filter(
          ExternalInvoiceCaseMap.external_invoice_id == int(external_id),
          ExternalInvoiceCaseMap.matter_id != mid,
          or_(
            ExternalInvoiceCaseMap.is_deleted == False, # noqa: E712
            ExternalInvoiceCaseMap.is_deleted.is_(None),
          ),
        )
        .order_by(ExternalInvoiceCaseMap.id.desc())
        .first()
      )
      if row:
        remaining_case = (row.matter_id, row.our_ref)
    except SQLAlchemyError:
      db.session.rollback()
    except Exception:
      try:
        db.session.rollback()
      except Exception as rollback_exc:
        report_swallowed_exception(
          rollback_exc,
          context="invoice_bridge.unlink_legacy_invoice_from_case.rollback_main_db_remaining_case",
          log_key="invoice_bridge.unlink_legacy_invoice_from_case.rollback_main_db_remaining_case",
          log_window_seconds=300,
        )

  # Legacy invoice row: keep ipm_case_id/ref as a best-effort "primary" link
  conn = _connect_legacy_db()
  cur = conn.cursor()
  prior = {}
  try:
    legacy_row = cur.execute(
      "SELECT ipm_case_id, ipm_case_ref, ipm_invoice_id FROM invoices WHERE id=?",
      (int(external_id),),
    ).fetchone()
    if legacy_row:
      prior = _snapshot_legacy_link(legacy_row)
      current_mid = (prior.get("ipm_case_id") or "").strip() or None
      current_ref = (prior.get("ipm_case_ref") or "").strip() or None
      if mid and current_mid and current_mid != mid:
        raise InvoiceBridgeError(
          " Invoice Current selected Matter Link ."
        )
      if mid and ref and current_ref and current_ref != ref:
        raise InvoiceBridgeError(
          " Invoice Link Current Matter reference does not match."
        )
  except InvoiceBridgeError:
    raise
  except Exception:
    prior = {}
  if remaining_case[0]:
    cur.execute(
      "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=? WHERE id=?",
      (remaining_case[0], remaining_case[1], int(external_id)),
    )
  else:
    cur.execute(
      "UPDATE invoices SET ipm_case_id=NULL, ipm_case_ref=NULL, ipm_invoice_id=NULL WHERE id=?",
      (int(external_id),),
    )
  try:
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.unlink_legacy_invoice_from_case.rollback",
        log_key="invoice_bridge.unlink_legacy_invoice_from_case.rollback",
        log_window_seconds=300,
      )
    raise InvoiceBridgeError("Invoice  failed.") from exc

  # Backward-compatible 1:1 mapping snapshot maintenance
  try:
    if ExternalInvoiceCaseMap is not None and mid:
      q = db.session.query(ExternalInvoiceCaseMap).filter(
        ExternalInvoiceCaseMap.external_invoice_id == int(external_id),
        ExternalInvoiceCaseMap.matter_id == mid,
      )
      if soft:
        link = q.first()
        if link:
          link.is_deleted = True
          link.deleted_at = datetime.utcnow()
          link.deleted_by = actor_id
      else:
        q.delete(synchronize_session=False)

    if ExternalInvoiceCaseLink is not None:
      link = (
        db.session.query(ExternalInvoiceCaseLink)
        .filter(ExternalInvoiceCaseLink.external_invoice_id == int(external_id))
        .first()
      )
      if remaining_case[0]:
        if not link:
          link = ExternalInvoiceCaseLink(external_invoice_id=int(external_id))
          db.session.add(link)
        link.matter_id = remaining_case[0]
        link.our_ref = remaining_case[1]
        link.is_deleted = False
        link.deleted_at = None
        link.deleted_by = None
      else:
        if soft:
          if link:
            link.is_deleted = True
            link.deleted_at = datetime.utcnow()
            link.deleted_by = actor_id
        else:
          if link:
            db.session.delete(link)

    db.session.commit()
  except SQLAlchemyError as exc:
    db.session.rollback()
    _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError("Invoice  failed.") from exc
  except Exception as exc:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.unlink_legacy_invoice_from_case.rollback_main_db",
        log_key="invoice_bridge.unlink_legacy_invoice_from_case.rollback_main_db",
        log_window_seconds=300,
      )
    _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError("Invoice  Error .") from exc


def fetch_invoices_by_references(
  refs: Sequence[str], limit_per_ref: int = 10
) -> List[InvoiceSummary]:
  """Return invoice summaries whose number/internal_reference matches refs."""
  clean_refs = [(ref or "").strip() for ref in refs if (ref or "").strip()]
  if not clean_refs:
    return []

  placeholders = ",".join("?" for _ in clean_refs)
  sql = f"""
    SELECT invoices.id,
        invoices.number,
        invoices.internal_reference,
        invoices.issue_date,
        invoices.due_date,
        invoices.status,
        invoices.billing_status,
        invoices.payment_status,
        invoices.total,
        invoices.currency,
        clients.name AS client_name
     FROM invoices
     LEFT JOIN clients ON clients.id = invoices.client_id
     WHERE invoices.number IN ({placeholders})
      OR invoices.internal_reference IN ({placeholders})
     ORDER BY invoices.issue_date DESC, invoices.id DESC
  """
  params: List[str] = clean_refs + clean_refs
  summaries: List[InvoiceSummary] = []
  conn = _connect_legacy_db()
  cur = conn.cursor()
  cur.execute(sql, params)
  rows = cur.fetchmany(limit_per_ref * len(clean_refs))
  for row in rows or []:
    summaries.append(
      InvoiceSummary(
        invoice_id=row["id"],
        number=row["number"],
        internal_reference=row["internal_reference"],
        client_name=row["client_name"],
        issue_date=row["issue_date"],
        due_date=row["due_date"],
        status=row["status"],
        billing_status=row["billing_status"],
        payment_status=row["payment_status"],
        total=float(row["total"] or 0.0),
        currency=(row["currency"] or "USD"),
      )
    )
  return summaries


def fetch_invoices_for_matter(
  our_ref: Optional[str], expense_refs: Iterable[str] | None = None
) -> List[InvoiceSummary]:
  """Derive candidate references from matter data and fetch matching invoices.

  - our_ref: Matter Our Ref(: YYAA0000US)
  - expense_refs: LegacyExpense/LegacyInvoice fee_ref raw identifier 
  """
  refs: List[str] = []
  if our_ref:
    refs.append(our_ref.strip())
  if expense_refs:
    refs.extend([r.strip() for r in expense_refs if r and r.strip()])

  dedup = list(dict.fromkeys(refs)) # preserves order
  if not dedup:
    return []
  return fetch_invoices_by_references(dedup, limit_per_ref=5)


def fetch_invoices_by_ids(ids: Sequence[int]) -> Dict[int, Dict[str, Optional[str]]]:
  clean_ids = [int(i) for i in ids if i]
  if not clean_ids:
    return {}
  placeholders = ",".join("?" for _ in clean_ids)
  sql = f"""
    SELECT invoices.*, clients.name as client_name
     FROM invoices
     LEFT JOIN clients ON clients.id = invoices.client_id
     WHERE invoices.id IN ({placeholders})
  """
  result: Dict[int, Dict[str, Optional[str]]] = {}
  conn = _connect_legacy_db()
  cur = conn.cursor()
  rows = cur.execute(sql, clean_ids).fetchall()
  for row in rows or []:
    data = _serialize_invoice_row(row)
    result[int(row["id"])] = data

  # Fetch fee breakdown from line_items
  if result:
    # Initialize fee fields
    for inv_id in result:
      result[inv_id]["service_fee"] = 0.0
      result[inv_id]["official_fee"] = 0.0
      result[inv_id]["other_fee"] = 0.0

    cols = _line_item_columns(cur)
    has_qty_price = "qty" in cols and "unit_price" in cols

    if has_qty_price:
      select_cols = ["invoice_id"]
      if "item_type" in cols:
        select_cols.append("item_type")
      if "qty" in cols:
        select_cols.append("qty")
      if "unit_price" in cols:
        select_cols.append("unit_price")
      if "discount" in cols:
        select_cols.append("discount")
      if "is_estimated" in cols:
        select_cols.append("is_estimated")
      if "fx_rate_used" in cols:
        select_cols.append("fx_rate_used")
      if "fx_fee" in cols:
        select_cols.append("fx_fee")
      if "fx_gov" in cols:
        select_cols.append("fx_gov")
      if "fx_markup" in cols:
        select_cols.append("fx_markup")

      line_sql = f"""
        SELECT {", ".join(select_cols)}
         FROM line_items
         WHERE invoice_id IN ({placeholders})
      """
      line_rows = cur.execute(line_sql, clean_ids).fetchall()

      for lr in line_rows or []:
        inv_id = int(_row_get(lr, "invoice_id") or 0)
        if inv_id not in result:
          continue

        if "is_estimated" in cols:
          is_estimated = int(_float_or_zero(_row_get(lr, "is_estimated")))
          if is_estimated:
            continue

        item_type = (_row_get(lr, "item_type") or "service").lower().strip()
        qty = _float_or_zero(_row_get(lr, "qty") or 1)
        unit_price = _float_or_zero(_row_get(lr, "unit_price") or 0)
        discount = _float_or_zero(_row_get(lr, "discount") or 0)
        amount = qty * unit_price * (1 - (discount / 100.0))

        if item_type == "foreign":
          fx_rate = _float_or_zero(_row_get(lr, "fx_rate_used") or 0)
          if fx_rate > 0 and ("fx_fee" in cols or "fx_gov" in cols):
            fx_fee = _float_or_zero(_row_get(lr, "fx_fee"))
            fx_gov = _float_or_zero(_row_get(lr, "fx_gov"))
            fx_markup = _float_or_zero(_row_get(lr, "fx_markup"))
            markup = 1 + (fx_markup / 100.0)
            result[inv_id]["service_fee"] += fx_fee * fx_rate * markup
            result[inv_id]["official_fee"] += fx_gov * fx_rate * markup
            continue

        if item_type == "service":
          result[inv_id]["service_fee"] += amount
        elif item_type == "admin":
          result[inv_id]["other_fee"] += amount
        elif item_type == "foreign":
          result[inv_id]["service_fee"] += amount
        else:
          result[inv_id]["other_fee"] += amount
    elif "fee" in cols or "gov" in cols:
      line_sql = f"""
        SELECT invoice_id,
            item_type,
            COALESCE(SUM(fee), 0) as sum_fee,
            COALESCE(SUM(gov), 0) as sum_gov
         FROM line_items
         WHERE invoice_id IN ({placeholders})
         GROUP BY invoice_id, item_type
      """
      line_rows = cur.execute(line_sql, clean_ids).fetchall()

      for lr in line_rows or []:
        inv_id = int(lr["invoice_id"])
        item_type = (lr["item_type"] or "service").lower().strip()
        fee = _float_or_zero(lr["sum_fee"])
        gov = _float_or_zero(lr["sum_gov"])

        if inv_id in result:
          if item_type == "service":
            result[inv_id]["service_fee"] += fee
            result[inv_id]["official_fee"] += gov
          elif item_type == "admin":
            result[inv_id]["other_fee"] += fee + gov
          elif item_type == "foreign":
            result[inv_id]["service_fee"] += fee
            result[inv_id]["official_fee"] += gov
          else:
            result[inv_id]["other_fee"] += fee + gov
  return result


def fetch_recent_invoices(limit: int = 10) -> List[Dict[str, Optional[str]]]:
  sql = """
    SELECT invoices.*, clients.name as client_name
     FROM invoices
     LEFT JOIN clients ON clients.id = invoices.client_id
     ORDER BY COALESCE(invoices.issue_date, invoices.id) DESC, invoices.id DESC
     LIMIT ?
  """
  conn = _connect_legacy_db()
  cur = conn.cursor()
  rows = cur.execute(sql, (int(max(1, limit)),)).fetchall()
  return [_serialize_invoice_row(row) for row in rows or []]


def upsert_invoice_client(client: "Client") -> int:
  """Create or update a legacy invoice client row from CRM Client data.

  Returns the Invoice-module client_id.
  """
  if Client is None:
    raise InvoiceBridgeError("Client  .")
  if not isinstance(client, Client):
    raise InvoiceBridgeError("upsert_invoice_client  Client required.")

  ipm_client_id = int(client.id)
  ipm_party_id = (client.party_id or "").strip() or None

  mgr = (client.contact_person or "").strip() or None
  try:
    if not mgr and isinstance(client.extra, dict):
      mgr = (
        (client.extra.get("tax_manager") or "").strip()
        or (client.extra.get("tax_ceo") or "").strip()
        or (client.extra.get("contact_person") or "").strip()
        or None
      )
  except Exception as exc:
    # Best-effort: extra payload is optional for invoice client upsert.
    report_swallowed_exception(
      exc,
      context="invoice_bridge.upsert_invoice_client.extra_manager",
      log_key="invoice_bridge.upsert_invoice_client.extra_manager",
      log_window_seconds=300,
    )

  payload = {
    "name": (client.name or "").strip() or "( Client)",
    "email": (client.email or "").strip() or None,
    "phone": (client.phone or "").strip() or None,
    "address": (client.address or "").strip() or None,
    "manager": mgr,
    "notes": ((client.extra or {}).get("note") if isinstance(client.extra, dict) else None),
    "ipm_party_id": ipm_party_id,
    "ipm_client_id": ipm_client_id,
  }

  conn = _connect_legacy_db()
  cur = conn.cursor()
  row_id: Optional[int] = None
  if client.external_invoice_client_id:
    row = cur.execute(
      "SELECT id FROM clients WHERE id=?",
      (int(client.external_invoice_client_id),),
    ).fetchone()
    if row:
      row_id = int(row["id"])

  if row_id is None:
    if ipm_client_id:
      row = cur.execute(
        "SELECT id FROM clients WHERE ipm_client_id=?", (ipm_client_id,)
      ).fetchone()
      if row:
        row_id = int(row["id"])

  if row_id is None and ipm_party_id:
    row = cur.execute(
      "SELECT id FROM clients WHERE ipm_party_id=?", (ipm_party_id,)
    ).fetchone()
    if row:
      row_id = int(row["id"])

  columns = ", ".join(payload.keys())
  placeholders = ", ".join("?" for _ in payload)
  values = list(payload.values())

  if row_id is None:
    cur.execute(f"INSERT INTO clients ({columns}) VALUES ({placeholders})", values)
    row_id = int(cur.lastrowid)
  else:
    assignments = ", ".join(f"{col}=?" for col in payload.keys())
    cur.execute(
      f"UPDATE clients SET {assignments} WHERE id=?",
      values + [row_id],
    )
  conn.commit()

  if client.external_invoice_client_id != row_id:
    client.external_invoice_client_id = row_id
    try:
      db.session.add(client)
      db.session.commit()
    except SQLAlchemyError:
      db.session.rollback()
      raise
  return row_id


def ensure_invoice_client_link(client: "Client") -> int:
  """Idempotent helper that guarantees the CRM client is linked to invoice client.

  In unified mode (INVOICEAPP_UNIFIED_CLIENTS=1), the CRM clients table IS
  the invoice clients table, so no separate synchronization is needed.
  We simply return the client.id to indicate success.
  """
  # In unified mode, billing_invoices.client_id directly references clients.id
  # so there's no need to maintain a separate billing_clients record
  if unified_clients_enabled():
    return int(client.id)

  return upsert_invoice_client(client)


def fetch_invoice_client(invoice_client_id: int) -> Dict[str, object]:
  cid = int(invoice_client_id)
  conn = _connect_legacy_db()
  cur = conn.cursor()
  row = cur.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
  if not row:
    raise InvoiceBridgeError(f"Invoice Client not found: #{cid}")
  data: Dict[str, object] = {}
  try:
    for k in row.keys():
      data[k] = row[k]
  except Exception:
    # Fallback (should not happen with row_factory)
    data = {"id": cid}
  return data


def _update_legacy_client_ipm_link(*, invoice_client_id: int, ipm_client: "Client") -> None:
  cid = int(invoice_client_id)
  ipm_id = int(getattr(ipm_client, "id"))
  ipm_party_id = (getattr(ipm_client, "party_id", None) or "").strip() or None
  conn = _connect_legacy_db()
  cur = conn.cursor()
  cur.execute(
    "UPDATE clients SET ipm_client_id=?, ipm_party_id=? WHERE id=?",
    (ipm_id, ipm_party_id, cid),
  )
  conn.commit()


def ensure_ipm_client_link_from_invoice_client(invoice_client_id: int) -> "Client":
  """Reverse sync: ensure invoice-module client is linked to a CRM Client.

  - Uses legacy clients.ipm_client_id if present and valid.
  - Else matches by CRM Client.external_invoice_client_id (preferred).
  - Else attempts a conservative match by registration_number/email.
  - Writes back legacy clients.ipm_client_id/ipm_party_id.
  """

  if Client is None:
    raise InvoiceBridgeError("Client  .")

  cid = int(invoice_client_id)
  legacy = fetch_invoice_client(cid)

  ipm_client = None
  ipm_id_raw = legacy.get("ipm_client_id")
  try:
    if ipm_id_raw is not None and str(ipm_id_raw).strip().isdigit():
      ipm_client = db.session.get(Client, int(str(ipm_id_raw).strip()))
  except Exception:
    ipm_client = None

  active_clients = _active_clients_query()
  if ipm_client is not None and getattr(ipm_client, "is_deleted", False):
    ipm_client = None

  if ipm_client is None:
    ipm_client = active_clients.filter_by(external_invoice_client_id=cid).first()

  reg_no = str(legacy.get("biz_reg_number") or "").strip() or None
  email = str(legacy.get("email") or "").strip() or None

  if ipm_client is None and reg_no:
    hits = active_clients.filter_by(registration_number=reg_no).all()
    if len(hits) == 1:
      ipm_client = hits[0]
    elif len(hits) > 1:
      raise InvoiceBridgeError(
        f"days Registration No.({reg_no}) CRM Client {len(hits)}people Auto Link not available."
      )

  if ipm_client is None and email:
    hits = active_clients.filter_by(email=email).all()
    if len(hits) == 1:
      ipm_client = hits[0]
    elif len(hits) > 1:
      raise InvoiceBridgeError(
        f"days Email({email}) CRM Client {len(hits)}people Auto Link not available."
      )

  name = str(legacy.get("name") or "").strip() or "( Client)"
  phone = str(legacy.get("phone") or "").strip() or None
  addr = str(legacy.get("address") or "").strip() or None
  manager = str(legacy.get("manager") or "").strip() or None
  notes = str(legacy.get("notes") or "").strip() or None

  created = False
  if ipm_client is None:
    ipm_client = Client(
      name=name,
      email=email,
      phone=phone,
      address=addr,
      registration_number=reg_no,
      contact_person=manager,
      external_invoice_client_id=cid,
      extra={"note": notes} if notes else None,
    )
    db.session.add(ipm_client)
    created = True
  else:
    # Fill blanks only (avoid overwriting curated CRM data)
    if not (ipm_client.name or "").strip() and name:
      ipm_client.name = name
    if not (getattr(ipm_client, "email", None) or "").strip() and email:
      ipm_client.email = email
    if not (getattr(ipm_client, "phone", None) or "").strip() and phone:
      ipm_client.phone = phone
    if not (getattr(ipm_client, "address", None) or "").strip() and addr:
      ipm_client.address = addr
    if not (getattr(ipm_client, "registration_number", None) or "").strip() and reg_no:
      ipm_client.registration_number = reg_no
    if not (getattr(ipm_client, "contact_person", None) or "").strip() and manager:
      ipm_client.contact_person = manager
    if not getattr(ipm_client, "external_invoice_client_id", None):
      ipm_client.external_invoice_client_id = cid

    if notes and isinstance(getattr(ipm_client, "extra", None), dict):
      ipm_client.extra.setdefault("note", notes)

  try:
    db.session.commit()
  except SQLAlchemyError as exc:
    db.session.rollback()
    raise InvoiceBridgeError(f"CRM Client : {exc}") from exc

  try:
    _update_legacy_client_ipm_link(invoice_client_id=cid, ipm_client=ipm_client)
  except Exception as exc:
    # Keep application commit but surface warning
    raise InvoiceBridgeError(f"Invoice Client link : {exc}") from exc

  # Refresh ipm_client to ensure id is present
  if created:
    try:
      db.session.refresh(ipm_client)
    except Exception as exc:
      # Best-effort: refresh failure should not break client creation path.
      report_swallowed_exception(
        exc,
        context="invoice_bridge.get_or_create_ipm_client.refresh",
        log_key="invoice_bridge.get_or_create_ipm_client.refresh",
        log_window_seconds=300,
      )
  return ipm_client


def link_invoice_to_case(
  ipm_invoice: "LegacyInvoice",
  external_invoice_id: int,
  *,
  external_url: Optional[str] = None,
) -> Dict[str, Optional[str]]:
  if LegacyInvoice is None or not isinstance(ipm_invoice, LegacyInvoice):
    raise InvoiceBridgeError(" LegacyInvoice required.")

  external_invoice_id = int(external_invoice_id)
  ipm_case_ref: Optional[str] = None
  try:
    from app.models.ip_records import Matter

    m = db.session.get(Matter, str(ipm_invoice.matter_id))
    ipm_case_ref = (m.our_ref if m else None) or None
  except Exception:
    ipm_case_ref = None
  conn = _connect_legacy_db()
  cur = conn.cursor()
  row = cur.execute(
    "SELECT id, number, ipm_case_id, ipm_case_ref, ipm_invoice_id FROM invoices WHERE id=?",
    (external_invoice_id,),
  ).fetchone()
  if not row:
    raise InvoiceBridgeError(f"Invoice from ID {external_invoice_id} not found.")
  prior = _snapshot_legacy_link(row)
  cur.execute(
    "UPDATE invoices SET ipm_case_id=?, ipm_case_ref=?, ipm_invoice_id=? WHERE id=?",
    (
      ipm_invoice.matter_id,
      ipm_case_ref,
      ipm_invoice.invoice_id,
      external_invoice_id,
    ),
  )
  try:
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.link_invoice_to_case.rollback_legacy",
        log_key="invoice_bridge.link_invoice_to_case.rollback_legacy",
        log_window_seconds=300,
      )
    raise InvoiceBridgeError("Invoice  failed.") from exc
  external_number = row["number"]

  ipm_invoice.external_invoice_id = external_invoice_id
  ipm_invoice.external_invoice_number = external_number
  ipm_invoice.external_invoice_url = (
    external_url
    or ipm_invoice.external_invoice_url
    or _build_invoice_view_url(external_invoice_id)
  )
  try:
    db.session.add(ipm_invoice)
    if ExternalInvoiceCaseLink is not None:
      link = (
        db.session.query(ExternalInvoiceCaseLink)
        .filter(ExternalInvoiceCaseLink.external_invoice_id == int(external_invoice_id))
        .first()
      )
      if not link:
        link = ExternalInvoiceCaseLink(external_invoice_id=int(external_invoice_id))
        db.session.add(link)
      link.matter_id = str(ipm_invoice.matter_id)
      link.our_ref = ipm_case_ref
      link.external_invoice_number = external_number
      link.external_invoice_url = ipm_invoice.external_invoice_url
      link.ipm_invoice_id = str(ipm_invoice.invoice_id)
    db.session.commit()
  except SQLAlchemyError as exc:
    db.session.rollback()
    _restore_legacy_link(conn, invoice_id=external_invoice_id, prior=prior)
    raise InvoiceBridgeError(f"Internal Invoice Save failed: {exc}") from exc
  except Exception as exc:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.link_invoice_to_case.rollback_main_db",
        log_key="invoice_bridge.link_invoice_to_case.rollback_main_db",
        log_window_seconds=300,
      )
    _restore_legacy_link(conn, invoice_id=external_invoice_id, prior=prior)
    raise InvoiceBridgeError("Internal Invoice Save Error .") from exc

  return {
    "id": external_invoice_id,
    "number": external_number,
    "view_url": ipm_invoice.external_invoice_url,
    "ipm_case_id": ipm_invoice.matter_id,
  }


def unlink_invoice_case(ipm_invoice: "LegacyInvoice") -> None:
  if LegacyInvoice is None or not isinstance(ipm_invoice, LegacyInvoice):
    raise InvoiceBridgeError(" LegacyInvoice required.")
  external_id = ipm_invoice.external_invoice_id
  conn = None
  prior = {}
  if external_id:
    conn = _connect_legacy_db()
    cur = conn.cursor()
    try:
      legacy_row = cur.execute(
        "SELECT ipm_case_id, ipm_case_ref, ipm_invoice_id FROM invoices WHERE id=?",
        (int(external_id),),
      ).fetchone()
      if legacy_row:
        prior = _snapshot_legacy_link(legacy_row)
    except Exception:
      prior = {}
    cur.execute(
      "UPDATE invoices SET ipm_case_id=NULL, ipm_case_ref=NULL, ipm_invoice_id=NULL WHERE id=?",
      (int(external_id),),
    )
    try:
      conn.commit()
    except Exception as exc:
      try:
        conn.rollback()
      except Exception as rollback_exc:
        report_swallowed_exception(
          rollback_exc,
          context="invoice_bridge.unlink_invoice_from_case.rollback_legacy",
          log_key="invoice_bridge.unlink_invoice_from_case.rollback_legacy",
          log_window_seconds=300,
        )
      raise InvoiceBridgeError("Invoice  failed.") from exc
  ipm_invoice.external_invoice_id = None
  ipm_invoice.external_invoice_number = None
  ipm_invoice.external_invoice_url = None
  # Remove from main DB junction table too
  try:
    if ExternalInvoiceCaseLink is not None and external_id:
      (
        db.session.query(ExternalInvoiceCaseLink)
        .filter(ExternalInvoiceCaseLink.external_invoice_id == int(external_id))
        .delete(synchronize_session=False)
      )
    db.session.add(ipm_invoice)
    db.session.commit()
  except SQLAlchemyError as exc:
    db.session.rollback()
    if conn is not None:
      _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError(f"Internal Invoice Save failed: {exc}") from exc
  except Exception as exc:
    try:
      db.session.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="invoice_bridge.unlink_invoice_from_case.rollback_main_db",
        log_key="invoice_bridge.unlink_invoice_from_case.rollback_main_db",
        log_window_seconds=300,
      )
    if conn is not None:
      _restore_legacy_link(conn, invoice_id=int(external_id), prior=prior)
    raise InvoiceBridgeError("Internal Invoice Save Error .") from exc
