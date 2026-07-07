"""Matter-centric invoice link use case.

Routes should not coordinate Matter and billing bridge writes directly. This
module is the single entry point for linking/unlinking external invoices to a
canonical ``Matter.matter_id``.
"""

from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models.ip_records import Matter
from app.services.billing.invoice_bridge import (
  InvoiceBridgeError,
  fetch_external_invoice_links_for_case,
  link_legacy_invoice_to_case,
  unlink_legacy_invoice_from_case,
)
from app.services.case.case_audit_service import record_case_audit
from app.utils.error_logging import report_swallowed_exception


class InvoiceMatterLinkUseCase:
  """Matter-centric command API for external invoice links."""

  @staticmethod
  def _load_matter(matter_id: str) -> Matter:
    mid = (matter_id or "").strip()
    if not mid:
      raise InvoiceBridgeError("matter_id required.")
    matter = Matter.query.get(mid)
    if not matter:
      raise InvoiceBridgeError(f"matter_id '{mid}' Matter not found.")
    return matter

  @staticmethod
  def _record_audit(
    *,
    matter_id: str,
    action: str,
    actor_id: int | None,
    old_value: Any = None,
    new_value: Any = None,
  ) -> None:
    try:
      record_case_audit(
        case_id=matter_id,
        actor_user_id=actor_id,
        action=action,
        field_name="external_invoice_link",
        old_value=old_value,
        new_value=new_value,
      )
      db.session.commit()
    except Exception as exc:
      try:
        db.session.rollback()
      except Exception as rollback_exc:
        report_swallowed_exception(
          rollback_exc,
          context="invoice_matter_link_usecase.audit.rollback",
          log_key="invoice_matter_link_usecase.audit.rollback",
          log_window_seconds=300,
        )
      report_swallowed_exception(
        exc,
        context="invoice_matter_link_usecase.audit",
        log_key="invoice_matter_link_usecase.audit",
        log_window_seconds=300,
      )

  @classmethod
  def link(
    cls,
    *,
    matter_id: str,
    external_invoice_ref: str | int,
    actor_id: int | None = None,
  ) -> dict[str, Any]:
    matter = cls._load_matter(matter_id)
    mid = str(matter.matter_id)
    data = link_legacy_invoice_to_case(
      matter_id=mid,
      our_ref=str(getattr(matter, "our_ref", "") or "") or None,
      external_invoice_ref=external_invoice_ref,
    )
    result = dict(data or {})
    result["matter_id"] = mid
    cls._record_audit(
      matter_id=mid,
      action="BILLING_LINK",
      actor_id=actor_id,
      old_value=None,
      new_value={
        "external_invoice_ref": str(external_invoice_ref),
        "external_invoice_id": result.get("id"),
      },
    )
    return result

  @classmethod
  def unlink(
    cls,
    *,
    matter_id: str,
    external_invoice_ref: str | int,
    actor_id: int | None = None,
  ) -> None:
    matter = cls._load_matter(matter_id)
    mid = str(matter.matter_id)
    old_links = fetch_external_invoice_links_for_case(matter_id=mid)
    unlink_legacy_invoice_from_case(
      external_invoice_ref=external_invoice_ref,
      matter_id=mid,
      our_ref=str(getattr(matter, "our_ref", "") or "") or None,
      actor_id=actor_id,
    )
    cls._record_audit(
      matter_id=mid,
      action="BILLING_UNLINK",
      actor_id=actor_id,
      old_value={
        "external_invoice_ref": str(external_invoice_ref),
        "links": old_links,
      },
      new_value=None,
    )
