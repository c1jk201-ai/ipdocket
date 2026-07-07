from __future__ import annotations

from typing import Any

from flask import abort, jsonify, render_template, request

from app.utils.error_logging import report_swallowed_exception

from ..db import get_db, row_get, row_to_dict
from ..repos.invoice_case_repo import link_case_to_invoice, resolve_matter_identifier
from .invoices import _fetch_invoice_audit_rows, _parse_invoice_audit_meta, bp

SCOPE_ALL = "all"
SCOPE_BILLING = "billing"
SCOPE_PAYMENT = "payment"
VALID_LOG_SCOPES = {SCOPE_ALL, SCOPE_BILLING, SCOPE_PAYMENT}

SCOPE_ACTIONS: dict[str, tuple[str, ...]] = {
  SCOPE_BILLING: (
    "invoice.status_change",
    "invoice.tax_issued",
    "invoice.publish",
    "invoice.create",
  ),
  SCOPE_PAYMENT: (
    "invoice.payment.verify",
    "invoice.payment_meta.save",
    "invoice.payment.force_paid",
    "invoice.payment.unverify",
    "invoice.mark_paid",
  ),
}
SCOPE_BULK_MODE: dict[str, str] = {
  SCOPE_BILLING: "billing",
  SCOPE_PAYMENT: "payment",
}

PRETTY_META_SIMPLE_ACTION_LABELS = {
  "invoice.tax_issued": "Tax documentation recorded",
  "invoice.payment_meta.save": "Payment saved",
  "invoice.payment.force_paid": "Administrator marked paid",
  "invoice.payment.unverify": "Payment verification reopened",
  "invoice.mark_paid": "Marked paid",
}

CASE_AUTO_LINK_UNCHANGED = {"status": "skipped", "reason": "unchanged_or_not_applicable"}
CASE_AUTO_LINK_ALREADY = {"status": "skipped", "reason": "already_linked"}


def _normalize_scope(raw_scope: str | None) -> str:
  scope = (raw_scope or SCOPE_ALL).strip().lower()
  return scope if scope in VALID_LOG_SCOPES else SCOPE_ALL


def _build_action_filter_clause(scope: str) -> tuple[str, list[str]]:
  actions = SCOPE_ACTIONS.get(scope) or ()
  if not actions:
    return "", []
  placeholders = ",".join("?" for _ in actions)
  return f" AND a.action IN ({placeholders}) ", list(actions)


def _build_bulk_like_query(scope: str, invoice_number: str) -> tuple[str, list[str]]:
  del invoice_number
  sql = """
    UNION ALL
    SELECT a.*, u.username
    FROM audit_log a
    LEFT JOIN users u ON u.id = a.user_id
    WHERE a.action='invoice.bulk_status_change'
  """
  params: list[str] = []
  mode = SCOPE_BULK_MODE.get(scope)
  if mode:
    sql += " AND a.meta LIKE ?"
    params.append(f'%"mode": "{mode}"%')
  return sql, params


def _parse_meta_dict(meta_str: str | None) -> dict[str, Any] | None:
  return _parse_invoice_audit_meta(meta_str)


def _direct_actions_for_scope(scope: str) -> tuple[str, ...]:
  if scope == SCOPE_ALL:
    actions: list[str] = []
    for scoped_actions in SCOPE_ACTIONS.values():
      actions.extend(scoped_actions)
    return tuple(actions)
  return SCOPE_ACTIONS.get(scope) or ()


def _build_pretty_meta(action: str | None, meta_str: str | None) -> str:
  action_key = (action or "").strip()
  meta = _parse_meta_dict(meta_str)

  if action_key == "invoice.status_change":
    if isinstance(meta, dict):
      old_s = meta.get("old_status")
      new_s = meta.get("new_status")
      if old_s or new_s:
        return f"Status: {old_s or '-'} -> {new_s or '-'}"
    return ""

  if action_key == "invoice.publish":
    if isinstance(meta, dict) and meta.get("to_status"):
      return f"Issued: {meta.get('to_status')}"
    return "Issued Process"

  if action_key == "invoice.create":
    if isinstance(meta, dict):
      created_by = meta.get("created_by_username")
      return "Draft" + (f" - {created_by}" if created_by else "")
    return "Draft "

  if action_key == "invoice.payment.verify":
    if isinstance(meta, dict):
      return " success" if meta.get("ok") else f" : {meta.get('reason', '')}"
    return "Payment verification"

  if action_key == "invoice.bulk_status_change":
    if isinstance(meta, dict):
      mode = meta.get("mode")
      new_s = meta.get("new_status")
      if mode and new_s:
        mode_label = "Payment" if mode == SCOPE_PAYMENT else "Billing"
        return f"Bulk change - {mode_label}: {new_s}"
    return "Bulk change"

  if action_key in PRETTY_META_SIMPLE_ACTION_LABELS:
    return PRETTY_META_SIMPLE_ACTION_LABELS[action_key]

  return meta_str or ""


def _fetch_invoice_primary_fields(conn, invoice_id: int):
  try:
    return conn.execute(
      "SELECT ipm_case_id, ipm_case_ref FROM invoices WHERE id=?",
      (invoice_id,),
    ).fetchone()
  except Exception:
    return None


def _has_primary_case_fields(invoice_row) -> bool:
  if invoice_row is None:
    return False
  ipm_case_id = str(row_get(invoice_row, "ipm_case_id", 0) or "").strip()
  ipm_case_ref = str(row_get(invoice_row, "ipm_case_ref", 1) or "").strip()
  return bool(ipm_case_id or ipm_case_ref)


def _has_external_case_links(conn, invoice_id: int) -> bool:
  try:
    return bool(
      conn.execute(
        """
        SELECT 1
        FROM external_invoice_case_map
        WHERE external_invoice_id=?
         AND COALESCE(LOWER(CAST(is_deleted AS TEXT)), 'false')
           NOT IN ('1', 'true', 't', 'yes', 'y')
        LIMIT 1
        """,
        (invoice_id,),
      ).fetchone()
    )
  except Exception:
    return False


def _resolve_case_auto_link_for_internal_ref(
  conn, *, invoice_id: int, internal_reference: str
) -> dict[str, Any]:
  if not internal_reference:
    return dict(CASE_AUTO_LINK_UNCHANGED)

  invoice_row = _fetch_invoice_primary_fields(conn, invoice_id)
  has_primary = _has_primary_case_fields(invoice_row)
  has_links = _has_external_case_links(conn, invoice_id)
  if has_primary or has_links:
    return dict(CASE_AUTO_LINK_ALREADY)

  resolved = resolve_matter_identifier(conn, internal_reference)
  status = str(resolved.get("status") or "").strip()
  if status == "ok":
    matter_id = str(resolved.get("matter_id") or "").strip()
    if not matter_id:
      return {"status": "skipped", "reason": "empty"}
    linked = link_case_to_invoice(conn, invoice_id=invoice_id, matter_id=matter_id)
    if linked:
      return {
        "status": "linked",
        "matter_id": matter_id,
        "our_ref": str(resolved.get("our_ref") or "").strip() or matter_id,
        "source": "internal_reference",
      }
    return {"status": "skipped", "reason": "link_failed"}
  if status == "ambiguous":
    return {"status": "skipped", "reason": "ambiguous"}
  if status == "not_found":
    return {"status": "skipped", "reason": "not_found"}
  return {"status": "skipped", "reason": "empty"}


@bp.route("/<int:invoice_id>/logs")
def invoice_logs(invoice_id):
  """Recent invoice logs for scope=all, billing, or payment.

  Billing logs cover status changes and tax documentation.
  Payment logs cover verification, payment, save, and bulk updates.
  """
  scope = _normalize_scope(request.args.get("scope"))
  conn = get_db()
  try:
    invoice = conn.execute(
      """
      SELECT invoices.id, invoices.number, invoices.client_id,
          invoices.ipm_case_id, invoices.ipm_case_ref,
          clients.name AS client_name
       FROM invoices
       LEFT JOIN clients ON clients.id = invoices.client_id
       WHERE invoices.id=?
      """,
      (invoice_id,),
    ).fetchone()
    if not invoice:
      abort(404)
    invoice = row_to_dict(invoice)
    invoice_number = str(invoice.get("number") or "")
    rows = _fetch_invoice_audit_rows(
      conn,
      invoice_id=int(invoice_id),
      invoice_number=invoice_number,
      direct_actions=_direct_actions_for_scope(scope),
      bulk_mode=SCOPE_BULK_MODE.get(scope),
    )
  finally:
    conn.close()

  logs = []
  for r in rows:
    d = row_to_dict(r)
    d["pretty_meta"] = _build_pretty_meta(d.get("action"), d.get("meta"))
    # Hide redundant number from meta; already in page context
    logs.append(d)
  return render_template(
    "invoice_logs.html",
    invoice=invoice,
    logs=logs,
    scope=scope,
  )


@bp.route("/<int:invoice_id>/update_internal_ref", methods=["POST"])
def update_internal_ref(invoice_id):
  """Internal to AJAX """
  data = request.get_json(silent=True) or {}
  internal_reference = str(data.get("internal_reference") or "").strip()

  conn = get_db()
  case_auto_link = dict(CASE_AUTO_LINK_UNCHANGED)
  try:
    conn.execute(
      "UPDATE invoices SET internal_reference=? WHERE id=?",
      (internal_reference if internal_reference else None, invoice_id),
    )

    # Safety-first auto-link:
    # - only when invoice has no existing case links and no primary case fields
    # - only when internal_reference resolves to exactly one matter
    case_auto_link = _resolve_case_auto_link_for_internal_ref(
      conn,
      invoice_id=int(invoice_id),
      internal_reference=internal_reference,
    )
    conn.commit()
  except Exception as exc:
    try:
      conn.rollback()
    except Exception as rollback_exc:
      report_swallowed_exception(
        rollback_exc,
        context="billing_invoices.invoices_logs.update_internal_ref.rollback",
        log_key="billing_invoices.invoices_logs.update_internal_ref.rollback",
        log_window_seconds=300,
      )
    report_swallowed_exception(
      exc,
      context="billing_invoices.invoices_logs.update_internal_ref",
      log_key="billing_invoices.invoices_logs.update_internal_ref",
      log_window_seconds=300,
    )
    return jsonify({"success": False, "error": "internal_reference_update_failed"}), 500
  finally:
    conn.close()

  return (
    jsonify(
      {
        "success": True,
        "internal_reference": internal_reference,
        "case_auto_link": case_auto_link,
      }
    ),
    200,
  )


@bp.route("/<int:invoice_id>/update_admin_memo", methods=["POST"])
def update_admin_memo(invoice_id):
  """ Notes to AJAX """
  data = request.get_json(silent=True) or {}
  admin_memo = str(data.get("admin_memo") or "").strip()

  conn = get_db()
  try:
    conn.execute(
      "UPDATE invoices SET admin_memo=? WHERE id=?",
      (admin_memo if admin_memo else None, invoice_id),
    )
    conn.commit()
  finally:
    conn.close()

  return jsonify({"success": True, "admin_memo": admin_memo}), 200
