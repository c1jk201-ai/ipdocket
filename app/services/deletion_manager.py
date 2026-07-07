from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from flask import has_request_context
from flask_login import current_user
from sqlalchemy import and_, event, func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.models.annuity import AnnuityItem
from app.models.assets import FileAsset, MatterFileAsset
from app.models.deadline import Deadline, RenewalFee
from app.models.deletion_log import DeletionLog
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterMemoFileAsset, MatterPartyRole, MatterStaffAssignment
from app.models.party import Party, PartyAddress, PartyCode, PartyContact
from app.models.legacy_finance import (
    CaseExpenseInvoiceMap,
    ExternalInvoiceCaseLink,
    ExternalInvoiceCaseMap,
    LegacyExpense,
    LegacyExpensePayment,
    LegacyInvoice,
    LegacyInvoicePayment,
)
from app.models.user import User
from app.models.workflow import Workflow
from app.services.annuity.annuity_service import revive_soft_deleted_annuity_item
from app.services.case.legacy_case_adapter import LegacyCaseAdapter
from app.services.workflow.sync_requests import enqueue_annuity_sync_for_item
from app.utils.error_logging import report_swallowed_exception

_MODULE_INITIALIZED = False
_SUPPRESS_KEY = "deletion_manager_suppress_auto_archive"

_ENTITY_LABELS = {
    "deadline": "StatutoryDeadline",
    "renewal": "Updated",
    "workflow": "Task",
    "file": "File",
    "person": "",
    "billing_invoice": "Billing",
    "billing_expense": "Expense",
    "billing_payment": "",
    "billing_link": "Invoice link",
}

_MODEL_BY_NAME = {
    "MatterFileAsset": MatterFileAsset,
    "MatterMemoFileAsset": MatterMemoFileAsset,
    "MatterPartyRole": MatterPartyRole,
    "MatterStaffAssignment": MatterStaffAssignment,
    "Party": Party,
    "PartyCode": PartyCode,
    "PartyContact": PartyContact,
    "PartyAddress": PartyAddress,
    "LegacyInvoice": LegacyInvoice,
    "LegacyInvoicePayment": LegacyInvoicePayment,
    "LegacyExpense": LegacyExpense,
    "LegacyExpensePayment": LegacyExpensePayment,
    "ExternalInvoiceCaseLink": ExternalInvoiceCaseLink,
    "ExternalInvoiceCaseMap": ExternalInvoiceCaseMap,
    "CaseExpenseInvoiceMap": CaseExpenseInvoiceMap,
}


def deletion_entity_label(entity_type: str | None) -> str:
    normalized = (entity_type or "").strip().lower()
    return _ENTITY_LABELS.get(normalized, normalized or "-")


def infer_matter_id_for_deletion_log(log: DeletionLog) -> str | None:
    parent_type = (getattr(log, "parent_type", None) or "").strip().lower()
    parent_id = (getattr(log, "parent_id", None) or "").strip()
    if parent_type == "matter" and parent_id:
        return parent_id

    payload = log.payload if isinstance(log.payload, dict) else {}
    if not payload:
        return None

    if (log.entity_type or "").strip().lower() == "workflow":
        value = payload.get("case_id")
    else:
        value = payload.get("matter_id") or payload.get("case_id")

    if value is None:
        return None
    text_val = str(value).strip()
    return text_val or None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip().split("T", 1)[0])
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _column_payload(obj: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_model": obj.__class__.__name__,
        "_table": getattr(obj, "__tablename__", ""),
        "_schema": "deletion-log-v2",
    }
    for column in getattr(obj, "__table__", []).columns:
        payload[column.name] = _json_safe(getattr(obj, column.name, None))
    return payload


def _stringify_tags(tags: tuple[str, ...] | list[str] | set[str] | str | None) -> str | None:
    if tags is None:
        return None
    if isinstance(tags, str):
        raw = [tags]
    else:
        raw = list(tags)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        cleaned.append(token)
    return ",".join(cleaned)[:255] if cleaned else None


def _search_vector(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            for child in value.values():
                if child is not None:
                    parts.append(str(child))
            continue
        if isinstance(value, (list, tuple, set)):
            parts.extend(str(child) for child in value if child is not None)
            continue
        parts.append(str(value))
    return " ".join(part.strip() for part in parts if part and part.strip())[:8000]


def _actor_user_id_from_context(obj: Any | None = None) -> int | None:
    if has_request_context():
        try:
            if getattr(current_user, "is_authenticated", False):
                return int(current_user.id)
        except Exception:
            return _coerce_int(getattr(obj, "deleted_by", None) if obj is not None else None)
    raw = getattr(obj, "deleted_by", None) if obj is not None else None
    return _coerce_int(raw)


def _clear_soft_delete_fields(row: Any) -> None:
    if hasattr(row, "is_deleted"):
        row.is_deleted = False
    for attr in ("deleted_at", "deleted_by", "delete_reason", "deleted_op_id"):
        if hasattr(row, attr):
            setattr(row, attr, None)


def _assign_payload_columns(
    row: Any, payload: dict[str, Any], *, skip: set[str] | None = None
) -> None:
    skip = skip or set()
    for column in getattr(row, "__table__", []).columns:
        name = column.name
        if name.startswith("_") or name in skip or name not in payload:
            continue
        value = payload.get(name)
        type_name = column.type.__class__.__name__.lower()
        if value in ("", None):
            setattr(row, name, None)
        elif "date" in type_name and "time" not in type_name:
            setattr(row, name, _parse_date(value))
        elif "datetime" in type_name:
            setattr(row, name, _parse_datetime(value))
        else:
            setattr(row, name, value)


def _pk_column_name(model: type[Any]) -> str:
    columns = list(getattr(model, "__table__", []).primary_key.columns)
    if not columns:
        raise ValueError("missing_primary_key")
    return columns[0].name


class DeletionService:
    """Archive and restore deleted application data through DeletionLog."""

    def archive(
        self,
        obj: Any,
        *,
        user_id: int | None = None,
        tags: tuple[str, ...] | list[str] | set[str] | str | None = None,
        session: SASession | None = None,
    ) -> DeletionLog | None:
        target_session = session or db.session
        log = self.build_log(obj, user_id=user_id, tags=tags)
        if log is None:
            return None
        if self._has_pending_duplicate(target_session, log):
            return None
        target_session.add(log)
        return log

    def build_log(
        self,
        obj: Any,
        *,
        user_id: int | None = None,
        tags: tuple[str, ...] | list[str] | set[str] | str | None = None,
    ) -> DeletionLog | None:
        if isinstance(obj, DeletionLog):
            return None

        payload = self._payload_for_object(obj)
        if not payload:
            return None

        entity_type = self._entity_type_for_object(obj)
        entity_key = self._entity_key_for_object(obj)
        entity_id = self._entity_id_for_object(obj)
        parent_type, parent_id = self._parent_for_object(obj, payload)
        title = self._title_for_object(obj, payload)

        all_tags: list[str] = ["auto-archive"]
        if tags:
            all_tags.extend(tags if not isinstance(tags, str) else [tags])

        deleted_at = getattr(obj, "deleted_at", None)
        if not isinstance(deleted_at, datetime):
            deleted_at = datetime.utcnow()

        return DeletionLog(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_key=entity_key,
            title=title,
            payload=payload,
            parent_type=parent_type,
            parent_id=parent_id,
            search_vector=_search_vector(
                entity_type,
                entity_id,
                entity_key,
                title,
                parent_type,
                parent_id,
                payload,
            ),
            tags=_stringify_tags(all_tags),
            deleted_by=user_id if user_id is not None else _actor_user_id_from_context(obj),
            deleted_at=deleted_at,
        )

    def preview(self, log_id: int) -> dict[str, Any]:
        log = db.session.get(DeletionLog, int(log_id))
        if not log:
            raise ValueError("not_found")
        return self.preview_log(log)

    def preview_log(self, log: DeletionLog) -> dict[str, Any]:
        payload = log.payload if isinstance(log.payload, dict) else {}
        entity_type = (log.entity_type or "").strip().lower()
        warnings: list[str] = []
        blockers: list[str] = []
        dependencies: list[dict[str, str]] = []
        fields: list[dict[str, str]] = []
        matter_id: str | None = None
        matter_ref: str | None = None

        def _field(key: str, label: str) -> None:
            value = payload.get(key)
            if value is None:
                return
            rendered = str(value).strip()
            if not rendered:
                return
            fields.append({"key": key, "label": label, "value": rendered})

        def _matter_dependency(mid: str | None, *, required: bool = True) -> None:
            nonlocal matter_id, matter_ref
            matter_id = (mid or "").strip() or None
            if not matter_id:
                dependencies.append(
                    {
                        "name": " Matter",
                        "status": "missing",
                        "message": "Matter ID  none.",
                    }
                )
                if required:
                    blockers.append(" Matter (matter_id/case_id)  Restore  none.")
                return

            matter = db.session.get(Matter, matter_id)
            if not matter:
                dependencies.append(
                    {
                        "name": " Matter",
                        "status": "missing",
                        "message": f"matter_id={matter_id} Matter does not.",
                    }
                )
                if required:
                    blockers.append(f" Matter(matter_id={matter_id})  Restore  none.")
                return

            matter_ref = (getattr(matter, "our_ref", None) or "").strip() or None
            if bool(getattr(matter, "is_deleted", False)):
                dependencies.append(
                    {
                        "name": " Matter",
                        "status": "deleted",
                        "message": f"{matter_ref or matter_id} Matter Deleted status.",
                    }
                )
                blockers.append(" Matter Deleted status. Matter Restore/active  Retry.")
                return

            dependencies.append(
                {
                    "name": " Matter",
                    "status": "ok",
                    "message": f"{matter_ref or matter_id} Matter active Status.",
                }
            )

        if entity_type == "deadline":
            self._preview_deadline(payload, fields, blockers, warnings, _field, _matter_dependency)
            matter_id = matter_id or (payload.get("matter_id") or None)
        elif entity_type == "renewal":
            self._preview_renewal(payload, fields, blockers, _field, _matter_dependency)
            matter_id = matter_id or (payload.get("matter_id") or None)
        elif entity_type == "workflow":
            self._preview_workflow(payload, fields, blockers, warnings, _field, _matter_dependency)
            matter_id = matter_id or (payload.get("case_id") or None)
        elif entity_type == "file":
            self._preview_file(payload, fields, blockers, warnings, _field, _matter_dependency)
            matter_id = matter_id or (payload.get("matter_id") or getattr(log, "parent_id", None))
        elif entity_type == "person":
            self._preview_person(payload, fields, blockers, warnings, _field, _matter_dependency)
            matter_id = matter_id or (payload.get("matter_id") or getattr(log, "parent_id", None))
        elif entity_type in {
            "billing_invoice",
            "billing_expense",
            "billing_payment",
            "billing_link",
        }:
            self._preview_billing(
                entity_type,
                payload,
                fields,
                blockers,
                warnings,
                _field,
                _matter_dependency,
            )
            matter_id = matter_id or (payload.get("matter_id") or getattr(log, "parent_id", None))
        else:
            blockers.append(f"  entity_type : {entity_type or '-'}")

        if not fields:
            for key, value in payload.items():
                if str(key).startswith("_") or value is None:
                    continue
                rendered = str(value).strip()
                if rendered:
                    fields.append({"key": str(key), "label": str(key), "value": rendered})

        return {
            "entity_type": entity_type,
            "entity_label": deletion_entity_label(entity_type),
            "matter_id": matter_id,
            "matter_ref": matter_ref,
            "can_restore": len(blockers) == 0,
            "warnings": warnings,
            "blockers": blockers,
            "dependencies": dependencies,
            "fields": fields[:80],
        }

    def restore(self, log_id: int, *, actor_user_id: int | None) -> dict[str, Any]:
        log = db.session.get(DeletionLog, int(log_id))
        return self.restore_log(log, actor_user_id=actor_user_id)

    def restore_log(
        self,
        log: DeletionLog | None,
        *,
        actor_user_id: int | None,
    ) -> dict[str, Any]:
        if not log:
            raise ValueError("not_found")
        if log.restored_at:
            raise ValueError("already_restored")

        preview = self.preview_log(log)
        if preview.get("blockers"):
            err = ValueError("dependency_blocked")
            setattr(err, "preview", preview)
            raise err

        payload = log.payload or {}
        entity_type = (log.entity_type or "").strip().lower()
        restored_id: int | None = None
        restored_key: str | None = None
        matter_id: str | None = None
        case_id: Any = None
        link_url: str | None = None

        if entity_type == "deadline":
            restored_id, restored_key, matter_id, case_id, link_url = self._restore_deadline(
                log, payload
            )
        elif entity_type == "renewal":
            restored_id, restored_key, matter_id, case_id, link_url = self._restore_renewal(
                log, payload
            )
        elif entity_type == "workflow":
            restored_id, restored_key, matter_id, case_id, link_url = self._restore_workflow(
                log, payload
            )
        elif entity_type == "file":
            restored_id, restored_key, matter_id, link_url = self._restore_file(log, payload)
        elif entity_type == "person":
            restored_id, restored_key, matter_id, link_url = self._restore_person(log, payload)
        elif entity_type in {
            "billing_invoice",
            "billing_expense",
            "billing_payment",
            "billing_link",
        }:
            restored_id, restored_key, matter_id, link_url = self._restore_billing(
                entity_type, log, payload
            )
        else:
            raise ValueError("unsupported_entity_type")

        log.restored_entity_id = restored_id
        log.restored_entity_key = restored_key
        log.restored_by = actor_user_id
        log.restored_at = datetime.utcnow()
        db.session.add(log)
        db.session.commit()

        self._log_restore_audit(
            log,
            restored_id=restored_id,
            restored_key=restored_key,
            case_id=case_id,
            matter_id=matter_id,
        )
        return {
            "success": True,
            "restored_entity_id": restored_id,
            "restored_entity_key": restored_key,
            "case_id": case_id,
            "matter_id": matter_id,
            "link_url": link_url,
            "message": "Restore completed.",
        }

    def cleanup(self, *, days: int = 90) -> int:
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(days or 90)))
        rows = DeletionLog.query.filter(DeletionLog.deleted_at < cutoff).all()
        for row in rows:
            db.session.delete(row)
        db.session.commit()
        return len(rows)

    def _payload_for_object(self, obj: Any) -> dict[str, Any] | None:
        if isinstance(obj, DocketItem):
            return _column_payload(obj)
        if isinstance(obj, AnnuityItem):
            return _column_payload(obj)
        if isinstance(obj, Workflow):
            return _column_payload(obj)
        if isinstance(obj, MatterFileAsset):
            return _column_payload(obj)
        if isinstance(obj, MatterMemoFileAsset):
            return _column_payload(obj)
        if isinstance(obj, MatterPartyRole):
            return _column_payload(obj)
        if isinstance(obj, MatterStaffAssignment):
            return _column_payload(obj)
        if isinstance(obj, (Party, PartyCode, PartyContact, PartyAddress)):
            return _column_payload(obj)
        if isinstance(obj, LegacyInvoice):
            payload = _column_payload(obj)
            payload["payments"] = [
                _column_payload(row)
                for row in LegacyInvoicePayment.query.filter_by(invoice_id=str(obj.invoice_id)).all()
            ]
            return payload
        if isinstance(obj, LegacyExpense):
            payload = _column_payload(obj)
            payload["payments"] = [
                _column_payload(row)
                for row in LegacyExpensePayment.query.filter_by(expense_id=str(obj.expense_id)).all()
            ]
            payload["invoice_links"] = [
                _column_payload(row)
                for row in CaseExpenseInvoiceMap.query.filter_by(
                    expense_id=str(obj.expense_id)
                ).all()
            ]
            return payload
        if isinstance(obj, (LegacyInvoicePayment, LegacyExpensePayment)):
            return _column_payload(obj)
        if isinstance(
            obj, (ExternalInvoiceCaseLink, ExternalInvoiceCaseMap, CaseExpenseInvoiceMap)
        ):
            return _column_payload(obj)
        return None

    def _entity_type_for_object(self, obj: Any) -> str:
        if isinstance(obj, DocketItem):
            return "deadline"
        if isinstance(obj, AnnuityItem):
            return "renewal"
        if isinstance(obj, Workflow):
            return "workflow"
        if isinstance(obj, (MatterFileAsset, MatterMemoFileAsset)):
            return "file"
        if isinstance(
            obj,
            (MatterPartyRole, MatterStaffAssignment, Party, PartyCode, PartyContact, PartyAddress),
        ):
            return "person"
        if isinstance(obj, LegacyInvoice):
            return "billing_invoice"
        if isinstance(obj, LegacyExpense):
            return "billing_expense"
        if isinstance(obj, (LegacyInvoicePayment, LegacyExpensePayment)):
            return "billing_payment"
        if isinstance(
            obj, (ExternalInvoiceCaseLink, ExternalInvoiceCaseMap, CaseExpenseInvoiceMap)
        ):
            return "billing_link"
        raise ValueError("unsupported_entity_type")

    def _entity_key_for_object(self, obj: Any) -> str | None:
        for attr in (
            "docket_id",
            "annuity_id",
            "id",
            "matter_file_id",
            "memo_file_id",
            "mpr_id",
            "msa_id",
            "party_id",
            "party_code_id",
            "contact_id",
            "address_id",
            "invoice_id",
            "payment_id",
            "expense_id",
            "exp_payment_id",
        ):
            if hasattr(obj, attr):
                value = getattr(obj, attr, None)
                if value is not None:
                    return str(value)
        return None

    def _entity_id_for_object(self, obj: Any) -> int:
        if isinstance(obj, Workflow):
            return int(getattr(obj, "id", 0) or 0)
        if isinstance(
            obj, (ExternalInvoiceCaseLink, ExternalInvoiceCaseMap, CaseExpenseInvoiceMap)
        ):
            return int(getattr(obj, "id", 0) or 0)
        if obj.__class__.__name__ == "Invoice":
            return int(getattr(obj, "id", 0) or 0)
        return 0

    def _parent_for_object(
        self, obj: Any, payload: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        if isinstance(obj, Workflow):
            value = getattr(obj, "case_id", None)
            return ("matter", str(value)) if value else (None, None)
        for attr in ("matter_id", "case_id"):
            value = getattr(obj, attr, None)
            if value:
                return "matter", str(value)
        value = payload.get("matter_id") or payload.get("case_id")
        return ("matter", str(value)) if value else (None, None)

    def _title_for_object(self, obj: Any, payload: dict[str, Any]) -> str:
        if isinstance(obj, DocketItem):
            return (obj.name_free or obj.name_ref or str(obj.docket_id) or "").strip()
        if isinstance(obj, AnnuityItem):
            return f"Renewal {obj.cycle_no}" if obj.cycle_no else str(obj.annuity_id)
        if isinstance(obj, Workflow):
            return (obj.name or str(obj.id) or "").strip()
        if isinstance(obj, MatterFileAsset):
            return (
                obj.description or obj.role or obj.file_asset_id or obj.matter_file_id or ""
            ).strip()
        if isinstance(obj, MatterMemoFileAsset):
            return (
                obj.description or obj.role or obj.file_asset_id or obj.memo_file_id or ""
            ).strip()
        if isinstance(obj, MatterPartyRole):
            return (obj.raw_text or obj.party_id or obj.role_code or obj.mpr_id or "").strip()
        if isinstance(obj, MatterStaffAssignment):
            return (
                obj.raw_text or obj.staff_party_id or obj.staff_role_code or obj.msa_id or ""
            ).strip()
        if isinstance(obj, Party):
            return (obj.name_display or obj.name_en or obj.party_id or "").strip()
        if isinstance(obj, LegacyInvoice):
            return (
                obj.fee_ref
                or obj.external_invoice_number
                or obj.description
                or obj.invoice_id
                or ""
            ).strip()
        if isinstance(obj, LegacyExpense):
            return (obj.expense_ref or obj.dn_no or obj.vendor_name or obj.expense_id or "").strip()
        return str(self._entity_key_for_object(obj) or payload.get("_model") or "(deleted)")

    def _has_pending_duplicate(self, session: SASession, log: DeletionLog) -> bool:
        identity = (
            (log.entity_type or "").strip().lower(),
            int(log.entity_id or 0),
            str(log.entity_key or ""),
        )
        for row in session.new:
            if not isinstance(row, DeletionLog):
                continue
            row_identity = (
                (row.entity_type or "").strip().lower(),
                int(row.entity_id or 0),
                str(row.entity_key or ""),
            )
            if row_identity == identity:
                return True
        return False

    def _preview_deadline(self, payload, fields, blockers, warnings, _field, _matter_dependency):
        _field("docket_id", "Deadline ID")
        _field("matter_id", "Matter ID")
        _field("name_ref", " ")
        _field("name_free", "Deadline")
        _field("due_date", "Final Due date")
        _field("owner_staff_party_id", "Responsible staff_party_id")

        if payload.get("docket_id") or payload.get("matter_id"):
            docket_id = (payload.get("docket_id") or "").strip()
            matter_id = (payload.get("matter_id") or "").strip()
            _matter_dependency(matter_id)
            existing = db.session.get(DocketItem, docket_id) if docket_id else None
            if existing and not bool(getattr(existing, "is_deleted", False)):
                blockers.append(f"docket_id={docket_id}   active Status .")

            name_ref = (payload.get("name_ref") or "").strip()
            due_date = (payload.get("due_date") or "").strip()
            done_date = (payload.get("done_date") or "").strip()
            if matter_id and name_ref and due_date and not done_date:
                dup = (
                    DocketItem.query.filter(DocketItem.matter_id == matter_id)
                    .filter(DocketItem.name_ref == name_ref, DocketItem.due_date == due_date)
                    .filter(
                        or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == "")
                    )
                    .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
                    .first()
                )
                if dup and (not docket_id or str(dup.docket_id) != docket_id):
                    warnings.append(
                        f" (Matter++Deadline)  Deadline  . "
                        f"(docket_id={dup.docket_id})"
                    )
            return

        case_id = _coerce_int(payload.get("case_id"))
        _field("case_id", "Legacy Case ID")
        if case_id is None:
            blockers.append("legacy deadline Restore Required case_id none.")
            return
        legacy_case = LegacyCaseAdapter.get_case(case_id)
        if not legacy_case:
            blockers.append(f"legacy case_id={case_id}  does not.")

    def _preview_renewal(self, payload, fields, blockers, _field, _matter_dependency):
        _field("annuity_id", "Annuity ID")
        _field("matter_id", "Matter ID")
        _field("cycle_no", "")
        _field("annuity_status", "Status")
        _field("due_date", "Payment deadline")
        _field("owner_staff_party_id", "Responsible staff_party_id")

        if payload.get("annuity_id") or payload.get("matter_id"):
            annuity_id = (payload.get("annuity_id") or "").strip()
            matter_id = (payload.get("matter_id") or "").strip()
            _matter_dependency(matter_id)

            existing = db.session.get(AnnuityItem, annuity_id) if annuity_id else None
            if existing and not bool(getattr(existing, "is_deleted", False)):
                blockers.append(f"annuity_id={annuity_id}   active Status .")

            cycle_no = _coerce_int(payload.get("cycle_no") or payload.get("year"))
            if matter_id and cycle_no:
                dup = (
                    AnnuityItem.query.filter(AnnuityItem.matter_id == matter_id)
                    .filter(AnnuityItem.cycle_no == cycle_no)
                    .filter(
                        or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))
                    )
                    .first()
                )
                if dup and (not annuity_id or str(dup.annuity_id) != annuity_id):
                    blockers.append(
                        f" Matter/   . "
                        f"(matter_id={matter_id}, cycle_no={cycle_no}, annuity_id={dup.annuity_id})"
                    )
            return

        case_id = _coerce_int(payload.get("case_id"))
        _field("case_id", "Legacy Case ID")
        if case_id is None:
            blockers.append("legacy renewal Restore Required case_id none.")
        elif not LegacyCaseAdapter.get_case(case_id):
            blockers.append(f"legacy case_id={case_id}  does not.")

    def _preview_workflow(self, payload, fields, blockers, warnings, _field, _matter_dependency):
        _field("case_id", "Matter ID")
        _field("business_code", "Task")
        _field("name", "Task")
        _field("status", "Status")
        _field("due_date", "Deadline")
        _field("assignee_id", "Responsible user_id")
        _field("attorney_assignee_id", "Responsible attorney user_id")

        _matter_dependency((payload.get("case_id") or "").strip())
        business_code = (payload.get("business_code") or "").strip()
        if business_code:
            existing = Workflow.query.filter(Workflow.business_code == business_code).first()
            if existing:
                blockers.append(
                    f"business_code={business_code}    In Progress. "
                    f"(workflow_id={existing.id})"
                )

        for key in ("assignee_id", "attorney_assignee_id", "inspector_id"):
            user_id = _coerce_int(payload.get(key))
            if user_id is not None and not db.session.get(User, user_id):
                warnings.append(f"{key}={user_id} User does not.")

    def _preview_file(self, payload, fields, blockers, warnings, _field, _matter_dependency):
        _field("matter_file_id", "File link ID")
        _field("memo_file_id", "Notes File ID")
        _field("matter_id", "Matter ID")
        _field("file_asset_id", "File Asset ID")
        _field("role", "Role")
        _field("description", "Description")
        model_name = str(payload.get("_model") or "")
        matter_id = (payload.get("matter_id") or "").strip()
        if model_name == "MatterFileAsset":
            _matter_dependency(matter_id)
        elif model_name == "MatterMemoFileAsset":
            _field("memo_id", "Notes ID")

        file_asset_id = (payload.get("file_asset_id") or "").strip()
        if file_asset_id and not db.session.get(FileAsset, file_asset_id):
            warnings.append(f"file_asset_id={file_asset_id} Source file  not found.")

        existing = self._existing_for_payload(payload)
        if existing and not bool(getattr(existing, "is_deleted", False)):
            blockers.append(" File link  active Status.")

    def _preview_person(self, payload, fields, blockers, warnings, _field, _matter_dependency):
        _field("mpr_id", " Role ID")
        _field("msa_id", "Contact  ID")
        _field("matter_id", "Matter ID")
        _field("party_id", "Party ID")
        _field("role_code", "Role")
        _field("staff_role_code", "Responsible Role")
        model_name = str(payload.get("_model") or "")
        if model_name in {"MatterPartyRole", "MatterStaffAssignment"}:
            _matter_dependency((payload.get("matter_id") or "").strip())
        party_id = (payload.get("party_id") or payload.get("staff_party_id") or "").strip()
        if party_id and not db.session.get(Party, party_id):
            warnings.append(f"party_id={party_id}   does not.")
        existing = self._existing_for_payload(payload)
        if existing and not bool(getattr(existing, "is_deleted", False)):
            blockers.append(" /Contact   .")

    def _preview_billing(
        self,
        entity_type,
        payload,
        fields,
        blockers,
        warnings,
        _field,
        _matter_dependency,
    ):
        _field("invoice_id", "Billing ID")
        _field("expense_id", "Expense ID")
        _field("payment_id", "Receipt ID")
        _field("exp_payment_id", " ID")
        _field("matter_id", "Matter ID")
        _field("fee_ref", "Billing")
        _field("expense_ref", "Expense")
        _field("external_invoice_id", "External Billing ID")
        if payload.get("matter_id"):
            _matter_dependency(str(payload.get("matter_id") or ""))
        elif entity_type in {"billing_invoice", "billing_expense"}:
            blockers.append("Billing/Expense Restore Required matter_id none.")
        existing = self._existing_for_payload(payload)
        if existing and not bool(getattr(existing, "is_deleted", False)):
            blockers.append(" Billing/Expense   active Status.")

    def _restore_deadline(self, log: DeletionLog, payload: dict[str, Any]):
        if payload.get("docket_id") or payload.get("matter_id"):
            matter_id = str(payload.get("matter_id") or "")
            from app.utils.task_classification import determine_category_by_staff_role

            docket_id = str(payload.get("docket_id") or uuid.uuid4().hex)
            existing = db.session.get(DocketItem, docket_id)
            di = existing or DocketItem(docket_id=docket_id, matter_id=matter_id, category="WORK")
            _clear_soft_delete_fields(di)
            restored_owner = payload.get("owner_staff_party_id")
            di.matter_id = matter_id
            di.category = payload.get("category") or determine_category_by_staff_role(
                matter_id, staff_party_id=restored_owner
            )
            di.name_ref = payload.get("name_ref")
            di.name_free = payload.get("name_free") or log.title or "(restored)"
            di.due_date = payload.get("due_date")
            di.extended_due_date = payload.get("extended_due_date")
            di.visible_from_date = payload.get("visible_from_date")
            di.done_date = payload.get("done_date")
            di.owner_staff_party_id = payload.get("owner_staff_party_id")
            di.memo = payload.get("memo")
            di.raw_id = payload.get("raw_id")
            db.session.add(di)
            db.session.commit()
            return 0, str(di.docket_id), matter_id, None, f"/case/{matter_id}"

        case_id = payload.get("case_id")
        due_dt = _parse_date(payload.get("due_date"))
        if not due_dt:
            raise ValueError("missing_due_date")
        d = Deadline(
            case_id=case_id,
            title=payload.get("title") or log.title or "(restored)",
            type=payload.get("type"),
            due_date=due_dt,
            internal_due_date=_parse_date(payload.get("internal_due_date")),
            status=payload.get("status") or "new",
            assigned_to=payload.get("assigned_to"),
            priority=payload.get("priority") or "normal",
            notes=payload.get("notes"),
        )
        db.session.add(d)
        db.session.commit()
        case = LegacyCaseAdapter.get_case(d.case_id)
        return int(d.id), None, None, case_id, f"/case/{case_id}" if case else None

    def _restore_renewal(self, log: DeletionLog, payload: dict[str, Any]):
        if payload.get("annuity_id") or payload.get("matter_id"):
            matter_id = str(payload.get("matter_id") or "")
            cycle_no = _coerce_int(payload.get("cycle_no") or payload.get("year"))
            if not matter_id or not cycle_no or cycle_no <= 0:
                raise ValueError("missing_annuity_identity")

            annuity_id = str(payload.get("annuity_id") or uuid.uuid4().hex)
            existing = db.session.get(AnnuityItem, annuity_id)
            if existing is None:
                existing = AnnuityItem.query.filter_by(
                    matter_id=matter_id, cycle_no=cycle_no
                ).first()

            ai = existing or AnnuityItem(
                annuity_id=annuity_id, matter_id=matter_id, cycle_no=cycle_no
            )
            if existing is not None:
                revive_soft_deleted_annuity_item(existing)
            _clear_soft_delete_fields(ai)
            ai.matter_id = matter_id
            ai.cycle_no = cycle_no
            status_raw = payload.get("annuity_status") or payload.get("status") or "pending"
            ai.annuity_status = str(status_raw).strip().lower() if status_raw is not None else None
            ai.owner_staff_party_id = payload.get("owner_staff_party_id")
            ai.due_date = payload.get("due_date")
            ai.extended_due_date = payload.get("extended_due_date")
            ai.renewal_open_date = payload.get("renewal_open_date")
            ai.renewal_notice_due = payload.get("renewal_notice_due")
            ai.internal_due_date = payload.get("internal_due_date")
            ai.paid_date = payload.get("paid_date")
            ai.official_fee = self._safe_float(
                payload.get("official_fee") or payload.get("fee_amount")
            )
            ai.vat_amount = self._safe_float(payload.get("vat_amount"))
            ai.service_fee = self._safe_float(payload.get("service_fee"))
            ai.memo = payload.get("memo") or payload.get("notes")
            ai.raw_id = payload.get("raw_id")
            db.session.add(ai)
            enqueue_annuity_sync_for_item(annuity_item=ai)
            db.session.commit()
            return 0, str(ai.annuity_id), matter_id, None, f"/case/{matter_id}"

        case_id = payload.get("case_id")
        due_dt = _parse_date(payload.get("due_date"))
        if not due_dt:
            raise ValueError("missing_due_date")
        rf = RenewalFee(
            case_id=case_id,
            year=int(payload.get("year") or 1),
            due_date=due_dt,
            fee_amount=payload.get("fee_amount") or 0,
            currency=payload.get("currency") or "USD",
            status=payload.get("status") or "pending",
            notes=payload.get("notes"),
        )
        db.session.add(rf)
        db.session.commit()
        case = LegacyCaseAdapter.get_case(rf.case_id)
        return int(rf.id), None, None, case_id, f"/case/{case_id}" if case else None

    def _restore_workflow(self, log: DeletionLog, payload: dict[str, Any]):
        case_id = payload.get("case_id")
        wf = Workflow(
            case_id=case_id,
            name=payload.get("name") or log.title or "(restored)",
            status=payload.get("status") or Workflow.STATUS_PENDING,
            business_code=payload.get("business_code"),
            category=payload.get("category") or "WORK",
            priority=payload.get("priority") or "normal",
            send_memo=payload.get("send_memo"),
            request_start_date=_parse_date(payload.get("request_start_date")),
            legal_due_date=_parse_date(payload.get("legal_due_date")),
            draft_due_date=_parse_date(payload.get("draft_due_date")),
            draft_due_date2=_parse_date(payload.get("draft_due_date2")),
            submit_due_date=_parse_date(payload.get("submit_due_date")),
            draft_sent_date=_parse_date(payload.get("draft_sent_date")),
            submit_date=_parse_date(payload.get("submit_date")),
            due_date=_parse_date(payload.get("due_date")),
            completed_date=_parse_date(payload.get("completed_date")),
            difficulty=payload.get("difficulty"),
            page_count=payload.get("page_count"),
            work_hours=payload.get("work_hours"),
            assignee_id=payload.get("assignee_id"),
            attorney_assignee_id=payload.get("attorney_assignee_id"),
            inspector_id=payload.get("inspector_id"),
            created_by_id=payload.get("created_by_id"),
            note=payload.get("note"),
        )
        db.session.add(wf)
        db.session.commit()
        return (
            int(wf.id),
            None,
            str(case_id) if case_id else None,
            case_id,
            f"/case/{case_id}" if case_id else None,
        )

    def _restore_file(self, log: DeletionLog, payload: dict[str, Any]):
        model = self._model_for_payload(payload)
        if model not in {MatterFileAsset, MatterMemoFileAsset}:
            raise ValueError("unsupported_entity_type")
        row = self._upsert_from_payload(model, payload)
        matter_id = str(getattr(row, "matter_id", "") or payload.get("matter_id") or "")
        db.session.commit()
        return (
            0,
            str(self._entity_key_for_object(row) or log.entity_key or ""),
            matter_id or None,
            f"/case/{matter_id}" if matter_id else None,
        )

    def _restore_person(self, log: DeletionLog, payload: dict[str, Any]):
        model = self._model_for_payload(payload)
        if model not in {
            MatterPartyRole,
            MatterStaffAssignment,
            Party,
            PartyCode,
            PartyContact,
            PartyAddress,
        }:
            raise ValueError("unsupported_entity_type")
        row = self._upsert_from_payload(model, payload)
        matter_id = str(getattr(row, "matter_id", "") or payload.get("matter_id") or "")
        db.session.commit()
        return (
            0,
            str(self._entity_key_for_object(row) or log.entity_key or ""),
            matter_id or None,
            f"/case/{matter_id}" if matter_id else None,
        )

    def _restore_billing(self, entity_type: str, log: DeletionLog, payload: dict[str, Any]):
        model = self._model_for_payload(payload)
        if model is None:
            if entity_type == "billing_invoice":
                model = LegacyInvoice
            elif entity_type == "billing_expense":
                model = LegacyExpense
            else:
                raise ValueError("unsupported_entity_type")
        row = self._upsert_from_payload(model, payload)

        if isinstance(row, LegacyInvoice):
            self._restore_child_rows(LegacyInvoicePayment, payload.get("payments") or [])
        elif isinstance(row, LegacyExpense):
            self._restore_child_rows(LegacyExpensePayment, payload.get("payments") or [])
            self._restore_child_rows(CaseExpenseInvoiceMap, payload.get("invoice_links") or [])

        matter_id = str(getattr(row, "matter_id", "") or payload.get("matter_id") or "")
        db.session.commit()
        restored_id = int(getattr(row, "id", 0) or 0)
        return (
            restored_id,
            str(self._entity_key_for_object(row) or log.entity_key or ""),
            matter_id or None,
            f"/case/{matter_id}" if matter_id else None,
        )

    def _restore_child_rows(self, model: type[Any], payloads: list[dict[str, Any]]) -> None:
        if not isinstance(payloads, list):
            return
        for child_payload in payloads:
            if isinstance(child_payload, dict):
                self._upsert_from_payload(model, child_payload)

    def _upsert_from_payload(self, model: type[Any], payload: dict[str, Any]) -> Any:
        pk_name = _pk_column_name(model)
        pk_value = payload.get(pk_name)
        row = db.session.get(model, pk_value) if pk_value not in (None, "") else None
        if row is None:
            row = model()
        _assign_payload_columns(row, payload)
        _clear_soft_delete_fields(row)
        db.session.add(row)
        return row

    def _existing_for_payload(self, payload: dict[str, Any]) -> Any | None:
        model = self._model_for_payload(payload)
        if model is None:
            return None
        pk_name = _pk_column_name(model)
        pk_value = payload.get(pk_name)
        if pk_value in (None, ""):
            return None
        return db.session.get(model, pk_value)

    def _model_for_payload(self, payload: dict[str, Any]) -> type[Any] | None:
        model_name = str(payload.get("_model") or "").strip()
        return _MODEL_BY_NAME.get(model_name)

    def _safe_float(self, value: Any) -> float:
        try:
            if value in (None, ""):
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _log_restore_audit(
        self,
        log: DeletionLog,
        *,
        restored_id: int | None,
        restored_key: str | None,
        case_id: Any,
        matter_id: str | None,
    ) -> None:
        try:
            from app.blueprints.billing_invoices.auth import log_audit

            log_audit(
                "deletion.restore",
                "deletion_log",
                log.id,
                json.dumps(
                    {
                        "entity_type": log.entity_type,
                        "restored_entity_id": restored_id,
                        "restored_entity_key": restored_key,
                        "case_id": case_id,
                        "matter_id": matter_id,
                    },
                    ensure_ascii=False,
                ),
            )
        except (ImportError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as exc:
            report_swallowed_exception(
                exc,
                context="deletion_manager.restore.audit",
                log_key="deletion_manager.restore.audit",
                log_window_seconds=300,
            )


def _is_supported_object(obj: Any) -> bool:
    return isinstance(
        obj,
        (
            DocketItem,
            AnnuityItem,
            Workflow,
            MatterFileAsset,
            MatterMemoFileAsset,
            MatterPartyRole,
            MatterStaffAssignment,
            Party,
            PartyCode,
            PartyContact,
            PartyAddress,
            LegacyInvoice,
            LegacyInvoicePayment,
            LegacyExpense,
            LegacyExpensePayment,
            ExternalInvoiceCaseLink,
            ExternalInvoiceCaseMap,
            CaseExpenseInvoiceMap,
        ),
    )


def _is_soft_deleted_change(obj: Any) -> bool:
    if not _is_supported_object(obj):
        return False
    try:
        state = sa_inspect(obj)
    except Exception:
        return False
    if not state.persistent:
        return False
    if hasattr(obj, "is_deleted"):
        hist = state.attrs.is_deleted.history
        if hist.has_changes() and bool(getattr(obj, "is_deleted", False)):
            return True
    if hasattr(obj, "deleted_at"):
        hist = state.attrs.deleted_at.history
        if hist.has_changes() and getattr(obj, "deleted_at", None) is not None:
            return True
    return False


def _auto_archive_before_flush(session: SASession, _flush_context, _instances) -> None:
    if int(session.info.get(_SUPPRESS_KEY, 0) or 0) > 0:
        return

    service = DeletionService()
    actor_id = _actor_user_id_from_context()
    for obj in list(session.deleted):
        if not _is_supported_object(obj):
            continue
        try:
            service.archive(
                obj,
                user_id=actor_id,
                tags=("auto", "hard-delete"),
                session=session,
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deletion_manager.auto_archive.hard_delete",
                log_key="deletion_manager.auto_archive.hard_delete",
                log_window_seconds=300,
            )

    for obj in list(session.dirty):
        if not _is_soft_deleted_change(obj):
            continue
        try:
            service.archive(
                obj,
                user_id=actor_id,
                tags=("auto", "soft-delete"),
                session=session,
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="deletion_manager.auto_archive.soft_delete",
                log_key="deletion_manager.auto_archive.soft_delete",
                log_window_seconds=300,
            )


def init_deletion_listeners() -> None:
    global _MODULE_INITIALIZED
    if _MODULE_INITIALIZED:
        return
    if not event.contains(SASession, "before_flush", _auto_archive_before_flush):
        event.listen(SASession, "before_flush", _auto_archive_before_flush)
    _MODULE_INITIALIZED = True
