from __future__ import annotations

from typing import Any

from app.utils.error_logging import report_swallowed_exception

from ..db import _create_index_if_possible, _ensure_column, row_get

GENERAL_ATTACHMENT_ROLE = "general"
FOREIGN_REMITTANCE_PROOF_ROLE = "foreign_remittance_proof"


def normalize_invoice_attachment_role(value: Any) -> str:
  raw = str(value or "").strip().lower()
  if raw in {
    FOREIGN_REMITTANCE_PROOF_ROLE,
    "remittance",
    "foreign_remittance",
    "remittance_proof",
    "overseas_remittance_proof",
    "ForeignRemittance proof",
  }:
    return FOREIGN_REMITTANCE_PROOF_ROLE
  return GENERAL_ATTACHMENT_ROLE


def ensure_invoice_attachment_role_schema(conn) -> None:
  try:
    _ensure_column(conn, "invoice_attachments", "role", "TEXT DEFAULT 'general'")
    _create_index_if_possible(conn, "idx_attach_role", "invoice_attachments", "role")
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.remittance_proof.ensure_invoice_attachment_role_schema",
      log_key="billing_invoices.remittance_proof.ensure_invoice_attachment_role_schema",
      log_window_seconds=300,
    )


def _outgoing_matter_sql(alias: str) -> str:
  return (
    "("
    f"UPPER(TRIM(COALESCE({alias}.right_group, ''))) "
    "IN ('OUT', 'OUTGOING', 'OUTBOUND', 'FOREIGN') "
    f"OR TRIM(COALESCE({alias}.right_group, '')) "
    "IN ('Foreign', 'Foreign', '', '')"
    ")"
  )


def _not_deleted_sql(column_expr: str) -> str:
  return (
    f"COALESCE(LOWER(CAST({column_expr} AS TEXT)), 'false') "
    "NOT IN ('1', 'true', 't', 'yes', 'y')"
  )


def outgoing_invoice_filter_sql(invoice_alias: str = "invoices") -> str:
  matter_from_map = _outgoing_matter_sql("m_outgoing_filter")
  matter_from_primary = _outgoing_matter_sql("m_primary_outgoing_filter")
  matter_from_ref = _outgoing_matter_sql("m_ref_outgoing_filter")
  return f"""
    (
     COALESCE({invoice_alias}.is_outgoing, 0) = 1
     OR EXISTS (
      SELECT 1
       FROM external_invoice_case_map eicm_outgoing_filter
       JOIN matter m_outgoing_filter
        ON m_outgoing_filter.matter_id = eicm_outgoing_filter.matter_id
       WHERE eicm_outgoing_filter.external_invoice_id = {invoice_alias}.id
        AND {_not_deleted_sql("eicm_outgoing_filter.is_deleted")}
        AND {matter_from_map}
     )
     OR EXISTS (
      SELECT 1
       FROM matter m_primary_outgoing_filter
       WHERE m_primary_outgoing_filter.matter_id = {invoice_alias}.ipm_case_id
        AND {matter_from_primary}
     )
     OR EXISTS (
      SELECT 1
       FROM matter m_ref_outgoing_filter
       WHERE TRIM(COALESCE({invoice_alias}.ipm_case_ref, '')) <> ''
        AND UPPER(TRIM(COALESCE(m_ref_outgoing_filter.our_ref, '')))
          = UPPER(TRIM(COALESCE({invoice_alias}.ipm_case_ref, '')))
        AND {matter_from_ref}
     )
    )
  """


def invoice_requires_foreign_remittance_proof(
  conn,
  invoice_id: int,
  invoice: Any | None = None,
) -> bool:
  try:
    if int(row_get(invoice, "is_outgoing", default=0) or 0) == 1:
      return True
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.remittance_proof.invoice_requires_foreign_remittance_proof.flag",
      log_key="billing_invoices.remittance_proof.invoice_requires_foreign_remittance_proof.flag",
      log_window_seconds=300,
    )

  try:
    row = conn.execute(
      f"""
      SELECT 1
       FROM invoices
       WHERE invoices.id=?
        AND {outgoing_invoice_filter_sql("invoices")}
       LIMIT 1
      """,
      (int(invoice_id),),
    ).fetchone()
    return bool(row)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.remittance_proof.invoice_requires_foreign_remittance_proof",
      log_key="billing_invoices.remittance_proof.invoice_requires_foreign_remittance_proof",
      log_window_seconds=300,
    )
  return False


def invoice_has_foreign_remittance_proof(conn, invoice_id: int) -> bool:
  ensure_invoice_attachment_role_schema(conn)
  try:
    row = conn.execute(
      """
      SELECT 1
       FROM invoice_attachments
       WHERE invoice_id=?
        AND COALESCE(role, 'general')=?
       LIMIT 1
      """,
      (int(invoice_id), FOREIGN_REMITTANCE_PROOF_ROLE),
    ).fetchone()
    return bool(row)
  except Exception as exc:
    report_swallowed_exception(
      exc,
      context="billing_invoices.remittance_proof.invoice_has_foreign_remittance_proof",
      log_key="billing_invoices.remittance_proof.invoice_has_foreign_remittance_proof",
      log_window_seconds=300,
    )
  return False


def missing_foreign_remittance_proof(
  conn,
  invoice_id: int,
  invoice: Any | None = None,
) -> bool:
  if not invoice_requires_foreign_remittance_proof(conn, invoice_id, invoice):
    return False
  return not invoice_has_foreign_remittance_proof(conn, invoice_id)


def foreign_remittance_required_message() -> str:
  return "Foreign matters require remittance proof before final payment verification."
