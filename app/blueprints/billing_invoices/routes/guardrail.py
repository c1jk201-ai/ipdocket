from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models.billing_guardrail import BillingGuardrailFinding
from app.services.billing.guardrail_service import (
  list_guardrail_findings,
  summarize_guardrail_findings,
  sync_guardrail_findings,
)
from app.utils.error_logging import report_swallowed_exception

bp = Blueprint("guardrail", __name__)

TYPE_LABELS = {
  "unbilled_expense": "Billing ",
  "underbilled_expense": "Billing ",
  "uncollected_invoice": "Collection Invoice",
  "billable_workflow_without_invoice": "DoneTask BillingConfirm",
  "billable_event_without_invoice": "Matter BillingConfirm",
}

STATUS_LABELS = {
  BillingGuardrailFinding.STATUS_OPEN: "column",
  BillingGuardrailFinding.STATUS_REVIEWING: "",
  BillingGuardrailFinding.STATUS_RESOLVED: "",
  BillingGuardrailFinding.STATUS_DISMISSED: "",
}

SEVERITY_LABELS = {
  "high": "",
  "medium": "",
  "low": "",
}


def _safe_int(value, default: int) -> int:
  try:
    parsed = int(str(value or "").strip() or default)
  except Exception:
    parsed = default
  return max(1, min(parsed, 1000))


@bp.route("")
@bp.route("/")
def index():
  if request.args.get("refresh", "1") != "0":
    try:
      sync_result = sync_guardrail_findings(
        limit_per_source=_safe_int(request.args.get("scan_limit"), 300)
      )
    except Exception as exc:
      db.session.rollback()
      sync_result = None
      report_swallowed_exception(
        exc,
        context="billing_guardrail.index.sync",
        log_key="billing_guardrail.index.sync",
        log_window_seconds=300,
      )
      flash("Guardrail Auto  Error .", "warning")
  else:
    sync_result = None

  status = (request.args.get("status") or "open").strip().lower()
  finding_type = (request.args.get("type") or "").strip()
  severity = (request.args.get("severity") or "").strip().lower()
  q = (request.args.get("q") or "").strip()
  rows = list_guardrail_findings(
    status=status,
    finding_type=finding_type,
    severity=severity,
    q=q,
    limit=_safe_int(request.args.get("limit"), 200),
  )
  summary = summarize_guardrail_findings()
  return render_template(
    "billing_invoices/guardrail.html",
    rows=rows,
    summary=summary,
    sync_result=sync_result,
    type_labels=TYPE_LABELS,
    status_labels=STATUS_LABELS,
    severity_labels=SEVERITY_LABELS,
    selected_status=status,
    selected_type=finding_type,
    selected_severity=severity,
    q=q,
  )


@bp.route("/refresh", methods=["POST"])
def refresh():
  try:
    result = sync_guardrail_findings(
      limit_per_source=_safe_int(request.form.get("scan_limit"), 500)
    )
    flash(f"Guardrail Done: {result.scanned}items Confirm, {result.created}items .", "success")
  except Exception as exc:
    db.session.rollback()
    report_swallowed_exception(
      exc,
      context="billing_guardrail.refresh",
      log_key="billing_guardrail.refresh",
      log_window_seconds=300,
    )
    flash("Guardrail failed.", "danger")
  return redirect(url_for("billing_invoices.guardrail.index", refresh=0))


@bp.route("/<int:finding_id>/status", methods=["POST"])
def update_status(finding_id: int):
  row = db.session.get(BillingGuardrailFinding, finding_id)
  if not row:
    abort(404)
  new_status = (request.form.get("status") or "").strip().lower()
  if new_status not in STATUS_LABELS:
    abort(400)
  note = (request.form.get("resolution_note") or "").strip()
  row.status = new_status
  row.resolution_note = note or row.resolution_note
  if new_status in {
    BillingGuardrailFinding.STATUS_RESOLVED,
    BillingGuardrailFinding.STATUS_DISMISSED,
  }:
    row.resolved_at = datetime.utcnow()
    row.resolved_by = getattr(current_user, "id", None)
  else:
    row.resolved_at = None
    row.resolved_by = None
  db.session.commit()
  flash("Guardrail Item Status Change.", "success")
  return redirect(request.referrer or url_for("billing_invoices.guardrail.index", refresh=0))
