from __future__ import annotations

import json
import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app, g
from sqlalchemy import bindparam, func
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.models.backup_set import BackupSet
from app.models.case import Case
from app.models.client import Client
from app.models.crm import CRMActivity, CRMContact, CRMLead, CRMOpportunity
from app.models.crm_client_merge_log import CRMClientMergeLog
from app.models.system_config import SystemConfig
from app.services.billing.db_core import unified_clients_enabled
from app.services.billing.subsystem import billing_subsystem_enabled
from app.services.client.client_tagging import build_client_search_tags_text
from app.services.ops.operation_context import OperationContext
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

_INVOICE_TABLES = {
    "business_profile",
    "clients",
    "invoices",
    "line_items",
    "invoice_templates",
    "template_items",
    "audit_log",
    "client_deposit_ledger",
    "client_merge_log",
    "invoice_attachments",
    "client_attachments",
    "bank_import_jobs",
    "bank_transactions",
    "fx_rates_cache",
    "invoice_case_map",
    "invoice_payments",
    "invoice_integrations",
}

_LOCK_KEY = "LOCK:CLIENT_MERGE"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")

_MATTER_CLIENT_ID_NAMESPACES = (
    "domestic_patent",
    "domestic_design",
    "domestic_trademark",
    "incoming_patent",
    "incoming_design",
    "incoming_trademark",
    "outgoing_patent",
    "outgoing_design",
    "outgoing_trademark",
    "pct",
    "litigation",
    "misc",
    # Canonical helper namespace (best-effort)
    "basic",
)

_CLIENT_MERGE_FILL_FIELDS = (
    "type",
    "registration_number",
    "contact_person",
    "manager",
    "email",
    "phone",
    "address",
    "biz_reg_number",
    "biz_company_name",
    "biz_representative_name",
    "biz_opening_date",
    "biz_corp_registration_number",
    "biz_business_location",
    "biz_head_office_location",
    "biz_business_type",
    "biz_tax_invoice_email",
)

_CLIENT_MERGE_UNIQUE_FIELDS = ("party_id", "ipm_party_id", "ipm_client_id")

_EXTRA_MERGE_SKIP_KEYS = {
    "merge_overflow",
    "_merge_overflow",
    "biz_reg_file",
}

_MATTER_CLIENT_DISPLAY_KEYS = ("client_name",)
_MATTER_APPLICANT_DISPLAY_KEYS = (
    "applicant_name",
    "application_applicant_name",
    "applicant_registrant",
)


@dataclass(frozen=True)
class ORMFkRule:
    model: Any
    col: str
    name: str


@dataclass(frozen=True)
class SQLFkRule:
    table: str
    col: str
    name: str


class ClientMergeService:
    """Merge CRM clients and propagate changes across CRM + invoice modules."""

    @classmethod
    def merge_clients(
        cls,
        *,
        target_client_id: int,
        source_client_ids: List[int],
        merge_notes: bool = True,
        merged_by: Optional[int] = None,
        reason: Optional[str] = None,
        backup_required: bool = True,
        backup_attachments: bool = True,
    ) -> Dict[str, Any]:
        tgt = int(target_client_id)
        src_ids = cls._normalize_ids(source_client_ids, exclude={tgt})
        if not src_ids:
            raise ValueError("source_client_ids is empty after normalization.")

        backup_info: Dict[str, Any] = {}
        if backup_required:
            backup_info = cls._create_premerge_backup(
                tags=["client-merge", f"target:{tgt}"] + [f"source:{i}" for i in src_ids],
                note=(reason or f"CRM client merge target={tgt} sources={src_ids}"),
                include_attachments=backup_attachments,
            )

        op_targets = {"target_id": tgt, "source_ids": src_ids}
        op_summary = {
            "reason": reason,
            "backup": backup_info,
        }

        # Removed explicit db.session.begin() to avoid InvalidRequestError
        # The session is effectively already in a transaction in the request context.
        # However, because OperationContext usually expects to commit, we need to handle it.
        # But wait, OperationContext calls op.commit() at the end (line 236).
        # We just need to remove the outer 'with db.session.begin():' and unindent.

        try:
            with OperationContext(
                action="client.merge",
                risk_level="HIGH",
                undo_supported=True,
                undo_deadline_at=datetime.utcnow() + timedelta(days=7),
                targets_json=op_targets,
                summary_json=op_summary,
                preop_backup_required=False,
            ) as op:
                cls._acquire_merge_lock()

                ids_all = [tgt] + list(src_ids)
                clients = (
                    Client.query.filter(Client.id.in_(ids_all))
                    .order_by(Client.id)
                    .with_for_update()
                    .all()
                )
                if len(clients) != len(ids_all):
                    found = {c.id for c in clients}
                    missing = [i for i in ids_all if i not in found]
                    raise ValueError(f"Some client ids do not exist: {missing}")

                target = next(c for c in clients if c.id == tgt)
                sources = [c for c in clients if c.id != tgt]

                if getattr(target, "is_deleted", False):
                    raise ValueError(f"Target client is deleted: {tgt}")
                for s in sources:
                    if getattr(s, "is_deleted", False):
                        raise ValueError(f"Source client is deleted: {s.id}")

                backup_set_id = None
                if backup_info.get("path"):
                    retention_days = int(current_app.config.get("BACKUP_RETENTION_DAYS", 30) or 30)
                    backup_created_at = datetime.utcnow()
                    backup_set = BackupSet(
                        created_at=backup_created_at,
                        type="preop",
                        reason="client.merge",
                        artifact_paths_json={"db": backup_info.get("path")},
                        verify_status="not_checked",
                        retention_until=backup_created_at + timedelta(days=max(retention_days, 1)),
                    )
                    db.session.add(backup_set)
                    db.session.flush()
                    backup_set_id = backup_set.id
                    backup_info["backup_set_id"] = backup_set_id

                snapshot = {
                    "target_before": cls._snapshot_client_row(target),
                    "sources_before": [cls._snapshot_client_row(s) for s in sources],
                    "reason": reason,
                    "merge_notes": bool(merge_notes),
                    "backup": backup_info,
                    "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }

                field_merge = cls._merge_client_profile(target, sources)
                snapshot["client_field_merge"] = field_merge

                crm_notes_appended = None
                if merge_notes:
                    crm_notes_appended = cls._append_notes_to_target(target, sources)
                snapshot["crm_notes_appended"] = crm_notes_appended

                source_before_delete: Dict[int, Dict[str, Any]] = {}
                for s in sources:
                    before_source = cls._snapshot_client_row(s)
                    before_source["deleted_at"] = s.deleted_at.isoformat() if s.deleted_at else None
                    before_source["deleted_by"] = s.deleted_by
                    before_source["delete_reason"] = s.delete_reason
                    before_source["deleted_op_id"] = s.deleted_op_id
                    source_before_delete[int(s.id)] = before_source

                unique_moves = field_merge.get("unique_moves") or {}
                source_party_ids = cls._source_primary_party_link_ids(sources)

                # Prevent premature autoflush before we clear/move unique identifiers.
                # Otherwise, assigning a unique field on target (e.g. ipm_client_id)
                # can violate the unique constraint while the source still holds it.
                with db.session.no_autoflush:
                    moved_counts = cls._move_client_fks(src_ids, tgt)

                    invoice_summary = cls._merge_invoice_side(
                        target=target,
                        sources=sources,
                        merge_notes=merge_notes,
                        merged_by=merged_by,
                        deleted_op_id=op.operation.id,
                    )
                    snapshot["invoice"] = invoice_summary

                    cleared_unique_fields = cls._clear_moved_unique_fields_source_first(
                        target=target,
                        sources=sources,
                        unique_moves=unique_moves,
                    )
                    if cleared_unique_fields:
                        snapshot["source_unique_cleared"] = cleared_unique_fields
                    snapshot["target_after"] = cls._snapshot_client_row(target)

                    moved_counts.update(
                        cls._move_ipm_matter_links(
                            target=target,
                            sources=sources,
                            source_party_ids=source_party_ids,
                        )
                    )
                    snapshot["moved_counts"] = moved_counts

                now = datetime.utcnow()
                for s in sources:
                    before_source = source_before_delete.get(int(s.id))
                    if before_source is None:
                        before_source = cls._snapshot_client_row(s)
                        before_source["deleted_at"] = (
                            s.deleted_at.isoformat() if s.deleted_at else None
                        )
                        before_source["deleted_by"] = s.deleted_by
                        before_source["delete_reason"] = s.delete_reason
                        before_source["deleted_op_id"] = s.deleted_op_id

                    s.is_deleted = True
                    s.deleted_at = now
                    s.deleted_by = merged_by
                    s.delete_reason = reason or "client.merge"
                    s.deleted_op_id = op.operation.id
                    try:
                        s.external_invoice_client_id = None
                    except Exception as exc:
                        # Best-effort: attribute may not exist in some deployments.
                        report_swallowed_exception(
                            exc,
                            context="client_merge_service.merge_clients.clear_external_invoice_client_id",
                            log_key="client_merge_service.merge_clients.clear_external_invoice_client_id",
                            log_window_seconds=300,
                        )

                    after_source = cls._snapshot_client_row(s)
                    after_source["deleted_at"] = s.deleted_at.isoformat() if s.deleted_at else None
                    after_source["deleted_by"] = s.deleted_by
                    after_source["delete_reason"] = s.delete_reason
                    after_source["deleted_op_id"] = s.deleted_op_id

                    op.add_change(
                        entity_type="Client",
                        entity_id=str(s.id),
                        change_type="soft_delete",
                        before=before_source,
                        after=after_source,
                        meta={"target_id": tgt, "moved_counts": moved_counts},
                    )

                log = CRMClientMergeLog(
                    target_client_id=tgt,
                    source_client_ids_json=json.dumps(src_ids, ensure_ascii=False),
                    payload_json=json.dumps(snapshot, ensure_ascii=False),
                    merged_by=merged_by,
                )
                db.session.add(log)
                db.session.flush()

                snapshot["merge_log_id"] = log.id
                snapshot["completed_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                log.payload_json = json.dumps(snapshot, ensure_ascii=False)

                op.operation.summary_json = {
                    "moved_counts": moved_counts,
                    "invoice": invoice_summary,
                    "merge_log_id": log.id,
                    "backup": backup_info,
                }
                op.add_change(
                    entity_type="Client",
                    entity_id=str(tgt),
                    change_type="update",
                    before=snapshot.get("target_before"),
                    after=snapshot.get("target_after"),
                    meta={
                        "source_ids": src_ids,
                        "moved_counts": moved_counts,
                        "client_field_merge": field_merge,
                    },
                )

                op.commit()
                db.session.commit()

        except Exception:
            db.session.rollback()
            raise

        return {
            "ok": True,
            "target_client_id": tgt,
            "source_client_ids": src_ids,
            "backup": backup_info,
            "moved_counts": moved_counts,
            "invoice": invoice_summary,
        }

    @classmethod
    def _get_client_fk_rules(cls) -> List[ORMFkRule]:
        return [
            ORMFkRule(Case, "client_id", "cases.client_id"),
            ORMFkRule(CRMContact, "client_id", "crm_contacts.client_id"),
            ORMFkRule(CRMOpportunity, "client_id", "crm_opportunities.client_id"),
            ORMFkRule(CRMActivity, "client_id", "crm_activities.client_id"),
            ORMFkRule(CRMLead, "converted_client_id", "crm_leads.converted_client_id"),
        ]

    @classmethod
    def _get_invoice_fk_rules(cls) -> List[SQLFkRule]:
        return [
            SQLFkRule("invoices", "client_id", "billing_invoices.invoices.client_id"),
            SQLFkRule(
                "client_deposit_ledger",
                "client_id",
                "billing_invoices.client_deposit_ledger.client_id",
            ),
        ]

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, set)):
            return all(ClientMergeService._is_blank(v) for v in value)
        if isinstance(value, dict):
            return len(value) == 0
        return False

    @staticmethod
    def _normalize_scalar(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @classmethod
    def _values_equal(cls, left: Any, right: Any) -> bool:
        return cls._normalize_scalar(left) == cls._normalize_scalar(right)

    @staticmethod
    def _stringify_overflow_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            val = value.strip()
            return val if val else None
        if isinstance(value, (int, float, bool)):
            return str(value)
        return None

    @classmethod
    def _add_overflow(
        cls,
        overflow: Dict[str, List[Dict[str, Any]]],
        key: str,
        value: Any,
        source_id: Optional[int],
    ) -> None:
        serialized = cls._stringify_overflow_value(value)
        if serialized is None:
            return
        bucket = overflow.setdefault(key, [])
        for item in bucket:
            if item.get("value") == serialized:
                return
        payload: Dict[str, Any] = {"value": serialized}
        if source_id is not None:
            payload["source_id"] = int(source_id)
        bucket.append(payload)

    @classmethod
    def _pick_best_value(cls, values: List[Any]) -> Any:
        if not values:
            return None
        if len(values) == 1:
            return values[0]

        def _score(val: Any) -> int:
            if isinstance(val, str):
                return len(val.strip())
            try:
                return int(len(val))  # type: ignore[arg-type]
            except Exception:
                return 1

        return sorted(values, key=_score, reverse=True)[0]

    @classmethod
    def _list_item_key(cls, item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        try:
            return json.dumps(item, sort_keys=True, ensure_ascii=False)
        except Exception:
            return str(item)

    @classmethod
    def _merge_list_unique(cls, target_list: Any, source_list: Any) -> Tuple[List[Any], bool]:
        tgt = target_list if isinstance(target_list, list) else []
        src = source_list if isinstance(source_list, list) else []
        out: List[Any] = []
        seen: set[str] = set()

        for item in tgt:
            if cls._is_blank(item):
                continue
            key = cls._list_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)

        added = False
        for item in src:
            if cls._is_blank(item):
                continue
            key = cls._list_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            added = True

        return out, added

    @classmethod
    def _merge_dict_fill(
        cls,
        target_dict: Dict[str, Any],
        source_dict: Dict[str, Any],
        overflow: Dict[str, List[Dict[str, Any]]],
        source_id: Optional[int],
        prefix: str,
    ) -> bool:
        changed = False
        for key, src_val in source_dict.items():
            if cls._is_blank(src_val):
                continue
            tgt_val = target_dict.get(key)
            if key not in target_dict or cls._is_blank(tgt_val):
                target_dict[key] = deepcopy(src_val)
                changed = True
                continue
            if isinstance(tgt_val, dict) and isinstance(src_val, dict):
                if cls._merge_dict_fill(
                    tgt_val,
                    src_val,
                    overflow,
                    source_id,
                    f"{prefix}.{key}",
                ):
                    changed = True
                continue
            if isinstance(tgt_val, list) and isinstance(src_val, list):
                merged, added = cls._merge_list_unique(tgt_val, src_val)
                if added:
                    target_dict[key] = merged
                    changed = True
                continue
            if not cls._values_equal(tgt_val, src_val):
                cls._add_overflow(overflow, f"{prefix}.{key}", src_val, source_id)
        return changed

    @classmethod
    def _merge_client_profile(cls, target: Client, sources: List[Client]) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "updated_fields": {},
            "unique_moves": {},
            "extra_updates": [],
            "overflow": {},
        }

        overflow_existing: Dict[str, List[Dict[str, Any]]] = {}
        target_extra = target.extra if isinstance(target.extra, dict) else {}
        if isinstance(target_extra, dict):
            existing = target_extra.get("merge_overflow")
            if isinstance(existing, dict):
                for k, v in existing.items():
                    if isinstance(v, list):
                        overflow_existing[k] = list(v)

        overflow: Dict[str, List[Dict[str, Any]]] = deepcopy(overflow_existing)

        # Move unique identifiers (party ids) when target is empty.
        for field in _CLIENT_MERGE_UNIQUE_FIELDS:
            target_val = getattr(target, field, None)
            if cls._is_blank(target_val):
                candidates: List[Any] = []
                for s in sources:
                    v = getattr(s, field, None)
                    if cls._is_blank(v):
                        continue
                    if all(not cls._values_equal(v, c) for c in candidates):
                        candidates.append(v)
                if len(candidates) == 1:
                    chosen = candidates[0]
                    setattr(target, field, chosen)
                    result["unique_moves"][field] = chosen
                elif len(candidates) > 1:
                    for s in sources:
                        v = getattr(s, field, None)
                        if not cls._is_blank(v):
                            cls._add_overflow(overflow, field, v, getattr(s, "id", None))
            else:
                for s in sources:
                    v = getattr(s, field, None)
                    if not cls._is_blank(v) and not cls._values_equal(v, target_val):
                        cls._add_overflow(overflow, field, v, getattr(s, "id", None))

        # Fill basic fields when target is blank; record conflicts in overflow.
        for field in _CLIENT_MERGE_FILL_FIELDS:
            target_val = getattr(target, field, None)
            candidates: List[Any] = []
            for s in sources:
                v = getattr(s, field, None)
                if cls._is_blank(v):
                    continue
                candidates.append(v)

            if not candidates:
                continue

            if cls._is_blank(target_val):
                chosen = cls._pick_best_value(candidates)
                if not cls._is_blank(chosen):
                    setattr(target, field, chosen)
                    result["updated_fields"][field] = chosen
                for s in sources:
                    v = getattr(s, field, None)
                    if cls._is_blank(v):
                        continue
                    if chosen is not None and cls._values_equal(v, chosen):
                        continue
                    cls._add_overflow(overflow, field, v, getattr(s, "id", None))
            else:
                for s in sources:
                    v = getattr(s, field, None)
                    if cls._is_blank(v):
                        continue
                    if cls._values_equal(v, target_val):
                        continue
                    cls._add_overflow(overflow, field, v, getattr(s, "id", None))

        # Merge extra JSON fields
        extra_updates: set[str] = set()
        merged_extra: Dict[str, Any] = dict(target_extra) if isinstance(target_extra, dict) else {}

        for s in sources:
            src_extra = s.extra if isinstance(s.extra, dict) else {}
            if not isinstance(src_extra, dict):
                continue
            for key, src_val in src_extra.items():
                if key in _EXTRA_MERGE_SKIP_KEYS:
                    continue
                if cls._is_blank(src_val):
                    continue
                tgt_val = merged_extra.get(key)
                if key not in merged_extra or cls._is_blank(tgt_val):
                    merged_extra[key] = deepcopy(src_val)
                    extra_updates.add(key)
                    continue
                if isinstance(tgt_val, dict) and isinstance(src_val, dict):
                    if cls._merge_dict_fill(
                        tgt_val,
                        src_val,
                        overflow,
                        getattr(s, "id", None),
                        f"extra.{key}",
                    ):
                        extra_updates.add(key)
                    continue
                if isinstance(tgt_val, list) and isinstance(src_val, list):
                    merged_list, added = cls._merge_list_unique(tgt_val, src_val)
                    if added:
                        merged_extra[key] = merged_list
                        extra_updates.add(key)
                    continue
                if not cls._values_equal(tgt_val, src_val):
                    cls._add_overflow(overflow, f"extra.{key}", src_val, getattr(s, "id", None))

        if overflow:
            merged_extra["merge_overflow"] = overflow

        if extra_updates or overflow:
            target.extra = merged_extra
            flag_modified(target, "extra")
            result["extra_updates"] = sorted(extra_updates)

        if result["updated_fields"] or result["unique_moves"] or extra_updates:
            values = [
                getattr(target, "name", None),
                getattr(target, "biz_company_name", None),
                (merged_extra.get("name_en") if isinstance(merged_extra, dict) else None),
                (merged_extra.get("tax_company_name") if isinstance(merged_extra, dict) else None),
                getattr(target, "registration_number", None),
                getattr(target, "biz_reg_number", None),
                getattr(target, "biz_corp_registration_number", None),
                getattr(target, "phone", None),
            ]
            tags = build_client_search_tags_text(values)
            target.search_tags = tags or None

        result["overflow"] = overflow
        return result

    @staticmethod
    def _compact_name(value: Any) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _source_client_names(cls, sources: List[Client]) -> set[str]:
        return {
            name
            for name in (cls._compact_name(getattr(source, "name", None)) for source in sources)
            if name
        }

    @classmethod
    def _source_primary_party_link_ids(cls, sources: List[Client]) -> set[str]:
        out: set[str] = set()
        for source in sources:
            party_id = str(
                getattr(source, "party_id", "") or getattr(source, "ipm_party_id", "") or ""
            ).strip()
            if party_id:
                out.add(party_id)
        return out

    @classmethod
    def _is_source_client_name(cls, value: Any, source_names: set[str]) -> bool:
        if not source_names:
            return False
        return cls._compact_name(value) in source_names

    @classmethod
    def _clear_moved_unique_fields_from_sources(
        cls, sources: List[Client], unique_moves: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        if not unique_moves:
            return {}

        cleared: Dict[str, Dict[str, Any]] = {}
        for source in sources:
            source_id = str(getattr(source, "id", "") or "")
            for field, value in unique_moves.items():
                if field not in _CLIENT_MERGE_UNIQUE_FIELDS:
                    continue
                try:
                    if not cls._values_equal(getattr(source, field, None), value):
                        continue
                    setattr(source, field, None)
                except Exception:
                    continue
                cleared.setdefault(source_id, {})[field] = value
        return cleared

    @classmethod
    def _clear_moved_unique_fields_source_first(
        cls,
        *,
        target: Client,
        sources: List[Client],
        unique_moves: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        cleared = cls._clear_moved_unique_fields_from_sources(sources, unique_moves)
        if cleared:
            # Flush only sources first so the target can adopt unique identifiers safely.
            db.session.flush(sources)

        for field, value in (unique_moves or {}).items():
            if field not in _CLIENT_MERGE_UNIQUE_FIELDS:
                continue
            try:
                setattr(target, field, value)
            except Exception:
                continue
        return cleared

    @classmethod
    def _move_client_fks(cls, source_ids: List[int], target_id: int) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for rule in cls._get_client_fk_rules():
            col = getattr(rule.model, rule.col)
            q = rule.model.query.filter(col.in_(source_ids))
            n = q.update({rule.col: target_id}, synchronize_session=False)
            out[rule.name] = int(n or 0)
        return out

    @classmethod
    def _move_ipm_matter_links(
        cls,
        *,
        target: Client,
        sources: List[Client],
        source_party_ids: Optional[set[str]] = None,
    ) -> Dict[str, int]:
        """
        Move Legacy IPM(Matter) links that are not represented as simple FK columns.

        - MatterCustomField.data.client_id (JSON) : used by CRM↔Matter linking in new UI
        - matter_party_role.party_id : used by migrated v2 data + v_matter_overview aggregation
        - Denormalized display names that exactly equal a merged source client name

        Best-effort: failures are swallowed and reported.
        """
        out: Dict[str, int] = {
            "ipm.matter_custom_field.data.client_id": 0,
            "ipm.matter_party_role.party_id": 0,
            "ipm.matter_party_role.raw_text": 0,
            "ipm.case_flat_index": 0,
        }
        affected_matter_ids: set[str] = set()
        target_name = cls._compact_name(getattr(target, "name", None))
        source_names = cls._source_client_names(sources)

        # 1) MatterCustomField.data.client_id → target.id, and keep exact display-name
        # copies in sync. This prevents merged clients from continuing to display as
        # the deleted source client on case views/lists.
        try:
            from sqlalchemy import String, cast, or_

            from app.models.ip_records import MatterCustomField

            tgt_id = str(getattr(target, "id", "") or "").strip()
            src_ids = [
                str(getattr(s, "id", "") or "").strip()
                for s in sources
                if str(getattr(s, "id", "") or "").strip()
            ]
            if tgt_id and src_ids:
                try:
                    bind = db.session.get_bind()
                    dialect = (getattr(bind.dialect, "name", "") or "").lower() if bind else ""
                except Exception:
                    dialect = ""

                if dialect.startswith("postgres"):
                    client_id_expr = MatterCustomField.data["client_id"].as_string()
                else:
                    client_id_expr = func.json_extract(MatterCustomField.data, "$.client_id")
                client_id_expr = cast(client_id_expr, String)

                filters = [client_id_expr.in_(src_ids)]
                if source_names:
                    data_text = cast(MatterCustomField.data, String)
                    filters.extend(data_text.ilike(f"%{name}%") for name in source_names)

                rows = (
                    MatterCustomField.query.filter(
                        MatterCustomField.namespace.in_(_MATTER_CLIENT_ID_NAMESPACES)
                    )
                    .filter(or_(*filters))
                    .all()
                )
                updated = 0
                for row in rows:
                    try:
                        payload = row.data or {}
                        if not isinstance(payload, dict):
                            payload = {}
                        new_payload = dict(payload)
                        changed = False

                        if str(new_payload.get("client_id") or "").strip() in src_ids:
                            new_payload["client_id"] = tgt_id
                            if target_name:
                                new_payload["client_name"] = target_name
                            changed = True

                        if target_name:
                            for key in _MATTER_CLIENT_DISPLAY_KEYS + _MATTER_APPLICANT_DISPLAY_KEYS:
                                if cls._is_source_client_name(new_payload.get(key), source_names):
                                    new_payload[key] = target_name
                                    changed = True

                        if changed:
                            row.data = new_payload
                            updated += 1
                            mid = str(getattr(row, "matter_id", "") or "").strip()
                            if mid:
                                affected_matter_ids.add(mid)
                    except Exception:
                        continue
                out["ipm.matter_custom_field.data.client_id"] = int(updated)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_merge_service._move_ipm_matter_links.matter_custom_field",
                log_key="client_merge_service._move_ipm_matter_links.matter_custom_field",
                log_window_seconds=300,
            )

        # 2) matter_party_role.party_id/raw_text → target (migrated linkage + display)
        try:
            from app.models.ip_records import MatterPartyRole

            tgt_party_id = (
                str(getattr(target, "party_id", "") or getattr(target, "ipm_party_id", "") or "")
            ).strip()
            source_party_ids = source_party_ids or cls._source_primary_party_link_ids(sources)
            src_party_ids = sorted(
                set(source_party_ids) - {""} - ({tgt_party_id} if tgt_party_id else set())
            )
            if tgt_party_id and src_party_ids:
                stmt = text(
                    """
                    UPDATE matter_party_role
                       SET party_id = :tgt
                     WHERE lower(COALESCE(role_code, '')) IN ('client', 'applicant')
                       AND party_id IN :src
                    """
                ).bindparams(bindparam("src", expanding=True))
                res = db.session.execute(stmt, {"tgt": tgt_party_id, "src": src_party_ids})
                out["ipm.matter_party_role.party_id"] = int(getattr(res, "rowcount", 0) or 0)
                if getattr(res, "rowcount", 0):
                    rows = (
                        db.session.execute(
                            text(
                                """
                                SELECT DISTINCT matter_id
                                FROM matter_party_role
                                WHERE party_id = :tgt
                                  AND lower(COALESCE(role_code, '')) IN ('client', 'applicant')
                                """
                            ),
                            {"tgt": tgt_party_id},
                        )
                        .scalars()
                        .all()
                    )
                    affected_matter_ids.update(str(mid).strip() for mid in rows if str(mid).strip())

            if target_name and source_names:
                rows = (
                    MatterPartyRole.query.filter(
                        func.lower(func.coalesce(MatterPartyRole.role_code, "")).in_(
                            ["client", "applicant"]
                        )
                    )
                    .filter(
                        func.trim(func.coalesce(MatterPartyRole.raw_text, "")).in_(
                            sorted(source_names)
                        )
                    )
                    .all()
                )
                updated_raw = 0
                for row in rows:
                    if not cls._is_source_client_name(getattr(row, "raw_text", None), source_names):
                        continue
                    row.raw_text = target_name
                    updated_raw += 1
                    mid = str(getattr(row, "matter_id", "") or "").strip()
                    if mid:
                        affected_matter_ids.add(mid)
                out["ipm.matter_party_role.raw_text"] = int(updated_raw)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_merge_service._move_ipm_matter_links.matter_party_role",
                log_key="client_merge_service._move_ipm_matter_links.matter_party_role",
                log_window_seconds=300,
            )

        if affected_matter_ids:
            try:
                from app.services.case.canonical_field_service import upsert_case_flat_index

                db.session.flush()
                refreshed = 0
                for matter_id in sorted(affected_matter_ids):
                    if upsert_case_flat_index(str(matter_id)):
                        refreshed += 1
                out["ipm.case_flat_index"] = refreshed
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._move_ipm_matter_links.case_flat_index",
                    log_key="client_merge_service._move_ipm_matter_links.case_flat_index",
                    log_window_seconds=300,
                )

        return out

    @classmethod
    def _merge_invoice_side(
        cls,
        *,
        target: Client,
        sources: List[Client],
        merge_notes: bool,
        merged_by: Optional[int],
        deleted_op_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        integrated = billing_subsystem_enabled(current_app)
        unified = unified_clients_enabled()
        prefix = (current_app.config.get("INVOICEAPP_TABLE_PREFIX") or "").strip()

        summary: Dict[str, Any] = {
            "enabled": integrated,
            "unified_clients": unified,
            "table_prefix": prefix,
        }
        if not integrated:
            summary["skipped_reason"] = "billing subsystem is disabled"
            return summary

        if unified:
            invoice_target_id = int(target.id)
            invoice_source_ids = [int(s.id) for s in sources]
            summary["invoice_target_id"] = invoice_target_id
            summary["invoice_source_ids"] = invoice_source_ids
            try:
                invoices_tbl = cls._invoice_actual_table("invoices")
                if not cls._table_exists(invoices_tbl):
                    summary["skipped_reason"] = f"invoice tables not found: {invoices_tbl}"
                    return summary

                moved_counts = cls._move_invoice_fks(
                    source_ids=invoice_source_ids,
                    target_id=invoice_target_id,
                )
                file_moves = cls._move_invoice_client_attachment_files(
                    source_invoice_client_ids=invoice_source_ids,
                    target_invoice_client_id=invoice_target_id,
                    attach_table=cls._invoice_actual_table("client_attachments"),
                )
                moved_counts["billing_invoices.client_attachments.client_id"] = int(
                    file_moves.get("rows_count", 0) or 0
                )
                summary["attachment_file_moves"] = {
                    k: v for k, v in file_moves.items() if k != "renamed_map"
                }
                summary["moved_counts"] = moved_counts
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._merge_invoice_side.unified",
                    log_key="client_merge_service._merge_invoice_side.unified",
                    log_window_seconds=300,
                )
                summary["skipped_reason"] = f"invoice merge failed: {type(exc).__name__}"
                summary["error"] = str(exc)
            return summary

        invoice_target_id = cls._resolve_invoice_client_id(target)
        invoice_source_ids: List[int] = []
        invoice_source_map: List[Tuple[int, int]] = []
        for s in sources:
            sid = cls._resolve_invoice_client_id(s)
            if sid is not None:
                invoice_source_ids.append(int(sid))
                invoice_source_map.append((int(s.id), int(sid)))

        invoice_source_ids = [i for i in invoice_source_ids if i != invoice_target_id]
        invoice_source_ids = cls._normalize_ids(invoice_source_ids, exclude=set())

        adopted_from_client_id = None
        if invoice_target_id is None and invoice_source_ids:
            invoice_target_id = int(invoice_source_ids[0])
            for cid, inv_id in invoice_source_map:
                if int(inv_id) == int(invoice_target_id):
                    adopted_from_client_id = int(cid)
                    break
            target.external_invoice_client_id = invoice_target_id
            if adopted_from_client_id is not None:
                for s in sources:
                    if int(s.id) == int(adopted_from_client_id):
                        try:
                            s.external_invoice_client_id = None
                        except Exception as exc:
                            # Best-effort: attribute may not exist in some deployments.
                            report_swallowed_exception(
                                exc,
                                context="client_merge_service._merge_invoice_side.clear_source_external_invoice_client_id",
                                log_key="client_merge_service._merge_invoice_side.clear_source_external_invoice_client_id",
                                log_window_seconds=300,
                            )
                        break
            invoice_source_ids = [i for i in invoice_source_ids if int(i) != int(invoice_target_id)]

        summary["invoice_target_id"] = invoice_target_id
        summary["invoice_source_ids"] = invoice_source_ids
        summary["adopted_from_client_id"] = adopted_from_client_id

        if invoice_target_id is None:
            summary["skipped_reason"] = "No linked invoice_client_id on target/sources"
            return summary

        cls._update_invoice_client_link(invoice_target_id, target)

        merge_summary = cls._merge_invoice_clients_dual(
            target_invoice_client_id=int(invoice_target_id),
            source_invoice_client_ids=invoice_source_ids,
            merge_notes=merge_notes,
            merged_by=merged_by,
            ipm_target_client_id=int(target.id),
            ipm_target_party_id=(
                (getattr(target, "party_id", None) or getattr(target, "ipm_party_id", None))
            ),
            deleted_op_id=deleted_op_id,
        )
        summary["invoice_client_merge"] = merge_summary
        summary["moved_counts"] = merge_summary.get("moved_counts", {})
        return summary

    @classmethod
    def _move_invoice_fks(cls, *, source_ids: List[int], target_id: int) -> Dict[str, int]:
        if not source_ids:
            return {r.name: 0 for r in cls._get_invoice_fk_rules()}

        out: Dict[str, int] = {}
        for rule in cls._get_invoice_fk_rules():
            table = cls._invoice_actual_table(rule.table)
            stmt = text(
                f"UPDATE {table} SET {rule.col} = :target WHERE {rule.col} IN :src"
            ).bindparams(bindparam("src", expanding=True))
            res = db.session.execute(
                stmt, {"target": int(target_id), "src": [int(i) for i in source_ids]}
            )
            out[rule.name] = int(getattr(res, "rowcount", 0) or 0)
        return out

    @classmethod
    def _merge_invoice_clients_dual(
        cls,
        *,
        target_invoice_client_id: int,
        source_invoice_client_ids: List[int],
        merge_notes: bool,
        merged_by: Optional[int],
        ipm_target_client_id: int,
        ipm_target_party_id: Optional[str],
        deleted_op_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        src_ids = [
            int(i) for i in source_invoice_client_ids if int(i) != int(target_invoice_client_id)
        ]
        if not src_ids:
            return {"performed": False, "reason": "no source invoice clients"}

        clients_tbl = cls._invoice_actual_table("clients")
        merge_log_tbl = cls._invoice_actual_table("client_merge_log")
        inv_tbl = cls._invoice_actual_table("invoices")
        led_tbl = cls._invoice_actual_table("client_deposit_ledger")
        attach_tbl = cls._invoice_actual_table("client_attachments")

        src_rows = (
            db.session.execute(
                text(f"SELECT * FROM {clients_tbl} WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": src_ids},
            )
            .mappings()
            .all()
        )
        sources_json = json.dumps([dict(r) for r in src_rows], ensure_ascii=False)

        inv_rows = db.session.execute(
            text(f"SELECT id, client_id FROM {inv_tbl} WHERE client_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": src_ids},
        ).fetchall()
        invoice_map = {str(int(_r[0])): int(_r[1]) for _r in (inv_rows or []) if _r is not None}

        led_rows = db.session.execute(
            text(f"SELECT id, client_id FROM {led_tbl} WHERE client_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": src_ids},
        ).fetchall()
        ledger_map = {str(int(_r[0])): int(_r[1]) for _r in (led_rows or []) if _r is not None}

        att_rows = db.session.execute(
            text(
                f"SELECT id, client_id, stored_name FROM {attach_tbl} WHERE client_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": src_ids},
        ).fetchall()
        attachments_map = {
            str(int(_r[0])): {
                "client_id": int(_r[1]),
                "stored_name": str(_r[2] or ""),
            }
            for _r in (att_rows or [])
            if _r is not None
        }

        # Move invoice references to target
        db.session.execute(
            text(f"UPDATE {inv_tbl} SET client_id=:tid WHERE client_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"tid": int(target_invoice_client_id), "ids": src_ids},
        )
        db.session.execute(
            text(f"UPDATE {led_tbl} SET client_id=:tid WHERE client_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"tid": int(target_invoice_client_id), "ids": src_ids},
        )

        # Attachments: move files and update DB
        file_moves = cls._move_invoice_client_attachment_files(
            source_invoice_client_ids=src_ids,
            target_invoice_client_id=int(target_invoice_client_id),
            attach_table=attach_tbl,
            attachment_rows=att_rows,
        )
        for att_id, new_name in file_moves.get("renamed_map", {}).items():
            if str(att_id) in attachments_map:
                attachments_map[str(att_id)]["stored_name_after"] = new_name

        notes_appended = None
        if merge_notes:
            trow = db.session.execute(
                text(f"SELECT notes FROM {clients_tbl} WHERE id = :id"),
                {"id": int(target_invoice_client_id)},
            ).fetchone()
            tnotes = (trow[0] if trow else "") or ""
            parts = []
            for r in src_rows:
                n = (r.get("notes") or "").strip()
                if n:
                    parts.append(f"[MERGED from invoice_client#{r.get('id')}] {n}")
            if parts:
                notes_appended = "\n".join(parts)
                merged_notes = (
                    (tnotes + "\n\n" + notes_appended).strip() if tnotes else notes_appended
                )
                db.session.execute(
                    text(f"UPDATE {clients_tbl} SET notes = :notes WHERE id = :id"),
                    {"notes": merged_notes, "id": int(target_invoice_client_id)},
                )

        cls._update_invoice_client_link(
            int(target_invoice_client_id),
            ipm_client_id=ipm_target_client_id,
            ipm_party_id=ipm_target_party_id,
        )

        invoice_map_json = json.dumps(
            {
                "version": 2,
                "invoices": invoice_map,
                "client_deposit_ledger": ledger_map,
                "client_attachments": attachments_map,
            },
            ensure_ascii=False,
        )

        db.session.execute(
            text(
                f"""
                INSERT INTO {merge_log_tbl}
                (target_id, sources_json, invoice_map_json, notes_appended, merged_by)
                VALUES (:target_id, :sources_json, :invoice_map_json, :notes_appended, :merged_by)
                """
            ),
            {
                "target_id": int(target_invoice_client_id),
                "sources_json": sources_json,
                "invoice_map_json": invoice_map_json,
                "notes_appended": notes_appended,
                "merged_by": merged_by,
            },
        )

        deleted_at = datetime.utcnow().isoformat(timespec="seconds")
        db.session.execute(
            text(
                f"""
                UPDATE {clients_tbl}
                   SET is_deleted = 1,
                       deleted_at = :deleted_at,
                       deleted_by = :deleted_by,
                       delete_reason = :delete_reason,
                       deleted_op_id = :deleted_op_id
                 WHERE id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {
                "ids": src_ids,
                "deleted_at": deleted_at,
                "deleted_by": merged_by,
                "delete_reason": "client.merge",
                "deleted_op_id": deleted_op_id,
            },
        )

        return {
            "performed": True,
            "target_invoice_client_id": int(target_invoice_client_id),
            "source_invoice_client_ids": src_ids,
            "notes_appended": bool(notes_appended),
            "file_moves": {k: v for k, v in file_moves.items() if k != "renamed_map"},
            "moved_counts": {
                "billing_invoices.invoices.client_id": len(invoice_map),
                "billing_invoices.client_deposit_ledger.client_id": len(ledger_map),
                "billing_invoices.client_attachments.client_id": len(attachments_map),
            },
        }

    @classmethod
    def _move_invoice_client_attachment_files(
        cls,
        *,
        source_invoice_client_ids: List[int],
        target_invoice_client_id: int,
        attach_table: str,
        attachment_rows: Optional[List[Tuple[Any, ...]]] = None,
    ) -> Dict[str, Any]:
        if not source_invoice_client_ids:
            return {
                "moved_files": 0,
                "renamed": 0,
                "missing_files": 0,
                "copy_failures": 0,
                "delete_failures": 0,
                "rows_count": 0,
                "renamed_map": {},
                "missing_items": [],
                "copy_failures_items": [],
                "delete_failures_items": [],
            }

        base = current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients")
        tgt_dir = os.path.join(base, f"client_{int(target_invoice_client_id)}")

        rows = attachment_rows or []
        if not rows:
            rows = db.session.execute(
                text(
                    f"SELECT id, client_id, stored_name FROM {attach_table} WHERE client_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [int(i) for i in source_invoice_client_ids]},
            ).fetchall()

        by_client: Dict[int, List[Tuple[int, str]]] = {}
        for r in rows or []:
            try:
                att_id = int(r[0])
                client_id = int(r[1])
                stored_name = str(r[2] or "")
                stored_name = os.path.basename(stored_name)
            except Exception:
                continue
            by_client.setdefault(client_id, []).append((att_id, stored_name))

        total_rows = sum(len(v) for v in by_client.values())
        if total_rows <= 0:
            return {
                "moved_files": 0,
                "renamed": 0,
                "missing_files": 0,
                "copy_failures": 0,
                "delete_failures": 0,
                "rows_count": 0,
                "renamed_map": {},
                "missing_items": [],
                "copy_failures_items": [],
                "delete_failures_items": [],
            }

        try:
            os.makedirs(tgt_dir, exist_ok=True)
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_merge_service._merge_invoice_attachments.ensure_target_dir",
                log_key="client_merge_service._merge_invoice_attachments.ensure_target_dir",
                log_window_seconds=300,
            )
            denied_items: List[Dict[str, Any]] = []
            for sid, items in by_client.items():
                for att_id, stored_name in items:
                    denied_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "dst_name": stored_name,
                            "error": str(exc),
                        }
                    )
            return {
                "moved_files": 0,
                "renamed": 0,
                "missing_files": 0,
                "copy_failures": len(denied_items),
                "delete_failures": 0,
                "rows_count": total_rows,
                "renamed_map": {},
                "missing_items": [],
                "copy_failures_items": denied_items,
                "delete_failures_items": [],
            }

        moved_files = 0
        renamed = 0
        missing = 0
        copy_failures = 0
        delete_failures = 0
        renamed_map: Dict[int, str] = {}
        missing_items: List[Dict[str, Any]] = []
        copy_failures_items: List[Dict[str, Any]] = []
        delete_failures_items: List[Dict[str, Any]] = []
        req_id = getattr(g, "request_id", None)

        for sid, items in by_client.items():
            src_dir = os.path.join(base, f"client_{int(sid)}")
            src_dir_real = os.path.normcase(os.path.realpath(src_dir))
            src_dir_exists = os.path.isdir(src_dir)
            for att_id, stored_name in items:
                if not stored_name:
                    missing += 1
                    missing_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": "",
                            "reason": "stored_name_empty",
                        }
                    )
                    continue
                if not src_dir_exists:
                    missing += 1
                    missing_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "reason": "source_dir_missing",
                        }
                    )
                    continue
                src_path = os.path.join(src_dir, stored_name)
                src_path_real = os.path.normcase(os.path.realpath(src_path))
                if not src_path_real.startswith(src_dir_real + os.sep):
                    missing += 1
                    missing_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "reason": "invalid_path",
                        }
                    )
                    continue
                if not os.path.exists(src_path):
                    missing += 1
                    missing_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "reason": "source_file_missing",
                        }
                    )
                    continue
                dst_name = stored_name
                dst_path = os.path.join(tgt_dir, dst_name)
                if os.path.exists(dst_path):
                    dst_name = cls._unique_filename(tgt_dir, stored_name)
                    dst_path = os.path.join(tgt_dir, dst_name)
                try:
                    shutil.copy2(src_path, dst_path)
                    if os.path.getsize(src_path) != os.path.getsize(dst_path):
                        raise IOError("copy_verify_failed")
                except Exception as exc:
                    copy_failures += 1
                    copy_failures_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "dst_name": dst_name,
                            "error": str(exc),
                        }
                    )
                    try:
                        if os.path.exists(dst_path):
                            os.remove(dst_path)
                    except Exception as cleanup_exc:
                        # Best-effort cleanup of partially copied attachment.
                        report_swallowed_exception(
                            cleanup_exc,
                            context="client_merge_service._merge_invoice_attachments.cleanup_dst_remove",
                            log_key="client_merge_service._merge_invoice_attachments.cleanup_dst_remove",
                            log_window_seconds=300,
                        )
                    current_app.logger.exception(
                        "Client merge attachment copy failed (request_id=%s, client_id=%s, attachment_id=%s)",
                        req_id,
                        sid,
                        att_id,
                    )
                    continue

                if dst_name != stored_name:
                    renamed += 1
                    renamed_map[int(att_id)] = dst_name

                try:
                    os.remove(src_path)
                except Exception as exc:
                    delete_failures += 1
                    delete_failures_items.append(
                        {
                            "client_id": int(sid),
                            "attachment_id": int(att_id),
                            "stored_name": stored_name,
                            "dst_name": dst_name,
                            "error": str(exc),
                        }
                    )
                    current_app.logger.exception(
                        "Client merge attachment delete failed (request_id=%s, client_id=%s, attachment_id=%s)",
                        req_id,
                        sid,
                        att_id,
                    )
                    continue

                moved_files += 1

            try:
                if os.path.isdir(src_dir) and not os.listdir(src_dir):
                    os.rmdir(src_dir)
            except Exception as cleanup_exc:
                # Best-effort cleanup of empty source directory.
                report_swallowed_exception(
                    cleanup_exc,
                    context="client_merge_service._merge_invoice_attachments.cleanup_src_dir",
                    log_key="client_merge_service._merge_invoice_attachments.cleanup_src_dir",
                    log_window_seconds=300,
                )

            db.session.execute(
                text(f"UPDATE {attach_table} SET client_id = :tid WHERE client_id = :sid"),
                {"tid": int(target_invoice_client_id), "sid": int(sid)},
            )

        for att_id, new_name in renamed_map.items():
            db.session.execute(
                text(f"UPDATE {attach_table} SET stored_name = :sn WHERE id = :id"),
                {"sn": new_name, "id": int(att_id)},
            )

        return {
            "moved_files": moved_files,
            "renamed": renamed,
            "missing_files": missing,
            "copy_failures": copy_failures,
            "delete_failures": delete_failures,
            "rows_count": total_rows,
            "renamed_map": renamed_map,
            "missing_items": missing_items,
            "copy_failures_items": copy_failures_items,
            "delete_failures_items": delete_failures_items,
        }

    @staticmethod
    def collect_attachment_move_issues(invoice_summary: Dict[str, Any]) -> Dict[str, int]:
        def _read(src: Optional[Dict[str, Any]], out: Dict[str, int]) -> None:
            if not src:
                return
            out["missing"] += int(src.get("missing_files", 0) or 0)
            out["copy_failures"] += int(src.get("copy_failures", 0) or 0)
            out["delete_failures"] += int(src.get("delete_failures", 0) or 0)

        issues = {"missing": 0, "copy_failures": 0, "delete_failures": 0}
        _read(invoice_summary.get("attachment_file_moves"), issues)
        merge_summary = invoice_summary.get("invoice_client_merge") or {}
        _read(merge_summary.get("file_moves"), issues)
        return issues

    @classmethod
    def _invoice_actual_table(cls, table: str) -> str:
        prefix = (current_app.config.get("INVOICEAPP_TABLE_PREFIX") or "").strip()
        integrated = billing_subsystem_enabled(current_app)
        unified = unified_clients_enabled()
        if table not in _INVOICE_TABLES:
            raise ValueError(f"Invalid invoice table: {table}")
        if prefix and not _IDENTIFIER_RE.match(prefix):
            raise ValueError("Invalid INVOICEAPP_TABLE_PREFIX")
        if integrated and prefix and table in _INVOICE_TABLES:
            if unified and table == "clients":
                return table
            return f"{prefix}{table}"
        return table

    @classmethod
    def _table_exists(cls, table_name: str) -> bool:
        try:
            bind = db.session.get_bind()
            dialect = (
                (getattr(getattr(bind, "dialect", None), "name", "") or "").lower() if bind else ""
            )
            if dialect.startswith("sqlite"):
                row = db.session.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
                    {"name": str(table_name)},
                ).fetchone()
                return bool(row)
            if dialect.startswith("postgres"):
                row = db.session.execute(
                    text("SELECT to_regclass(:name)"),
                    {"name": str(table_name)},
                ).fetchone()
                return bool(row and row[0])
        except Exception:
            return False
        return False

    @classmethod
    def _invoice_client_exists(cls, invoice_client_id: int) -> bool:
        try:
            tbl = cls._invoice_actual_table("clients")
            row = db.session.execute(
                text(f"SELECT 1 FROM {tbl} WHERE id=:id"),
                {"id": int(invoice_client_id)},
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    @classmethod
    def _resolve_invoice_client_id(cls, client: Client) -> Optional[int]:
        raw = getattr(client, "external_invoice_client_id", None)
        inv_id: Optional[int] = None
        try:
            if raw is not None:
                inv_id = int(raw)
        except Exception:
            inv_id = None
        if inv_id is not None and cls._invoice_client_exists(inv_id):
            return inv_id
        if inv_id is not None:
            try:
                client.external_invoice_client_id = None
            except Exception as exc:
                # Best-effort: attribute may not exist in some deployments.
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._resolve_invoice_client_id.clear_external_invoice_client_id",
                    log_key="client_merge_service._resolve_invoice_client_id.clear_external_invoice_client_id",
                    log_window_seconds=300,
                )

        ipm_client_id = getattr(client, "id", None)
        ipm_party_id = getattr(client, "party_id", None) or getattr(client, "ipm_party_id", None)
        if ipm_client_id is None and not ipm_party_id:
            return None

        try:
            tbl = cls._invoice_actual_table("clients")
            row = db.session.execute(
                text(
                    f"""
                    SELECT id
                      FROM {tbl}
                     WHERE ipm_client_id = :pcid
                        OR ipm_party_id = :ppid
                     ORDER BY id DESC
                     LIMIT 1
                    """
                ),
                {"pcid": ipm_client_id, "ppid": ipm_party_id},
            ).fetchone()
        except Exception:
            row = None

        if row:
            try:
                inv_id = int(row[0])
            except Exception:
                inv_id = None
        if inv_id is not None and not getattr(client, "external_invoice_client_id", None):
            client.external_invoice_client_id = inv_id
        return inv_id

    @classmethod
    def _update_invoice_client_link(
        cls,
        invoice_client_id: int,
        ipm_client: Optional[Client] = None,
        ipm_client_id: Optional[int] = None,
        ipm_party_id: Optional[str] = None,
    ) -> None:
        if ipm_client is not None:
            ipm_client_id = int(getattr(ipm_client, "id", 0) or 0) or None
            ipm_party_id = getattr(ipm_client, "party_id", None) or getattr(
                ipm_client, "ipm_party_id", None
            )
        if ipm_client_id is None and ipm_party_id is None:
            return
        try:
            tbl = cls._invoice_actual_table("clients")
            db.session.execute(
                text(
                    f"""
                    UPDATE {tbl}
                       SET ipm_client_id = :pcid,
                           ipm_party_id = :ppid
                     WHERE id = :id
                    """
                ),
                {
                    "pcid": int(ipm_client_id) if ipm_client_id is not None else None,
                    "ppid": ipm_party_id,
                    "id": int(invoice_client_id),
                },
            )
        except Exception as exc:
            # Best-effort: keep App->invoice client linkage advisory.
            report_swallowed_exception(
                exc,
                context="client_merge_service._update_invoice_client_link",
                log_key="client_merge_service._update_invoice_client_link",
                log_window_seconds=300,
            )

    @classmethod
    def _create_premerge_backup(
        cls, *, tags: List[str], note: str, include_attachments: bool
    ) -> Dict[str, Any]:
        from app.blueprints.billing_invoices.routes.admin import (
            _cleanup_old_backups,
            _create_backup_file,
            _write_backup_meta,
        )

        db_url = (current_app.config.get("SQLALCHEMY_DATABASE_URI") or "").strip()
        backup_dir = current_app.config.get("BACKUP_DIR") or "data/backups"
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")

        if db_url.startswith("postgresql"):
            path = _create_backup_file()
            try:
                # Mark as pre-op so retention logic can protect operation-linked backups.
                _write_backup_meta(path, source="preop", note=note, tags=tags, created_by=None)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._create_premerge_backup.write_backup_meta",
                    log_key="client_merge_service._create_premerge_backup.write_backup_meta",
                    log_window_seconds=300,
                )
            try:
                _cleanup_old_backups()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._create_premerge_backup.cleanup_old_backups",
                    log_key="client_merge_service._create_premerge_backup.cleanup_old_backups",
                    log_window_seconds=300,
                )
            ext = os.path.splitext(path)[1].lstrip(".").lower()
            kind = f"postgresql.{ext}" if ext else "postgresql"
            return {"path": path, "kind": kind, "tags": tags, "note": note}

        if not db_url:
            db_path = current_app.config.get("DB_PATH") or current_app.config.get(
                "INVOICE_MODULE_DB_PATH"
            )
            if db_path:
                db_url = f"sqlite:///{db_path}"

        m = re.match(r"^sqlite:(/{2,3})(.+)$", db_url)
        if not m:
            raise RuntimeError("Backup required but DB uri is not postgresql/sqlite.")

        raw_path = (m.group(2) or "").strip()
        if raw_path == ":memory:":
            # In-memory SQLite has no backing file, so a filesystem backup is impossible.
            # This is common in tests and local ephemeral runs.
            return {"path": None, "kind": "sqlite.memory", "tags": tags, "note": note}
        db_path = raw_path
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)

        if not os.path.exists(db_path):
            raise RuntimeError(f"SQLite DB file not found for backup: {db_path}")

        backup_path = os.path.join(backup_dir, f"backup-{ts}.db")
        shutil.copy2(db_path, backup_path)

        meta = {
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source": "forced",
            "note": note,
            "tags": tags,
            "created_by": None,
            "size_bytes": (os.path.getsize(backup_path) if os.path.exists(backup_path) else None),
        }
        with open(os.path.splitext(backup_path)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        if include_attachments:
            try:
                cls._zip_dir(
                    current_app.config.get("ATTACHMENTS_DIR"),
                    backup_dir,
                    f"attachments-{ts}.zip",
                )
            except Exception as exc:
                # Best-effort: attachment backup should not block client merge.
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._create_premerge_backup.zip_attachments",
                    log_key="client_merge_service._create_premerge_backup.zip_attachments",
                    log_window_seconds=300,
                )
            try:
                cls._zip_dir(
                    current_app.config.get("CLIENT_ATTACHMENTS_DIR", "uploads/clients"),
                    backup_dir,
                    f"client_attachments-{ts}.zip",
                )
            except Exception as exc:
                # Best-effort: attachment backup should not block client merge.
                report_swallowed_exception(
                    exc,
                    context="client_merge_service._create_premerge_backup.zip_client_attachments",
                    log_key="client_merge_service._create_premerge_backup.zip_client_attachments",
                    log_window_seconds=300,
                )

        try:
            _cleanup_old_backups()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_merge_service._create_premerge_backup.cleanup_old_backups",
                log_key="client_merge_service._create_premerge_backup.cleanup_old_backups",
                log_window_seconds=300,
            )
        return {"path": backup_path, "kind": "sqlite.db", "tags": tags, "note": note}

    @classmethod
    def _acquire_merge_lock(cls) -> None:
        row = SystemConfig.query.filter_by(key=_LOCK_KEY).first()
        if not row:
            db.session.add(SystemConfig(key=_LOCK_KEY, value=""))
            db.session.flush()

        try:
            db.session.execute(
                text("SELECT key FROM system_config WHERE key = :k FOR UPDATE"),
                {"k": _LOCK_KEY},
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="client_merge_service._acquire_merge_lock.select_for_update",
                log_key="client_merge_service._acquire_merge_lock.select_for_update",
                log_window_seconds=300,
            )

    @staticmethod
    def _normalize_ids(ids: List[int], exclude: set[int]) -> List[int]:
        out: List[int] = []
        seen = set()
        for v in ids or []:
            try:
                i = int(v)
            except Exception:
                continue
            if i in exclude or i in seen:
                continue
            seen.add(i)
            out.append(i)
        return out

    @staticmethod
    def _snapshot_client_row(c: Client) -> Dict[str, Any]:
        return {
            "id": c.id,
            "name": getattr(c, "name", None),
            "email": getattr(c, "email", None),
            "phone": getattr(c, "phone", None),
            "registration_number": getattr(c, "registration_number", None),
            "party_id": getattr(c, "party_id", None),
            "ipm_party_id": getattr(c, "ipm_party_id", None),
            "ipm_client_id": getattr(c, "ipm_client_id", None),
            "external_invoice_client_id": getattr(c, "external_invoice_client_id", None),
            "notes": getattr(c, "notes", None),
            "is_deleted": getattr(c, "is_deleted", None),
        }

    @staticmethod
    def _append_notes_to_target(target: Client, sources: List[Client]) -> Optional[str]:
        tnotes = (getattr(target, "notes", None) or "").strip()
        parts = []
        for s in sources:
            n = (getattr(s, "notes", None) or "").strip()
            if n:
                parts.append(f"[MERGED from client#{s.id} {getattr(s, 'name', '')}] {n}")
        if not parts:
            return None
        appended = "\n".join(parts)
        target.notes = (tnotes + "\n\n" + appended).strip() if tnotes else appended
        return appended

    @staticmethod
    def _unique_filename(directory: str, filename: str) -> str:
        base, ext = os.path.splitext(os.path.basename(filename))
        if not base:
            base = "file"
        candidate = f"{base}{ext}"
        i = 1
        while os.path.exists(os.path.join(directory, candidate)):
            candidate = f"{base} ({i}){ext}"
            i += 1
            if i > 500:
                candidate = f"{base}__{datetime.utcnow().strftime('%H%M%S')}{ext}"
                break
        return candidate

    @staticmethod
    def _zip_dir(root: Optional[str], backup_dir: str, name: str) -> Optional[str]:
        import zipfile

        if not root or not os.path.isdir(root):
            return None
        os.makedirs(backup_dir, exist_ok=True)
        zp = os.path.join(backup_dir, name)
        with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirs, files in os.walk(root):
                for f in files:
                    fp = os.path.join(dirpath, f)
                    rel = os.path.relpath(fp, root)
                    zf.write(fp, arcname=rel)
        return zp
