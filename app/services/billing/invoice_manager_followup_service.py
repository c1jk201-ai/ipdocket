from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import func, or_

from app.extensions import db
from app.models.case_flat_index import CaseFlatIndex
from app.models.docket import DocketItem
from app.models.matter import Matter
from app.models.legacy_finance import ExternalInvoiceCaseLink, ExternalInvoiceCaseMap
from app.models.workflow import Workflow
from app.services.billing.db_core import get_db, row_to_dict

_ALLOWED_ACTIONS = {
  "invoice.payment.verify",
  "invoice.tax_issued",
  "invoice.status_change",
}
_TERMINAL_WORKFLOW_STATUSES = {"COMPLETED", "ABANDONED"}
_REGISTRATION_NAME_REFS = {
  "MGMT:STATUS_RED:REGISTRATIONDEADLINE",
  "MGMT:STATUS_RED:REGISTRATIONDUE DATE",
  "MGMT:REGISTRATION",
}


def _parse_meta(meta: Any) -> dict[str, Any]:
  if isinstance(meta, dict):
    return meta
  if isinstance(meta, str):
    try:
      parsed = json.loads(meta)
    except Exception:
      return {}
    return parsed if isinstance(parsed, dict) else {}
  return {}


def _normalize_status(value: Any) -> str:
  return str(value or "").strip().lower()


def _load_invoice(invoice_id: int) -> dict[str, Any] | None:
  conn = get_db()
  try:
    row = conn.execute(
      """
      SELECT id, number, status, billing_status, payment_status, payment_verified,
          ipm_case_id, ipm_case_ref
      FROM invoices
      WHERE id=?
      """,
      (int(invoice_id),),
    ).fetchone()
    return row_to_dict(row) if row else None
  finally:
    conn.close()


def _payment_complete(invoice: dict[str, Any]) -> bool:
  payment_status = _normalize_status(invoice.get("payment_status"))
  if payment_status == "paid":
    return True
  try:
    return int(invoice.get("payment_verified") or 0) == 1
  except Exception:
    return False


def _billing_status(invoice: dict[str, Any]) -> str:
  billing = _normalize_status(invoice.get("billing_status"))
  if billing:
    return billing
  return _normalize_status(invoice.get("status"))


def _should_trigger_for_action(
  *, action: str, meta: dict[str, Any], invoice: dict[str, Any]
) -> bool:
  if action not in _ALLOWED_ACTIONS:
    return False
  if action == "invoice.payment.verify":
    return meta.get("ok") is True
  if action == "invoice.tax_issued":
    return True
  if action == "invoice.status_change":
    new_status = _normalize_status(meta.get("new_status"))
    return new_status == "tax_issued" or _billing_status(invoice) == "tax_issued"
  return False


def _invoice_is_followup_ready(invoice: dict[str, Any]) -> bool:
  billing = _billing_status(invoice)
  if billing in {"", "draft", "void"}:
    return False
  return _payment_complete(invoice) or billing == "tax_issued"


def _resolve_matter_ids(invoice_id: int, invoice: dict[str, Any]) -> list[str]:
  matter_ids: list[str] = []
  seen: set[str] = set()

  def _add(value: Any) -> None:
    raw = str(value or "").strip()
    if not raw or raw in seen:
      return
    seen.add(raw)
    matter_ids.append(raw)

  rows = (
    db.session.query(ExternalInvoiceCaseMap.matter_id)
    .filter(ExternalInvoiceCaseMap.external_invoice_id == int(invoice_id))
    .filter(
      (ExternalInvoiceCaseMap.is_deleted.is_(False))
      | (ExternalInvoiceCaseMap.is_deleted.is_(None))
    )
    .all()
  )
  for row in rows:
    _add(getattr(row, "matter_id", None))

  link_rows = (
    db.session.query(ExternalInvoiceCaseLink.matter_id)
    .filter(ExternalInvoiceCaseLink.external_invoice_id == int(invoice_id))
    .filter(
      (ExternalInvoiceCaseLink.is_deleted.is_(False))
      | (ExternalInvoiceCaseLink.is_deleted.is_(None))
    )
    .all()
  )
  for row in link_rows:
    _add(getattr(row, "matter_id", None))

  _add(invoice.get("ipm_case_id"))
  if not matter_ids:
    our_ref = str(invoice.get("ipm_case_ref") or "").strip()
    if our_ref:
      row = (
        db.session.query(Matter.matter_id)
        .filter(Matter.our_ref == our_ref)
        .filter((Matter.is_deleted.is_(False)) | (Matter.is_deleted.is_(None)))
        .first()
      )
      if row:
        _add(getattr(row, "matter_id", None))

  if not matter_ids:
    return []

  valid_rows = (
    db.session.query(Matter.matter_id)
    .filter(Matter.matter_id.in_(matter_ids))
    .filter((Matter.is_deleted.is_(False)) | (Matter.is_deleted.is_(None)))
    .all()
  )
  valid_ids = {
    str(row.matter_id).strip() for row in valid_rows if getattr(row, "matter_id", None)
  }
  return [matter_id for matter_id in matter_ids if matter_id in valid_ids]


def _is_open_docket(docket_item: DocketItem) -> bool:
  if bool(getattr(docket_item, "is_deleted", False)):
    return False
  return not str(getattr(docket_item, "done_date", "") or "").strip()


def _is_registration_docket(docket_item: DocketItem) -> bool:
  ref = str(getattr(docket_item, "name_ref", "") or "").strip().upper()
  if ref in _REGISTRATION_NAME_REFS:
    return True
  title_compact = "".join(str(getattr(docket_item, "name_free", "") or "").split()).casefold()
  return "registrationdeadline" in title_compact


def _registration_docket_priority(docket_item: DocketItem) -> tuple[int, str]:
  ref = str(getattr(docket_item, "name_ref", "") or "").strip().upper()
  if ref.startswith("MGMT:STATUS_RED:"):
    return (0, ref)
  if ref == "MGMT:REGISTRATION":
    return (1, ref)
  return (2, ref)


def _open_registration_dockets(matter_id: str) -> list[DocketItem]:
  rows = (
    DocketItem.query.filter(DocketItem.matter_id == matter_id)
    .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
    .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
    .all()
  )
  dockets = [row for row in rows if _is_open_docket(row) and _is_registration_docket(row)]
  return sorted(
    dockets,
    key=lambda row: (
      _registration_docket_priority(row),
      str(getattr(row, "extended_due_date", "") or getattr(row, "due_date", "") or ""),
      str(getattr(row, "docket_id", "") or ""),
    ),
  )


def _normalize_user_id(value: Any) -> int | None:
  try:
    user_id = int(value)
  except (TypeError, ValueError):
    return None
  return user_id if user_id > 0 else None


def _manager_id_for_matter(matter_id: str) -> int | None:
  idx = CaseFlatIndex.query.get(matter_id)
  if not idx:
    return None
  return _normalize_user_id(getattr(idx, "manager_id", None))


def _workflow_rank(
  workflow: Workflow,
  *,
  docket_rank: tuple[int, str],
  manager_id: int | None,
) -> tuple[int, int, int, int, date, int]:
  inspector_id = _normalize_user_id(getattr(workflow, "inspector_id", None))
  assignee_id = _normalize_user_id(getattr(workflow, "assignee_id", None))
  attorney_id = _normalize_user_id(getattr(workflow, "attorney_assignee_id", None))
  category = str(getattr(workflow, "category", "") or "").strip().upper()
  manager_only_rank = 0 if inspector_id and not assignee_id and not attorney_id else 1
  manager_match_rank = 0 if manager_id and inspector_id == manager_id else 1
  category_rank = 0 if category == "MGMT" else (1 if "MGMT" in category else 2)
  due_date = getattr(workflow, "due_date", None) or date.max
  workflow_id = int(getattr(workflow, "id", 0) or 0)
  return (
    docket_rank[0],
    manager_only_rank,
    manager_match_rank,
    category_rank,
    due_date,
    workflow_id,
  )


def _pick_registration_manager_workflow(matter_id: str) -> Workflow | None:
  manager_id = _manager_id_for_matter(matter_id)
  best_workflow: Workflow | None = None
  best_rank: tuple[int, int, int, int, date, int] | None = None

  for docket_item in _open_registration_dockets(matter_id):
    docket_id = str(getattr(docket_item, "docket_id", "") or "").strip()
    if not docket_id:
      continue
    workflows = (
      Workflow.query.filter(Workflow.case_id == matter_id)
      .filter(Workflow.business_code.like(f"DOCKET:{docket_id}%"))
      .filter(
        or_(
          Workflow.status.is_(None),
          func.upper(func.trim(func.coalesce(Workflow.status, ""))).notin_(
            tuple(_TERMINAL_WORKFLOW_STATUSES)
          ),
        )
      )
      .all()
    )
    docket_rank = _registration_docket_priority(docket_item)
    for workflow in workflows:
      inspector_id = _normalize_user_id(getattr(workflow, "inspector_id", None))
      category = str(getattr(workflow, "category", "") or "").strip().upper()
      if inspector_id is None and "MGMT" not in category:
        continue
      rank = _workflow_rank(workflow, docket_rank=docket_rank, manager_id=manager_id)
      if best_rank is None or rank < best_rank:
        best_rank = rank
        best_workflow = workflow

  if best_workflow is not None:
    return best_workflow

  fallback_rows = (
    Workflow.query.filter(Workflow.case_id == matter_id)
    .filter(
      or_(
        Workflow.status.is_(None),
        func.upper(func.trim(func.coalesce(Workflow.status, ""))).notin_(
          tuple(_TERMINAL_WORKFLOW_STATUSES)
        ),
      )
    )
    .all()
  )
  fallback_candidates = []
  for workflow in fallback_rows:
    title_compact = "".join(str(getattr(workflow, "name", "") or "").split()).casefold()
    if "registrationdeadline" not in title_compact:
      continue
    inspector_id = _normalize_user_id(getattr(workflow, "inspector_id", None))
    category = str(getattr(workflow, "category", "") or "").strip().upper()
    if inspector_id is None and "MGMT" not in category:
      continue
    fallback_candidates.append(workflow)

  if not fallback_candidates:
    return None

  fallback_candidates.sort(
    key=lambda workflow: _workflow_rank(
      workflow,
      docket_rank=(9, ""),
      manager_id=manager_id,
    )
  )
  return fallback_candidates[0]


def _invoice_open_url(invoice_id: int) -> str | None:
  from flask import current_app

  base = str(current_app.config.get("INVOICE_MODULE_VIEW_BASE_URL", "") or "").strip()
  if not base:
    return None
  return f"{base.rstrip('/')}/{int(invoice_id)}"


def _build_followup_summary(*, invoice: dict[str, Any]) -> str:
  invoice_id = int(invoice.get("id") or 0)
  number = str(invoice.get("number") or f"#{invoice_id}").strip()
  statuses: list[str] = []
  if _payment_complete(invoice):
    statuses.append("Paid")
  billing = _billing_status(invoice)
  if billing == "tax_issued":
    statuses.append("Tax recorded")
  status_text = ", ".join(statuses) if statuses else "Open "
  invoice_url = _invoice_open_url(invoice_id)
  invoice_label = f"Invoice {number}"
  invoice_text = f"<{invoice_url}|{invoice_label}>" if invoice_url else invoice_label
  return f"{invoice_text} {status_text}. Registration Open Confirm required."


def _followup_notice_exists(*, invoice_id: int, matter_id: str) -> bool:
  matter_token = json.dumps({"matter_id": matter_id}, ensure_ascii=False, separators=(",", ":"))[
    1:-1
  ]
  conn = get_db()
  try:
    row = conn.execute(
      """
      SELECT 1
      FROM audit_log
      WHERE action=?
       AND target_type='invoice'
       AND target_id=?
       AND meta LIKE ?
      LIMIT 1
      """,
      (
        "invoice.manager_followup_notice",
        int(invoice_id),
        f"%{matter_token}%",
      ),
    ).fetchone()
    return bool(row)
  finally:
    conn.close()


def _record_followup_notice(
  *,
  invoice_id: int,
  matter_id: str,
  workflow_id: int,
  action: str,
  actor_id: int | None,
) -> None:
  meta = json.dumps(
    {
      "matter_id": matter_id,
      "workflow_id": int(workflow_id),
      "source_action": action,
    },
    ensure_ascii=False,
    separators=(",", ":"),
  )
  conn = get_db()
  try:
    conn.execute(
      """
      INSERT INTO audit_log (actor_id, user_id, action, target_type, target_id, meta)
      VALUES (?, ?, ?, 'invoice', ?, ?)
      """,
      (
        actor_id,
        actor_id,
        "invoice.manager_followup_notice",
        int(invoice_id),
        meta,
      ),
    )
    conn.commit()
  finally:
    conn.close()


def maybe_notify_manager_followup_for_invoice(
  *,
  action: str,
  invoice_id: int | None,
  meta: Any = None,
  actor_id: int | None = None,
) -> dict[str, Any]:
  if not invoice_id or action not in _ALLOWED_ACTIONS:
    return {"status": "skipped", "reason": "action"}

  invoice = _load_invoice(int(invoice_id))
  if not invoice:
    return {"status": "skipped", "reason": "missing_invoice"}
  if not _invoice_is_followup_ready(invoice):
    return {"status": "skipped", "reason": "not_ready"}

  parsed_meta = _parse_meta(meta)
  if not _should_trigger_for_action(action=action, meta=parsed_meta, invoice=invoice):
    return {"status": "skipped", "reason": "trigger"}

  matter_ids = _resolve_matter_ids(int(invoice_id), invoice)
  if not matter_ids:
    return {"status": "skipped", "reason": "missing_matter"}


  sent = 0
  notified_workflow_ids: list[int] = []
  for matter_id in matter_ids:
    if _followup_notice_exists(invoice_id=int(invoice_id), matter_id=matter_id):
      continue
    workflow = _pick_registration_manager_workflow(matter_id)
    if not workflow:
      continue
    _record_followup_notice(
      invoice_id=int(invoice_id),
      matter_id=matter_id,
      workflow_id=int(workflow.id),
      action=action,
      actor_id=actor_id,
    )
    sent += 1
    notified_workflow_ids.append(int(workflow.id))

  return {"status": "ok", "sent": sent, "workflow_ids": notified_workflow_ids}
