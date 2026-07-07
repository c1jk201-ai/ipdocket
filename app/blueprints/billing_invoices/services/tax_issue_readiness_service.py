from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..db import row_to_dict

FINALIZED_BILLING_STATUSES = {"tax_issued", "cash_issued", "processed"}


def _not_deleted_sql(column_expr: str) -> str:
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


@dataclass(frozen=True)
class TaxIssueReadiness:
  ready: bool
  reasons: list[str]
  invoice_id: int | None
  case_matched: bool
  payment_verified: bool
  status_blocked: bool


def _coerce_invoice_dict(invoice_row: Any) -> dict[str, Any]:
  try:
    return row_to_dict(invoice_row)
  except Exception:
    if isinstance(invoice_row, dict):
      return invoice_row
  return {}


def evaluate_tax_issue_readiness(
  conn,
  invoice_row: Any,
  *,
  ensure_case_map: Callable[[Any], None] | None = None,
) -> TaxIssueReadiness:
  """Return readiness gates before confirming manually recorded tax documentation.

  A blank invoice row is treated as ready for compatibility with legacy callers
  that may still render standalone tax-invoice screens.
  """
  if not invoice_row:
    return TaxIssueReadiness(
      ready=True,
      reasons=[],
      invoice_id=None,
      case_matched=True,
      payment_verified=True,
      status_blocked=False,
    )

  invoice = _coerce_invoice_dict(invoice_row)
  try:
    invoice_id = int(invoice.get("id") or 0)
  except Exception:
    invoice_id = None

  if not invoice_id:
    return TaxIssueReadiness(
      ready=True,
      reasons=[],
      invoice_id=None,
      case_matched=True,
      payment_verified=True,
      status_blocked=False,
    )

  if ensure_case_map is not None:
    ensure_case_map(conn)

  case_matched = False
  try:
    row = conn.execute(
      f"""
      SELECT 1
      FROM external_invoice_case_map
      WHERE external_invoice_id=?
       AND {_not_deleted_sql("is_deleted")}
      LIMIT 1
      """,
      (invoice_id,),
    ).fetchone()
    case_matched = bool(row)
  except Exception:
    case_matched = False

  billing_status = str(invoice.get("billing_status") or "").strip().lower()
  legacy_status = str(invoice.get("status") or "").strip().lower()
  payment_status = str(invoice.get("payment_status") or "").strip().lower()
  try:
    payment_verified = int(invoice.get("payment_verified") or 0) == 1
  except Exception:
    payment_verified = bool(invoice.get("payment_verified"))
  payment_verified = payment_verified or payment_status == "paid" or legacy_status == "paid"
  status_blocked = (
    billing_status in FINALIZED_BILLING_STATUSES or legacy_status in FINALIZED_BILLING_STATUSES
  )

  reasons: list[str] = []
  if status_blocked:
    reasons.append("Billing status Change(Tax recorded )")
  if not case_matched:
    reasons.append("Matter Matching")
  if not payment_verified:
    reasons.append("Payment verification")

  return TaxIssueReadiness(
    ready=(not status_blocked) and case_matched and payment_verified,
    reasons=reasons,
    invoice_id=invoice_id,
    case_matched=case_matched,
    payment_verified=payment_verified,
    status_blocked=status_blocked,
  )
