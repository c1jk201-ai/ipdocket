from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from flask import current_app, flash, request, url_for
from flask_login import current_user
from sqlalchemy import func, inspect, or_

from app.blueprints.case.helpers_fields import (
    DOMESTIC_DESIGN_ALLOWED_KEYS,
    DOMESTIC_DESIGN_FIELDS,
    DOMESTIC_PATENT_ALLOWED_KEYS,
    DOMESTIC_PATENT_FIELDS,
    DOMESTIC_TRADEMARK_ALLOWED_KEYS,
    DOMESTIC_TRADEMARK_FIELDS,
    INCOMING_DESIGN_ALLOWED_KEYS,
    INCOMING_DESIGN_FIELDS,
    INCOMING_PATENT_ALLOWED_KEYS,
    INCOMING_PATENT_FIELDS,
    INCOMING_TRADEMARK_ALLOWED_KEYS,
    INCOMING_TRADEMARK_FIELDS,
    LITIGATION_ALLOWED_KEYS,
    LITIGATION_FIELDS,
    MISC_ALLOWED_KEYS,
    MISC_FIELDS,
    OUTGOING_DESIGN_ALLOWED_KEYS,
    OUTGOING_DESIGN_FIELDS,
    OUTGOING_PATENT_ALLOWED_KEYS,
    OUTGOING_PATENT_FIELDS,
    OUTGOING_TRADEMARK_ALLOWED_KEYS,
    OUTGOING_TRADEMARK_FIELDS,
    PCT_ALLOWED_KEYS,
    PCT_FIELDS,
)
from app.extensions import db
from app.models.case import Case
from app.models.client import Client
from app.models.party import Party, PartyStaff
from app.models.ip_records import (
    DocketItem,
    EventKeyMap,
    Matter,
    MatterCustomField,
    MatterEvent,
    MatterIdentifier,
    MatterPartyRole,
    RawImportField,
    VMatterOverview,
)
from app.models.user import User
from app.services.case.case_kind import (
    PATENT_LIKE_TYPES,
    _apply_case_kind_to_matter,
    _has_litigation_keyword,
    _infer_case_kind,
    _infer_case_kind_from_app_no,
    _infer_case_kind_from_right_name,
    _lookup_app_no,
    _lookup_raw_right_label,
    _normalize_case_division,
    _normalize_case_type,
    resolve_profile_case_kind,
)
from app.services.case.case_parameter_service import CaseParameterService
from app.services.case.form_support import (
    allowed_keys_from_fields,
    should_skip_custom_field_filter_key,
    validate_application_number,
)
from app.services.case.helpers_files import (
    _attach_image_file_asset,
    _is_allowed_image_upload,
    _load_linked_file_asset,
    sha256_filestorage,
)
from app.services.case.helpers_staff import (
    _BASIC_CANONICAL_STAFF_KEYS,
    _format_staff_value,
    _normalize_staff_token,
    _overlay_basic_staff_fields,
    _resolve_user_from_id,
    _resolve_user_from_staff_token,
    _resolve_users_from_staff_fields,
    _split_staff_tokens,
    _sync_matter_staff_assignments,
    _update_basic_matter_info,
)
from app.services.core.staff_options import build_staff_assignment_lists
from app.services.matter import matter_auto_status as _auto_status
from app.services.matter.auto_status_apply import (
    _auto_complete_workflows_from_events as _service_auto_complete_workflows_from_events,
    apply_auto_status_from_db,
)
from app.utils.docket_dates import parse_date as _parse_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text

# Regex pattern for extracting YYYY-MM-DD from various date formats
_DATE_YYYY_MM_DD_RE = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})")


__all__ = [
    "DOMESTIC_DESIGN_ALLOWED_KEYS",
    "DOMESTIC_DESIGN_FIELDS",
    "DOMESTIC_PATENT_ALLOWED_KEYS",
    "DOMESTIC_PATENT_FIELDS",
    "DOMESTIC_TRADEMARK_ALLOWED_KEYS",
    "DOMESTIC_TRADEMARK_FIELDS",
    "INCOMING_DESIGN_ALLOWED_KEYS",
    "INCOMING_DESIGN_FIELDS",
    "INCOMING_PATENT_ALLOWED_KEYS",
    "INCOMING_PATENT_FIELDS",
    "INCOMING_TRADEMARK_ALLOWED_KEYS",
    "INCOMING_TRADEMARK_FIELDS",
    "LITIGATION_ALLOWED_KEYS",
    "LITIGATION_FIELDS",
    "MISC_ALLOWED_KEYS",
    "MISC_FIELDS",
    "OUTGOING_DESIGN_ALLOWED_KEYS",
    "OUTGOING_DESIGN_FIELDS",
    "OUTGOING_TRADEMARK_ALLOWED_KEYS",
    "OUTGOING_TRADEMARK_FIELDS",
    "OUTGOING_PATENT_ALLOWED_KEYS",
    "OUTGOING_PATENT_FIELDS",
    "PCT_ALLOWED_KEYS",
    "PCT_FIELDS",
    "_MatterWrapper",
    "_apply_auto_status_from_db",
    "_apply_case_kind_to_matter",
    "_build_case_list_extras",
    "_build_staff_assignment_context",
    "_build_staff_picker_context",
    "_date_only_str",
    "_ensure_attorney_column",
    "_ensure_case_type_column",
    "_expand_ref_range",
    "_fill_case_fields_from_ipm",
    "_fill_dom_design_from_ipm",
    "_fill_dom_patent_from_ipm",
    "_fill_dom_trademark_from_ipm",
    "_fill_incoming_design_from_ipm",
    "_fill_incoming_patent_from_ipm",
    "_fill_incoming_trademark_from_ipm",
    "_fill_outgoing_design_from_ipm",
    "_fill_outgoing_patent_from_ipm",
    "_fill_outgoing_trademark_from_ipm",
    "_fill_pct_from_ipm",
    "_format_staff",
    "_get_case_date",
    "_get_special_template",
    "_has_litigation_keyword",
    "_infer_case_kind",
    "_infer_case_kind_from_app_no",
    "_infer_case_kind_from_right_name",
    "_is_generic_proposal_title",
    "_is_yes",
    "_load_ipm_case_sheet_mapping",
    "_lookup_app_no",
    "_lookup_raw_right_label",
    "_normalize_case_division",
    "_normalize_case_type",
    "_normalize_ref",
    "_normalize_our_ref_input",
    "_normalize_date_input",
    "_validate_custom_field_updates",
    "_log_custom_field_filtering",
    "_parse_int",
    "_parse_refs_from_text",
    "_priority_numbers_present",
    "_save_case_data",
    "_save_foreign_info",
    "_strip_ref_noise",
    "PATENT_LIKE_TYPES",
    "_sync_matter_events_from_dom_patent",
    "_sync_matter_events_from_dom_trademark",
    "_sync_matter_events_from_inc_design",
    "_sync_matter_events_from_inc_patent",
    "_sync_matter_events_from_inc_trademark",
    "_sync_matter_events_from_litigation",
    "_sync_matter_events_from_out_design",
    "_sync_matter_events_from_out_patent",
    "_sync_matter_events_from_out_trademark",
    "_sync_matter_events_from_pct",
    "_sync_matter_identifiers_from_dom_patent",
    "_sync_matter_identifiers_from_dom_trademark",
    "_sync_matter_identifiers_from_inc_design",
    "_sync_matter_identifiers_from_inc_patent",
    "_sync_matter_identifiers_from_inc_trademark",
    "_sync_matter_identifiers_from_out_design",
    "_sync_matter_identifiers_from_out_patent",
    "_sync_matter_identifiers_from_out_trademark",
    "_sync_matter_identifiers_from_pct",
    "_sync_matter_party_roles",
    "_to_int",
    "_to_int_list",
    "allowed_keys_from_fields",
    "_load_linked_file_asset",
    "_attach_image_file_asset",
    "_is_allowed_image_upload",
    "validate_application_number",
    "sha256_filestorage",
    "_add_years",
    "_clear_duplicate_appeal_no",
    "_BASIC_CANONICAL_STAFF_KEYS",
    "_overlay_basic_staff_fields",
    "_split_staff_tokens",
    "_normalize_staff_token",
    "_format_staff_value",
    "_resolve_user_from_id",
    "_resolve_user_from_staff_token",
    "_resolve_users_from_staff_fields",
    "_sync_matter_staff_assignments",
    "_update_basic_matter_info",
    "_apply_same_client_logic_helper",
    "_is_allowed_image_upload",
    "_ASSISTANT_PREFILL_FIELDS",
    "_extract_prefill_params",
    "_hx_hard_redirect_response",
    "_normalize_our_ref_input",
    "_normalize_date_input",
    "_CREATE_ALLOWED_DIVISIONS",
    "_CREATE_ALLOWED_TYPES",
    "_is_valid_create_kind",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_DOM_PATENT_OUR_REF_RE = re.compile(r"^(?P<yy>\d{2})PD(?P<num>\d{4})US$")


@lru_cache(maxsize=1)
def _load_ipm_case_sheet_mapping() -> dict[str, list[dict]]:
    """
    Load ipm_mapping.csv (sheet_name='Matter') into {source_column: [rows...]}
    Cached per process.
    """
    base_dir = Path(current_app.root_path).parent
    candidates = [
        base_dir / "data" / "ipm_mapping.csv",
        base_dir / "ipm_mapping.csv",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {}

    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("sheet_name") or "").strip() != "Matter":
                continue
            rows.append(
                {
                    "source_column": (r.get("source_column") or "").strip(),
                    "target_table": (r.get("target_table") or "").strip(),
                    "target_column": (r.get("target_column") or "").strip(),
                    "mapping_type": (r.get("mapping_type") or "").strip(),
                    "transform": (r.get("transform") or "").strip(),
                    "multi_value": (r.get("multi_value") or "").strip(),
                    "params_json": (r.get("params_json") or "").strip(),
                }
            )

    by_source: dict[str, list[dict]] = {}
    for r in rows:
        if not r["source_column"]:
            continue
        by_source.setdefault(r["source_column"], []).append(r)
    return by_source


def _format_staff(name_display: str | None, staff_code: str | None) -> str:
    # Return only display name as requested
    n = (name_display or "").strip()
    c = (staff_code or "").strip()
    return n or c


def _build_case_list_extras(cases: list[VMatterOverview]) -> dict[str, dict[str, str]]:
    """
    List view (/case/*) uses VMatterOverview which doesn't include many sheet fields.
    Build a small set of commonly-needed display values in bulk (best-effort).
    """
    extras: dict[str, dict[str, str]] = {}
    matter_ids = [str(getattr(c, "matter_id", "") or "") for c in (cases or [])]
    matter_ids = [m for m in matter_ids if m]
    if not matter_ids:
        return extras

    def _sql_in(field: str, prefix: str, values: list[str]) -> tuple[str, dict]:
        params = {}
        parts = []
        for i, v in enumerate(values):
            k = f"{prefix}{i}"
            params[k] = v
            parts.append(f":{k}")
        return f"{field} IN ({', '.join(parts)})", params

    def _fallback_blue_from_overview(ov: VMatterOverview | None) -> str:
        if not ov:
            return ""
        matter_type = (getattr(ov, "matter_type", "") or "").strip().upper()
        our_ref = (getattr(ov, "our_ref", "") or "").strip().upper()
        if not matter_type and len(our_ref) >= 4 and our_ref[:2].isdigit():
            code = our_ref[2:4]
            if code.startswith("P"):
                matter_type = "PATENT"
            elif code.startswith("U"):
                matter_type = "UTILITY"
            elif code.startswith("D"):
                matter_type = "DESIGN"
            elif code.startswith("T"):
                matter_type = "TRADEMARK"

        if matter_type in ("PATENT", "UTILITY", "DESIGN", "TRADEMARK"):
            return "Filing  In Progress"
        if matter_type in ("TRIAL", "LITIGATION", "LAWSUIT"):
            return "Matter In Progress"
        return ""

    _EVENT_SYNC_NAMESPACE_ORDER = (
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
    )
    custom_field_data_by_mid_ns: dict[str, dict[str, dict]] = {}
    official_applicant_mids: set[str] = set()

    def _expected_namespace_for_overview(ov: VMatterOverview | None) -> str:
        if not ov:
            return ""
        right_group, matter_type = resolve_profile_case_kind(
            getattr(ov, "right_group", None),
            getattr(ov, "matter_type", None),
        )
        try:
            profile = CaseParameterService.get_case_profile(right_group, matter_type)
            return (profile.namespace or "").strip()
        except Exception:
            return ""

    def _pick_custom_status_payload(mid: str, ov: VMatterOverview | None) -> dict | None:
        by_ns = custom_field_data_by_mid_ns.get(mid) or {}
        if not by_ns:
            return None
        if len(by_ns) == 1:
            return next(iter(by_ns.values()))
        expected_ns = _expected_namespace_for_overview(ov)
        if expected_ns and expected_ns in by_ns:
            return by_ns.get(expected_ns)
        for ns in _EVENT_SYNC_NAMESPACE_ORDER:
            payload = by_ns.get(ns)
            if payload:
                return payload
        return next(iter(by_ns.values()))

    def _supplement_event_summary_from_payload(summary, payload: dict) -> None:
        _auto_status.supplement_event_summary_from_payload(summary, payload)

    def _display_red_candidates_from_text(value: str | None) -> list[tuple[date | None, str]]:
        candidates: list[tuple[date | None, str]] = []
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            due_date = None
            match = _DATE_YYYY_MM_DD_RE.search(line)
            if match:
                due_date = _parse_date("-".join(match.groups()))
            candidates.append((due_date, line))
        return candidates

    def _set_earliest_display_red(
        mid: str,
        candidates: list[tuple[date | None, str]],
    ) -> None:
        if not extras.get(mid):
            return
        all_candidates = _display_red_candidates_from_text(
            extras[mid].get("display_red")
        ) + candidates
        if not all_candidates:
            return
        dated = [item for item in all_candidates if item[0] is not None]
        if dated:
            extras[mid]["display_red"] = min(dated, key=lambda item: item[0])[1]
        else:
            extras[mid]["display_red"] = all_candidates[0][1]

    def _coerce_display_text(value) -> str:  # noqa: ANN001
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for k in ("value", "text", "label"):
                text_value = _coerce_display_text(value.get(k))
                if text_value:
                    return text_value
            return ""
        if isinstance(value, (list, tuple, set)):
            parts = []
            for item in value:
                text_value = _coerce_display_text(item)
                if text_value:
                    parts.append(text_value)
            return ", ".join(parts)
        return str(value).strip()

    def _extract_trademark_classes(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in (
            "application_classes",
            "nice_classes",
            "goods_class",
            "registration_classes",
        ):
            value = _coerce_display_text(payload.get(key))
            if value:
                return value
        return ""

    for mid in matter_ids:
        extras[mid] = {
            "proposal_title": "",
            "inventor_name": "",
            "trademark_classes": "",
            "applicant_name": "",
            "applicant_client_id": "",
            "application_no": "",
            "application_date": "",
            "client_id": "",
            "client_name": "",
            "status_red_related_date": "",
            "derived_status_red": "",
            "derived_status_blue": "",
            "display_red": "",
            "display_blue": "",
        }

    # 1.5 + 1.6) Custom fields (basic + namespace fallback).
    # Run as a single query to reduce list-view DB round trips.
    try:
        rows = (
            MatterCustomField.query.with_entities(
                MatterCustomField.matter_id,
                MatterCustomField.namespace,
                MatterCustomField.data,
            )
            .filter(MatterCustomField.matter_id.in_(matter_ids))
            .all()
        )

        for mid, namespace, data in rows:
            m = str(mid or "").strip()
            ns = str(namespace or "").strip()
            if not m or m not in extras or not ns:
                continue
            if isinstance(data, dict) and data:
                custom_field_data_by_mid_ns.setdefault(m, {})[ns] = data

        def _apply_custom_field_row(
            mid: str,
            data: dict,
            *,
            include_applicant_fallback: bool,
        ) -> None:
            if not extras.get(mid):
                return
            if not isinstance(data, dict):
                return

            if not extras[mid].get("client_id"):
                client_id = str(data.get("client_id") or "").strip()
                if client_id:
                    extras[mid]["client_id"] = client_id

            if not extras[mid].get("client_name"):
                client_name = str(data.get("client_name") or "").strip()
                if client_name:
                    extras[mid]["client_name"] = client_name

            if not extras[mid].get("trademark_classes"):
                class_value = _extract_trademark_classes(data)
                if class_value:
                    extras[mid]["trademark_classes"] = class_value

            if not include_applicant_fallback or extras[mid].get("applicant_name"):
                return

            applicant_name = ""
            for key in (
                "application_applicant_name",
                "applicant_name",
                "applicant_registrant",
            ):
                value = str(data.get(key) or "").strip()
                if value:
                    applicant_name = value
                    if key == "application_applicant_name":
                        official_applicant_mids.add(mid)
                    break
            if applicant_name:
                extras[mid]["applicant_name"] = applicant_name

        # Keep prior precedence: basic namespace first, then fallback namespaces.
        for mid, namespace, data in rows:
            m = str(mid or "").strip()
            ns = str(namespace or "").strip()
            if not m or m not in extras or ns != "basic":
                continue
            _apply_custom_field_row(m, data or {}, include_applicant_fallback=False)

        for mid, namespace, data in rows:
            m = str(mid or "").strip()
            ns = str(namespace or "").strip()
            if not m or m not in extras or ns == "basic":
                continue
            _apply_custom_field_row(m, data or {}, include_applicant_fallback=True)

        # Prefer values from the expected namespace (when available) to avoid
        # cross-namespace bleed for migrated/legacy mixed rows.
        case_by_mid = {str(getattr(c, "matter_id", "") or ""): c for c in (cases or [])}
        for mid in matter_ids:
            payload = _pick_custom_status_payload(mid, case_by_mid.get(mid))
            class_value = _extract_trademark_classes(payload or {})
            if class_value:
                extras[mid]["trademark_classes"] = class_value
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras custom fields: {e}")

    # 1.7) Fallback client mapping via party-role linkage (migrated legacy party_id).
    # Prefer this over name heuristics: party_id is designed to be a stable identifier.
    try:
        from app.models.client import Client

        missing_client_ids = [
            mid for mid in matter_ids if not (extras.get(mid) or {}).get("client_id")
        ]
        if missing_client_ids:
            rows = (
                db.session.query(MatterPartyRole.matter_id, MatterPartyRole.party_id)
                .filter(MatterPartyRole.matter_id.in_(missing_client_ids))
                .filter(func.lower(func.coalesce(MatterPartyRole.role_code, "")) == "client")
                .filter(func.coalesce(MatterPartyRole.party_id, "") != "")
                .order_by(
                    MatterPartyRole.matter_id.asc(),
                    func.coalesce(MatterPartyRole.seq, 0).asc(),
                )
                .all()
            )

            by_mid_party_ids: dict[str, list[str]] = {}
            party_ids: list[str] = []
            for mid, pid in rows:
                m = str(mid or "").strip()
                p = str(pid or "").strip()
                if not (m and p):
                    continue
                by_mid_party_ids.setdefault(m, [])
                if p not in by_mid_party_ids[m]:
                    by_mid_party_ids[m].append(p)
                if p not in party_ids:
                    party_ids.append(p)

            if party_ids:
                clients = (
                    Client.query.filter(
                        (Client.is_deleted.is_(False)) | (Client.is_deleted.is_(None))
                    )
                    .filter(
                        or_(Client.party_id.in_(party_ids), Client.ipm_party_id.in_(party_ids))
                    )
                    .all()
                )
                by_party: dict[str, Client] = {}
                for c in clients:
                    for pid in (getattr(c, "party_id", None), getattr(c, "ipm_party_id", None)):
                        pid = str(pid or "").strip()
                        if pid:
                            by_party[pid] = c

                for mid in missing_client_ids:
                    if (extras.get(mid) or {}).get("client_id"):
                        continue
                    pids = by_mid_party_ids.get(mid) or []
                    resolved: list[Client] = []
                    for pid in pids:
                        c = by_party.get(pid)
                        if c and c not in resolved:
                            resolved.append(c)
                    if len(resolved) == 1:
                        client = resolved[0]
                        cid = str(getattr(client, "id", "") or "").strip()
                        if cid:
                            extras[mid]["client_id"] = cid
                        if not extras[mid].get("client_name"):
                            extras[mid]["client_name"] = str(
                                getattr(client, "name", "") or ""
                            ).strip()
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras party-role client mapping: {e}")

    # 1) matter.raw_id + status_red_related_date (single batched lookup).
    matter_id_to_raw_id: dict[str, str] = {}
    matter_id_to_status_red_related_date: dict[str, str] = {}
    try:
        rows = (
            db.session.query(Matter.matter_id, Matter.raw_id, Matter.status_red_related_date)
            .filter(Matter.matter_id.in_(matter_ids))
            .all()
        )
        for mid, rid, dt in rows:
            m = (mid or "").strip()
            if not m:
                continue
            if (rid or "").strip():
                matter_id_to_raw_id[m] = str(rid)
            v = _date_only_str(dt)
            if v:
                matter_id_to_status_red_related_date[m] = v
                if extras.get(m) is not None and not extras[m].get("status_red_related_date"):
                    extras[m]["status_red_related_date"] = v
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 1: {e}")
        matter_id_to_raw_id = {}
        matter_id_to_status_red_related_date = {}

    # 2.5) Use stored status values directly for list display (optimized - no N+1 queries)
    # NOTE: derive_auto_status is expensive (many DB queries per case). For the list view,
    # we use the stored values directly. Full derivation happens on case detail page.
    try:
        by_mid = {str(getattr(c, "matter_id", "") or ""): c for c in (cases or [])}
        for mid in matter_ids:
            c = by_mid.get(mid)
            if not c:
                continue
            # Use stored values directly instead of deriving
            raw_red = (getattr(c, "status_red", "") or "").strip()
            red = _auto_status.normalize_red_status(raw_red)
            blue = (getattr(c, "status_blue", "") or "").strip()
            red_date = (extras.get(mid, {}).get("status_red_related_date") or "").strip()

            # Simple formatting for display (from matter_auto_status._format_red_display)
            # Guardrail: hide non-action document titles accidentally stored in status_red (e.g. "PatentFiling").
            if (
                _auto_status.is_internal_mgmt_non_status_red_ref(raw_red)
                or _auto_status._looks_like_non_red_document_title(red)
                or _auto_status.is_non_action_status_red_label(red)
            ):
                red = ""
                red_date = ""
            display_red = red
            if red and red_date:
                display_red = f"{red}[{red_date}]"

            # Keep list "Status" aligned with detail "AutoStatus" blue line.
            # (inhouse_status is a separate manual status and should not override auto blue here)
            display_blue = blue

            extras[mid]["display_red"] = display_red
            extras[mid]["display_blue"] = display_blue
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 2.5: {e}")

    # 2.6) Refine blue status in list (best-effort, batch event fetch)
    # Keep list Status closer to detail AutoStatus without calling derive_auto_status per row.
    try:
        # Only refine rows that are likely to drift from event-derived detail blue:
        # - missing blue
        # - red is present (blue may need red/event-based upgrade)
        # - generic blue labels that can be upgraded (e.g., ForeignFiling In Progress(ExaminationBilling))
        generic_blue_states = {
            "Filing Examination In Progress",
            "Filing  In Progress",
            "ForeignFiling  In Progress",
            "Examination  Billing In Progress",
        }
        blue_refine_ids = []
        for mid in matter_ids:
            _x = extras.get(mid, {}) or {}
            cur_blue = (_x.get("display_blue") or "").strip()
            cur_red = (_x.get("display_red") or "").strip()
            if (not cur_blue) or cur_red or (cur_blue in generic_blue_states):
                blue_refine_ids.append(mid)

        if blue_refine_ids:
            rows = (
                db.session.query(
                    MatterEvent.matter_id,
                    MatterEvent.event_key,
                    EventKeyMap.std_event_key,
                    MatterEvent.event_at,
                )
                .outerjoin(EventKeyMap, EventKeyMap.raw_event_key == MatterEvent.event_key)
                .filter(MatterEvent.matter_id.in_(blue_refine_ids))
                .filter(MatterEvent.event_at.isnot(None))
                .filter(func.trim(MatterEvent.event_at) != "")
                .all()
            )

            by_mid_events: dict[str, list[tuple[str, str | None, str]]] = {
                mid: [] for mid in blue_refine_ids
            }
            for mid, raw_key, std_key, event_at in rows:
                m = (mid or "").strip()
                if not m:
                    continue
                by_mid_events.setdefault(m, []).append((raw_key, std_key, event_at))

            case_by_mid = {str(getattr(c, "matter_id", "") or ""): c for c in (cases or [])}
            today = date.today()

            for mid in blue_refine_ids:
                ov = case_by_mid.get(mid)
                if not ov:
                    continue

                current_display_blue = (extras.get(mid, {}).get("display_blue") or "").strip()
                event_rows = by_mid_events.get(mid) or []
                event_presence: set[str]
                event_due_by_std_key: dict[str, date]
                expired_deadlines: set[str] = set()

                summary = _auto_status._summarize_event_rows(event_rows)
                payload = _pick_custom_status_payload(mid, ov)
                if payload:
                    _supplement_event_summary_from_payload(summary, payload)

                event_presence = set(summary.presence)
                event_due_by_std_key = _auto_status._build_due_by_std_key(summary)
                if event_due_by_std_key:
                    expired_deadlines = _auto_status._expired_deadlines_by_policy(
                        event_due_by_std_key, today=today
                    )

                blue = ""
                if event_presence:
                    blue = _auto_status.normalize_blue_status(
                        _auto_status._derive_blue_from_events(
                            mid,
                            event_presence=event_presence,
                            event_due_by_std_key=event_due_by_std_key,
                            event_summary=summary,
                            expired_deadlines=expired_deadlines,
                        )
                    )

                if not blue:
                    raw_red = (getattr(ov, "status_red", "") or "").strip()
                    red = ""
                    if not _auto_status.is_internal_mgmt_non_status_red_ref(raw_red):
                        red = _auto_status.normalize_red_status(raw_red)
                    if _auto_status.is_non_action_status_red_label(red):
                        red = ""
                    blue = _auto_status.normalize_blue_status(
                        _auto_status._suggest_blue_from_red(red)
                    )

                if not blue:
                    current_display_blue_norm = _auto_status.normalize_blue_status(
                        current_display_blue
                    )
                    if (
                        current_display_blue_norm
                        and not _auto_status.is_evidence_required_blue_status(
                            current_display_blue_norm
                        )
                    ):
                        blue = current_display_blue_norm

                if not blue:
                    blue = _fallback_blue_from_overview(ov)

                display_blue = blue

                if event_presence:
                    fallback_due_by_std_key: dict[str, date] = {}
                    try:
                        needs_exam_deadline_fallback = (
                            "APPLICATION_DATE" in event_presence
                            and "EXAM_REQUEST_DATE" not in event_presence
                            and "EXAM_REQUESTED" not in event_presence
                            and "EXAM_REQUEST_DEADLINE" not in event_presence
                        )
                        if needs_exam_deadline_fallback:
                            matter_type = (getattr(ov, "matter_type", "") or "").strip().upper()
                            our_ref = (getattr(ov, "our_ref", "") or "").strip().upper()
                            if not matter_type and len(our_ref) >= 4 and our_ref[:2].isdigit():
                                code = our_ref[2:4]
                                if code.startswith("P"):
                                    matter_type = "PATENT"
                                elif code.startswith("U"):
                                    matter_type = "UTILITY"
                                elif code.startswith("D"):
                                    matter_type = "DESIGN"
                                elif code.startswith("T"):
                                    matter_type = "TRADEMARK"

                            if matter_type in ("PATENT", "UTILITY"):
                                filing_dt = event_due_by_std_key.get("APPLICATION_DATE")
                                if filing_dt:
                                    fallback_due_by_std_key["EXAM_REQUEST_DEADLINE"] = (
                                        _auto_status._add_years(filing_dt, 3)
                                    )
                    except Exception:
                        fallback_due_by_std_key = {}

                    pending_post_filing = _auto_status._collect_post_filing_pending_deadlines(
                        event_presence,
                        event_due_by_std_key,
                        event_summary=summary,
                        fallback_due_by_std_key=fallback_due_by_std_key,
                        expired_deadlines=expired_deadlines,
                    )
                    if pending_post_filing:
                        # Match detail auto-status: append pending post-filing red lines
                        # (e.g., ForeignFilingDeadline + Examination requestDeadline).
                        raw_red_label = (getattr(ov, "status_red", "") or "").strip()
                        red_label = ""
                        if not _auto_status.is_internal_mgmt_non_status_red_ref(raw_red_label):
                            red_label = _auto_status.normalize_red_status(raw_red_label)
                        if _auto_status.is_non_action_status_red_label(red_label):
                            red_label = ""
                        red_date = (
                            extras.get(mid, {}).get("status_red_related_date") or ""
                        ).strip()
                        base_red = (extras.get(mid, {}).get("display_red") or "").strip()
                        if not base_red and red_label:
                            base_red = f"{red_label}[{red_date}]" if red_date else red_label
                        red_candidates: list[tuple[date | None, str]] = []
                        if red_label:
                            red_due = _parse_date(red_date) if red_date else None
                            red_candidates.append((red_due, base_red or red_label))
                        elif base_red:
                            red_candidates.append((None, base_red))
                        for lbl, due in pending_post_filing:
                            if lbl == red_label and _date_only_str(due) == _date_only_str(
                                red_date
                            ):
                                continue
                            red_candidates.append((due, f"{lbl}[{due.strftime('%Y-%m-%d')}]"))
                        dated_red_candidates = [
                            item for item in red_candidates if item[0] is not None
                        ]
                        if dated_red_candidates:
                            extras[mid]["display_red"] = min(
                                dated_red_candidates, key=lambda item: item[0]
                            )[1]
                        elif red_candidates:
                            extras[mid]["display_red"] = red_candidates[0][1]

                        current_display_blue_norm = _auto_status.normalize_blue_status(
                            current_display_blue
                        )
                        display_blue = _auto_status._merge_blue_with_pending_post_filing(
                            display_blue,
                            pending_post_filing,
                            preserve_primary_blue=(
                                current_display_blue_norm in _auto_status._PRIMARY_BLUE_STATES
                                and _auto_status.normalize_blue_status(display_blue)
                                == current_display_blue_norm
                            ),
                        )

                if display_blue:
                    extras[mid]["display_blue"] = display_blue
                if blue:
                    extras[mid]["derived_status_blue"] = blue
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 2.6: {e}")

    # 2.7) Include open MGMT status-red dockets as list red candidates.
    # Detail auto-status can display these even before their work visibility window;
    # list status should show the earliest red candidate as the compact representative.
    try:
        rows = (
            db.session.query(
                DocketItem.matter_id,
                DocketItem.name_ref,
                DocketItem.name_free,
                DocketItem.due_date,
            )
            .filter(DocketItem.matter_id.in_(matter_ids))
            .filter(func.coalesce(DocketItem.is_deleted, False).is_(False))
            .filter(DocketItem.name_ref.isnot(None))
            .filter(func.upper(func.trim(DocketItem.name_ref)).like("MGMT:STATUS_RED:%"))
            .filter(or_(DocketItem.done_date.is_(None), func.trim(DocketItem.done_date) == ""))
            .filter(DocketItem.due_date.isnot(None))
            .filter(func.trim(DocketItem.due_date) != "")
            .all()
        )
        case_by_mid = {str(getattr(c, "matter_id", "") or ""): c for c in (cases or [])}
        by_mid_red_candidates: dict[str, list[tuple[date | None, str]]] = {}
        marker = "MGMT:STATUS_RED:"
        try:
            from app.utils.annuity_deadline_routing import is_annuity_status_red_label
        except Exception:
            is_annuity_status_red_label = None

        for mid, name_ref, name_free, due_raw in rows:
            m = str(mid or "").strip()
            if not m or m not in extras:
                continue
            due_dt = _parse_date(due_raw)
            if not due_dt:
                continue
            ref = str(name_ref or "").strip()
            label = ""
            if ref.upper().startswith(marker):
                label = _auto_status.normalize_red_status(ref[len(marker) :].strip())
            if not label:
                label = _auto_status.normalize_red_status(str(name_free or "").strip())
            if not label:
                continue

            ov = case_by_mid.get(m)
            right_group, matter_type = resolve_profile_case_kind(
                getattr(ov, "right_group", None),
                getattr(ov, "matter_type", None),
            )
            our_ref = str(getattr(ov, "our_ref", "") or "").strip().upper()
            is_pct = matter_type == "PCT" or "PCT" in our_ref
            if is_pct and _auto_status._is_pct_advisory_status_red_label(label):
                continue
            if (
                _auto_status.is_non_action_status_red_label(label)
                or _auto_status._looks_like_non_red_document_title(label)
            ):
                continue
            if is_annuity_status_red_label is not None and is_annuity_status_red_label(label):
                continue

            by_mid_red_candidates.setdefault(m, []).append(
                (due_dt, f"{label}[{due_dt.strftime('%Y-%m-%d')}]")
            )

        for mid, candidates in by_mid_red_candidates.items():
            _set_earliest_display_red(mid, candidates)
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 2.7: {e}")

    # 2) Normalized party roles (inventor/applicant)
    try:
        rows = (
            db.session.query(
                MatterPartyRole.matter_id,
                MatterPartyRole.role_code,
                MatterPartyRole.party_id,
                func.coalesce(Party.name_display, MatterPartyRole.raw_text, ""),
            )
            .outerjoin(Party, Party.party_id == MatterPartyRole.party_id)
            .filter(MatterPartyRole.matter_id.in_(matter_ids))
            .filter(MatterPartyRole.role_code.in_(["inventor", "applicant"]))
            .order_by(
                MatterPartyRole.matter_id.asc(),
                MatterPartyRole.role_code.asc(),
                func.coalesce(MatterPartyRole.seq, 0).asc(),
            )
            .all()
        )
        by_role: dict[tuple[str, str], list[str]] = {}
        applicant_party_ids: dict[str, list[str]] = {}
        for mid, role, party_id, name in rows:
            m = (mid or "").strip()
            r = (role or "").strip()
            pid = (party_id or "").strip()
            n = (name or "").strip()
            if not (m and r and n):
                continue
            by_role.setdefault((m, r), []).append(n)
            if r == "applicant" and pid:
                applicant_party_ids.setdefault(m, [])
                if pid not in applicant_party_ids[m]:
                    applicant_party_ids[m].append(pid)
        for mid in matter_ids:
            inv = by_role.get((mid, "inventor")) or []
            app = by_role.get((mid, "applicant")) or []
            if inv:
                extras[mid]["inventor_name"] = "; ".join(dict.fromkeys(inv))
            if app and mid not in official_applicant_mids:
                extras[mid]["applicant_name"] = "; ".join(dict.fromkeys(app))

        # Resolve applicant -> CRM client_id (best-effort).
        # Prefer party_id linkage (stable), otherwise do a conservative exact-name match.
        try:
            from app.models.client import Client

            # 2.1) party_id -> client_id (unique only)
            party_ids: list[str] = []
            for pids in applicant_party_ids.values():
                for pid in pids:
                    if pid and pid not in party_ids:
                        party_ids.append(pid)

            by_party_to_client_id: dict[str, int] = {}
            if party_ids:
                active = (Client.is_deleted.is_(False)) | (Client.is_deleted.is_(None))
                clients = (
                    Client.query.filter(active)
                    .filter(
                        or_(Client.party_id.in_(party_ids), Client.ipm_party_id.in_(party_ids))
                    )
                    .with_entities(Client.id, Client.party_id, Client.ipm_party_id)
                    .all()
                )
                for cid, pid1, pid2 in clients:
                    for pid in (pid1, pid2):
                        pid = str(pid or "").strip()
                        if pid:
                            by_party_to_client_id[pid] = int(cid)

                for mid in matter_ids:
                    if extras.get(mid, {}).get("applicant_client_id"):
                        continue
                    pids = applicant_party_ids.get(mid) or []
                    resolved = {
                        by_party_to_client_id.get(pid)
                        for pid in pids
                        if by_party_to_client_id.get(pid)
                    }
                    resolved = {int(x) for x in resolved if x}
                    if len(resolved) == 1:
                        extras[mid]["applicant_client_id"] = str(next(iter(resolved)))

            # 2.2) exact-name variants (unique only, single applicant only)
            def _collapse_spaces(value: str) -> str:
                return " ".join(str(value or "").split())

            def _name_variants(name: str) -> list[str]:
                base = _collapse_spaces(str(name or "").strip())
                if not base:
                    return []
                prefixes = ("Company ", "Company ", "()", "㈜", "()")

                def strip_prefix(s: str) -> str:
                    s = s.strip()
                    for pref in prefixes:
                        if s.startswith(pref):
                            return s[len(pref) :].strip()
                    return s

                core = strip_prefix(base)
                variants = {base, core}
                if core:
                    variants.add(f"Company {core}")
                    variants.add(f"(){core}")
                    variants.add(f"㈜{core}")
                return [v for v in variants if v]

            # Build a single batched query for all variants we might need.
            by_mid_variants: dict[str, set[str]] = {}
            all_lower_variants: set[str] = set()
            for mid in matter_ids:
                if extras.get(mid, {}).get("applicant_client_id"):
                    continue
                raw = (extras.get(mid, {}).get("applicant_name") or "").strip()
                if not raw:
                    continue
                # only attempt when it's effectively a single applicant in display text
                first = raw.split(";", 1)[0].split("\n", 1)[0].strip()
                if not first or first != raw:
                    continue
                vars_ = {v.strip().lower() for v in _name_variants(first) if v.strip()}
                if not vars_:
                    continue
                by_mid_variants[mid] = vars_
                all_lower_variants.update(vars_)

            if all_lower_variants:
                active = (Client.is_deleted.is_(False)) | (Client.is_deleted.is_(None))
                rows2 = (
                    Client.query.filter(active)
                    .filter(func.lower(func.trim(Client.name)).in_(sorted(all_lower_variants)))
                    .with_entities(Client.id, Client.name)
                    .all()
                )
                ids_by_lower: dict[str, list[int]] = {}
                for cid, cname in rows2:
                    key = _collapse_spaces(str(cname or "").strip()).lower()
                    if key:
                        ids_by_lower.setdefault(key, []).append(int(cid))

                for mid, vars_ in by_mid_variants.items():
                    if extras.get(mid, {}).get("applicant_client_id"):
                        continue
                    candidate_ids: set[int] = set()
                    for v in vars_:
                        for cid in ids_by_lower.get(v, []):
                            candidate_ids.add(int(cid))
                    if len(candidate_ids) == 1:
                        extras[mid]["applicant_client_id"] = str(next(iter(candidate_ids)))
        except Exception as exc:
            # best-effort; do not break list view
            report_swallowed_exception(
                exc,
                context="case.helpers._build_case_list_extras.applicant_client_id_lookup",
                log_key="case.helpers._build_case_list_extras.applicant_client_id_lookup",
                log_window_seconds=300,
            )
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 2: {e}")

    # 3) Identifiers/events (Application No./Filing date)
    try:
        app_no_id_types = ("Application No.", "APP_NO", "application_no", "app_no", "Text")
        app_no_priority = {
            "Application No.": 0,
            "APP_NO": 1,
            "application_no": 2,
            "app_no": 3,
            "Text": 99,
        }
        picked_priority: dict[str, int] = {}

        rows = (
            db.session.query(
                MatterIdentifier.matter_id,
                MatterIdentifier.id_type,
                MatterIdentifier.id_value,
            )
            .filter(MatterIdentifier.matter_id.in_(matter_ids))
            .filter(MatterIdentifier.id_type.in_(app_no_id_types))
            .order_by(MatterIdentifier.matter_id.asc(), MatterIdentifier.mid_id.asc())
            .all()
        )
        for mid, id_type, v in rows:
            m = (mid or "").strip()
            if not m:
                continue
            value = (v or "").strip()
            if not value:
                continue
            priority = app_no_priority.get((id_type or "").strip(), 99)
            current = picked_priority.get(m)
            if current is None or priority < current:
                if extras.get(m):
                    extras[m]["application_no"] = value
                    picked_priority[m] = priority
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 3: {e}")

    try:
        rows = (
            db.session.query(MatterEvent.matter_id, MatterEvent.event_at)
            .filter(MatterEvent.matter_id.in_(matter_ids))
            .filter(MatterEvent.event_key.in_(("Filing date", "Text")))
            .order_by(
                MatterEvent.matter_id.asc(),
                MatterEvent.event_at.asc(),
                MatterEvent.mevent_id.asc(),
            )
            .all()
        )
        for mid, v in rows:
            m = (mid or "").strip()
            if not m:
                continue
            if extras.get(m) and not extras[m]["application_date"]:
                extras[m]["application_date"] = _date_only_str(v)
    except Exception as e:
        current_app.logger.error(f"Error in _build_case_list_extras 3b: {e}")

    raw_import_cols = [
        "Inventor",
        "Applicant name",
        "Application No.",
        "Filing date",
        "Proposed title",
        "",
        "Type",
        "Registrationtarget.",
        "   .",
    ]

    # 4) Fallback to raw_import_field (Matter) when not normalized.
    # Replaces old raw_import_row usage
    raw_ids = [rid for rid in (matter_id_to_raw_id.get(mid) for mid in matter_ids) if rid]
    if raw_ids:
        try:
            rows = (
                db.session.query(
                    RawImportField.raw_id,
                    RawImportField.source_column,
                    RawImportField.value_text,
                )
                .filter(RawImportField.raw_id.in_(raw_ids))
                .filter(RawImportField.sheet_name == "Matter")
                .filter(RawImportField.source_column.in_(raw_import_cols))
                .all()
            )

            # Group by raw_id -> {col: val}
            raw_map: dict[str, dict[str, str]] = {}
            for rid, col, val in rows:
                rid = (rid or "").strip()
                if not rid:
                    continue
                if rid not in raw_map:
                    raw_map[rid] = {}
                raw_map[rid][(col or "").strip()] = (val or "").strip()

            for mid in matter_ids:
                rid = (matter_id_to_raw_id.get(mid) or "").strip()
                if not rid:
                    continue
                obj = raw_map.get(rid) or {}
                if not obj:
                    continue

                if not extras[mid]["inventor_name"]:
                    extras[mid]["inventor_name"] = (obj.get("Inventor") or "").strip()
                if not extras[mid]["applicant_name"]:
                    extras[mid]["applicant_name"] = (obj.get("Applicant name") or "").strip()
                if not extras[mid]["application_no"]:
                    extras[mid]["application_no"] = (obj.get("Application No.") or "").strip()
                if not extras[mid]["application_date"]:
                    extras[mid]["application_date"] = _date_only_str(obj.get("Filing date"))
                if not extras[mid]["proposal_title"]:
                    extras[mid]["proposal_title"] = (obj.get("Proposed title") or "").strip()
                if not extras[mid]["trademark_classes"]:
                    for key in (
                        "",
                        "Type",
                        "Registrationtarget.",
                        "   .",
                    ):
                        class_value = (obj.get(key) or "").strip()
                        if class_value:
                            extras[mid]["trademark_classes"] = class_value
                            break
        except Exception as e:
            current_app.logger.error(f"Error in _build_case_list_extras 4: {e}")

    # 5) Fallback: if status_red_related_date is not present on matter, use overview.next_due_date
    try:
        for ov in cases or []:
            mid = (getattr(ov, "matter_id", "") or "").strip()
            if not mid or mid not in extras:
                continue
            if extras[mid].get("status_red_related_date"):
                continue
            next_due = ((getattr(ov, "next_due_date", "") or "")).strip()
            if next_due:
                extras[mid]["status_red_related_date"] = _date_only_str(next_due)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.helpers._build_case_list_extras.fallback_next_due",
            log_key="case.helpers._build_case_list_extras.fallback_next_due",
            log_window_seconds=300,
        )

    return extras


_GENERIC_PROPOSAL_TITLES = {
    "DomesticPatent",
    "DomesticTrademark",
    "DomesticDesign",
    "Domestic /Litigation",
    "Domestic/Litigation",
    "ForeignPatent",
    "ForeignTrademark",
    "ForeignDesign",
    "Foreign /Litigation",
    "Foreign/Litigation",
    " · Patent",
    " · Trademark",
    " · Design",
    " · /Litigation",
    "Incoming Patent",
    "Incoming Trademark",
    "Incoming Design",
    "Outgoing Patent",
    "Outgoing Trademark",
    "Outgoing Design",
    "//Litigation",
    "Other",
}


def _is_generic_proposal_title(value: str | None) -> bool:
    v = (value or "").strip()
    return v in _GENERIC_PROPOSAL_TITLES


def _date_only_str(value) -> str:
    """
    Normalize many timestamp-ish inputs to 'YYYY-MM-DD' when possible.
    Accepts: None, str, date, datetime, or any stringifiable value.

    Examples:
      - None -> ''
      - date(2025, 12, 22) -> '2025-12-22'
      - datetime(2025, 12, 22, 15, 30) -> '2025-12-22'
      - '2025-12-22 00:00:00' -> '2025-12-22'
      - '2025.12.24' -> '2025-12-24'
      - '2025/12/24' -> '2025-12-24'
      - '2025-12-24  5:26:00' -> '2025-12-24'
    """
    if value is None:
        return ""

    # Handle datetime first (datetime is subclass of date)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    s = str(value).strip()
    if not s:
        return ""

    # Fast path: already ISO date prefix (YYYY-MM-DD...)
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        head = s[:10]
        if head[0:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit():
            return head

    # Also support '2025.12.24', '2025/12/24', '2025-12-24  ...'
    m = _DATE_YYYY_MM_DD_RE.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    return s


def _bulk_replace_matter_events(
    *, mid: str, src: str, event_pairs: list[tuple[str, object]]
) -> None:
    """
    Replace (mid, src) scoped events in bulk. Executes 2 queries instead of 2N.

    Args:
        mid: matter_id
        src: source_column (e.g., 'form:domestic_patent')
        event_pairs: [(event_key, raw_value), ...] raw_value can be str/date/datetime/None
    """
    mid = (mid or "").strip()
    if not mid:
        return

    key_aliases = {
        "// Upload": ("// Upload", "// "),
        "// ": ("// Upload", "// "),
        " Upload": (" Upload", " "),
        " ": (" Upload", " "),
        "Final rejection Upload": ("Final rejection Upload", "Final rejection "),
        "Final rejection ": ("Final rejection Upload", "Final rejection "),
        "Notice of allowance Upload": ("Notice of allowance Upload", "Notice of allowance "),
        "Notice of allowance ": ("Notice of allowance Upload", "Notice of allowance "),
        "Examination Final rejection Upload": (
            "Examination Final rejection Upload",
            "Examination Final rejection ",
        ),
        "Examination Final rejection ": (
            "Examination Final rejection Upload",
            "Examination Final rejection ",
        ),
        "Publication decision notice Upload": ("Publication decision notice Upload", "Publication decision notice "),
        "Publication decision notice ": ("Publication decision notice Upload", "Publication decision notice "),
    }

    # Normalize dates and filter valid keys
    pairs = [(k, _date_only_str(v)) for (k, v) in event_pairs if (k or "").strip()]
    keys: list[str] = []
    for key, _ in pairs:
        for alias in key_aliases.get(key, (key,)):
            alias_text = str(alias or "")
            if not alias_text.strip():
                continue
            for candidate in (alias_text, alias_text.strip()):
                if candidate and candidate not in keys:
                    keys.append(candidate)

    if keys:
        (
            MatterEvent.query.filter_by(matter_id=mid, source_column=src)
            .filter(MatterEvent.event_key.in_(keys))
            .delete(synchronize_session=False)
        )

    rows = [
        MatterEvent(
            matter_id=mid,
            event_key=k,
            event_at=at,
            source_column=src,
        )
        for (k, at) in pairs
        if (at or "").strip()
    ]
    if rows:
        db.session.add_all(rows)


def _bulk_replace_matter_identifiers(
    *, mid: str, source_column: str, id_pairs: list[tuple[str, str]]
) -> None:
    """
    Replace identifiers for given mid + id_type list in bulk. Executes 2 queries instead of 2N.

    Args:
        mid: matter_id
        source_column: source column name (e.g., 'domestic_patent')
        id_pairs: [(id_type, id_value), ...]
    """
    mid = (mid or "").strip()
    if not mid:
        return

    pairs = [(k, (v or "").strip()) for (k, v) in id_pairs if (k or "").strip()]
    id_types = [k for (k, _) in pairs]
    application_identifier_types = {
        "Application No.",
        "PCT Application No.",
        "EP Application No.",
        " Application No.",
        "CTM Application No.",
        " Application No.",
        "DefaultDesignApplication No.",
        "Parent application No.",
    }
    app_norms = {
        _identifier_norm(v)
        for (k, v) in pairs
        if k in application_identifier_types and _identifier_norm(v)
    }
    if app_norms:
        pairs = [
            (k, v)
            for (k, v) in pairs
            if not (k in {"", "Matter reference"} and _identifier_norm(v) in app_norms)
        ]
    if id_types:
        (
            MatterIdentifier.query.filter(MatterIdentifier.matter_id == mid)
            .filter(MatterIdentifier.id_type.in_(id_types))
            .delete(synchronize_session=False)
        )

    rows = [
        {
            "mid_id": uuid.uuid4().hex,
            "matter_id": mid,
            "id_type": k,
            "id_value": v,
            "country": None,
            "raw_text": v,
            "source_column": source_column,
            "raw_id": None,
        }
        for (k, v) in pairs
        if v
    ]
    if rows:
        db.session.bulk_insert_mappings(MatterIdentifier, rows)


def _identifier_norm(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z-]", "", str(value or "")).upper()


def _clear_duplicate_appeal_no(data: dict) -> None:
    if not isinstance(data, dict):
        return
    appeal_norm = _identifier_norm(data.get("appeal_no"))
    if not appeal_norm:
        return
    app_number_keys = (
        "application_no",
        "parent_application_no",
        "basic_application_no",
        "pct_application_no",
        "ep_application_no",
        "madrid_application_no",
        "ctm_application_no",
        "hague_application_no",
    )
    for key in app_number_keys:
        if appeal_norm and appeal_norm == _identifier_norm(data.get(key)):
            data["appeal_no"] = ""
            return


def _is_yes(value: str | None) -> bool:
    v = (value or "").strip().lower()
    return v in {"y", "yes", "true", "1"}


def _priority_numbers_present(dom_patent: dict) -> bool:
    return bool(
        (dom_patent.get("priority_no") or "").strip()
        or (dom_patent.get("parent_application_no") or "").strip()
    )


def _fill_case_fields_from_ipm(*, matter_obj: Matter, fields: list, data: dict) -> dict:
    """
    Fill missing fields from legacy IPM tables based on ipm_mapping.csv.
    Priority: keep existing data values; only fill when empty.
    """
    matter_id = str(getattr(matter_obj, "matter_id", "") or "")
    matter_raw_id = (getattr(matter_obj, "raw_id", "") or "").strip() or None
    by_source = _load_ipm_case_sheet_mapping()

    label_to_key = {}
    label_to_type = {}
    for pair in fields:
        for cell in pair:
            label, key, field_type = cell[0], cell[1], cell[2]
            if not label or key == "__blank__":
                continue
            label_to_key[str(label).strip()] = str(key).strip()
            label_to_type[str(label).strip()] = str(field_type).strip()

    needed_labels = []
    for label, key in label_to_key.items():
        v = (data.get(key) or "").strip()
        if v and not (key == "proposal_title" and _is_generic_proposal_title(v)):
            continue
        needed_labels.append(label)

    if not needed_labels:
        return data

    raw_field_map: dict[str, str] = {}
    event_map: dict[str, str] = {}
    identifier_map: dict[str, str] = {}
    staff_map: dict[tuple[str, int], str] = {}
    party_role_map: dict[str, list[str]] = {}

    if matter_raw_id:
        try:
            with db.session.begin_nested():
                rows = db.session.execute(
                    text(
                        """
                        SELECT source_column, value_text
                        FROM raw_import_field
                        WHERE raw_id = :rid
                          AND sheet_name = 'Matter'
                        """
                    ).execution_options(policy_bypass=True),
                    {"rid": str(matter_raw_id)},
                ).all()
            raw_field_map = {
                (r[0] or "").strip(): (r[1] or "").strip() for r in rows if (r[0] or "").strip()
            }
        except Exception:
            raw_field_map = {}

    try:
        with db.session.begin_nested():
            rows = db.session.execute(
                text(
                    """
                    SELECT event_key, event_at
                    FROM matter_event
                    WHERE matter_id = :mid
                    """
                ).execution_options(policy_bypass=True),
                {"mid": str(matter_id)},
            ).all()
        for k, v in rows:
            kk = (k or "").strip()
            vv = (v or "").strip()
            if kk and vv and (kk not in event_map):
                event_map[kk] = vv
    except Exception:
        event_map = {}

    event_aliases = {
        "APP_DATE": "Filing date",
        "PUB_DATE": "Publication date",
        "REG_DATE": "Registration date",
    }
    for src, alias in event_aliases.items():
        if src in event_map and alias not in event_map:
            event_map[alias] = event_map[src]

    try:
        with db.session.begin_nested():
            rows = db.session.execute(
                text(
                    """
                    SELECT id_type, id_value
                    FROM matter_identifier
                    WHERE matter_id = :mid
                    """
                ).execution_options(policy_bypass=True),
                {"mid": str(matter_id)},
            ).all()
        for k, v in rows:
            kk = (k or "").strip()
            vv = (v or "").strip()
            if kk and vv and (kk not in identifier_map):
                identifier_map[kk] = vv
    except Exception:
        identifier_map = {}

    identifier_aliases = {
        "APP_NO": "Application No.",
        "REG_NO": "Registration No.",
        "PUB_NO": "Publication No.",
        "application_no": "Application No.",
        "app_no": "Application No.",
        "registration_no": "Registration No.",
        "reg_no": "Registration No.",
        "publication_no": "Publication No.",
        "pub_no": "Publication No.",
    }
    for src, alias in identifier_aliases.items():
        if src in identifier_map and alias not in identifier_map:
            identifier_map[alias] = identifier_map[src]

    try:
        with db.session.begin_nested():
            rows = db.session.execute(
                text(
                    """
                    SELECT
                      msa.staff_role_code,
                      ROW_NUMBER() OVER (
                        PARTITION BY LOWER(TRIM(msa.staff_role_code))
                        ORDER BY
                          COALESCE(NULLIF(TRIM(p.name_display), ''), NULLIF(TRIM(msa.raw_text), ''), '') ASC,
                          COALESCE(NULLIF(TRIM(ps.staff_code), ''), '') ASC,
                          msa.msa_id ASC
                      ) AS seq,
                      p.name_display,
                      ps.staff_code
                    FROM matter_staff_assignment msa
                    JOIN party_staff ps ON ps.party_id = msa.staff_party_id
                    JOIN party p ON p.party_id = ps.party_id
                    WHERE msa.matter_id = :mid
                    ORDER BY msa.staff_role_code ASC, seq ASC
                    """
                ).execution_options(policy_bypass=True),
                {"mid": str(matter_id)},
            ).all()
        for role_code, seq, name_display, staff_code in rows:
            rc = (role_code or "").strip().lower()
            try:
                s = int(seq or 1)
            except Exception:
                s = 1
            val = _format_staff(name_display, staff_code)
            if rc and val and ((rc, s) not in staff_map):
                staff_map[(rc, s)] = val
    except Exception:
        staff_map = {}

    try:
        with db.session.begin_nested():
            rows = db.session.execute(
                text(
                    """
                    SELECT
                      mpr.role_code,
                      mpr.seq,
                      COALESCE(NULLIF(TRIM(p.name_display), ''), NULLIF(TRIM(mpr.raw_text), '')) AS name_display
                    FROM matter_party_role mpr
                    LEFT JOIN party p ON p.party_id = mpr.party_id
                    WHERE mpr.matter_id = :mid
                    ORDER BY mpr.role_code ASC, mpr.seq ASC
                    """
                ).execution_options(policy_bypass=True),
                {"mid": str(matter_id)},
            ).all()
        for role_code, _seq, name_display in rows:
            rc = (role_code or "").strip().lower()
            nm = (name_display or "").strip()
            if not rc or not nm:
                continue
            party_role_map.setdefault(rc, []).append(nm)
    except Exception:
        party_role_map = {}

    def pick_value(label: str) -> str:
        hits = by_source.get(label) or []
        if not hits:
            return ""

        for h in hits:
            target_table = (h.get("target_table") or "").strip()
            mapping_type = (h.get("mapping_type") or "").strip()
            params_raw = (h.get("params_json") or "").strip()
            try:
                params = json.loads(params_raw) if params_raw else {}
            except Exception:
                params = {}

            if target_table == "raw_import_row" and mapping_type == "raw_json_only":
                return (raw_field_map.get(label) or "").strip()

            if target_table == "matter_event":
                event_key = (params.get("event_key") or label or "").strip()
                return (event_map.get(event_key) or "").strip()

            if target_table == "matter_identifier":
                id_type = (params.get("id_type") or label or "").strip()
                return (identifier_map.get(id_type) or "").strip()

            if target_table == "matter_staff_assignment":
                role = (params.get("staff_role_code") or "").strip()
                try:
                    seq = int(params.get("seq") or 1)
                except Exception:
                    seq = 1
                return (staff_map.get((role, seq)) or "").strip()

            if target_table == "matter_party_role":
                role = (params.get("role_code") or "").strip()
                names = party_role_map.get(role) or []
                seen = set()
                uniq = []
                for n in names:
                    if n in seen:
                        continue
                    seen.add(n)
                    uniq.append(n)
                return ", ".join(uniq).strip()

            if target_table == "matter":
                col = (h.get("target_column") or "").strip()
                v = getattr(matter_obj, col, "") if col else ""
                return (v or "").strip()

        return ""

    for label in needed_labels:
        key = label_to_key.get(label) or ""
        if not key:
            continue
        existing = (data.get(key) or "").strip()
        if existing and not (key == "proposal_title" and _is_generic_proposal_title(existing)):
            continue
        val = pick_value(label)
        if (label_to_type.get(label) or "").strip() == "date":
            val = _date_only_str(val)
        if val:
            data[key] = val

    return data


def _fill_dom_patent_from_ipm(*, matter_obj: Matter, dom_patent: dict) -> dict:
    """
    Fill missing fields from legacy IPM tables based on ipm_mapping.csv.
    Priority: keep existing dom_patent values; only fill when empty.
    """
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=DOMESTIC_PATENT_FIELDS, data=dom_patent
    )


def _fill_dom_design_from_ipm(*, matter_obj: Matter, dom_design: dict) -> dict:
    """
    Fill missing fields from legacy IPM tables based on ipm_mapping.csv.
    Priority: keep existing dom_design values; only fill when empty.
    """
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=DOMESTIC_DESIGN_FIELDS, data=dom_design
    )


def _fill_dom_trademark_from_ipm(*, matter_obj: Matter, dom_tm: dict) -> dict:
    """
    Fill missing fields from legacy IPM tables based on ipm_mapping.csv.
    Priority: keep existing dom_tm values; only fill when empty.
    """
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=DOMESTIC_TRADEMARK_FIELDS, data=dom_tm
    )


def _fill_incoming_patent_from_ipm(*, matter_obj: Matter, inc_patent: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=INCOMING_PATENT_FIELDS, data=inc_patent
    )


def _fill_incoming_design_from_ipm(*, matter_obj: Matter, inc_design: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=INCOMING_DESIGN_FIELDS, data=inc_design
    )


def _fill_incoming_trademark_from_ipm(*, matter_obj: Matter, inc_trademark: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=INCOMING_TRADEMARK_FIELDS, data=inc_trademark
    )


def _fill_outgoing_patent_from_ipm(*, matter_obj: Matter, out_patent: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=OUTGOING_PATENT_FIELDS, data=out_patent
    )


def _fill_outgoing_design_from_ipm(*, matter_obj: Matter, out_design: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=OUTGOING_DESIGN_FIELDS, data=out_design
    )


def _fill_outgoing_trademark_from_ipm(*, matter_obj: Matter, out_trademark: dict) -> dict:
    return _fill_case_fields_from_ipm(
        matter_obj=matter_obj, fields=OUTGOING_TRADEMARK_FIELDS, data=out_trademark
    )


def _fill_pct_from_ipm(*, matter_obj: Matter, pct: dict) -> dict:
    return _fill_case_fields_from_ipm(matter_obj=matter_obj, fields=PCT_FIELDS, data=pct)


def _sync_matter_identifiers_from_dom_trademark(*, matter_id: str, dom_trademark: dict) -> None:
    """
    Keep legacy identifier table in sync with domestic_trademark UI fields.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (dom_trademark.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Publication", _v("gazette_no")),
        ("Client ", _v("client_mgmt_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("appeal_no")),
        ("", _v("opposition_no")),
        ("Registration No.", _v("original_registration_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="domestic_trademark",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_dom_trademark(*, matter_id: str, dom_trademark: dict) -> None:
    """
    Keep legacy matter_event table in sync with domestic_trademark UI date fields.

    Note: We only manage rows written by this form (source_column='form:domestic_trademark')
    to avoid destroying imported/normalized event history.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(dom_trademark.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Draft", _v("draft_sent_date")),
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("Examination ", _v("expedited_request_date")),
        ("Examination ", _v("expedited_decision_date")),
        ("Publication date", _v("publication_date")),
        ("Publication", _v("gazette_date")),
        ("Publication decision notice Upload", _v("gazette_decision_received")),
        ("Publication decision", _v("gazette_decision_date")),
        ("  Deadline", _v("special_claim_doc_deadline")),
        (" ", _v("exhibition_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("Registration date", _v("original_registration_date")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("RegistrationPeriod", _v("reg_extension_date")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("Trademark Cancel Billing", _v("cancellation_request_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("ForeignFilingDeadline", _v("foreign_filing_deadline")),
        ("ForeignFiling date", _v("foreign_filing_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:domestic_trademark",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_out_trademark(
    *,
    matter_id: str,
    out_tm: dict | None = None,
    out_trademark: dict | None = None,
) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return
    if out_tm is None and out_trademark is None:
        return
    payload = (
        out_tm
        if isinstance(out_tm, dict)
        else out_trademark if isinstance(out_trademark, dict) else {}
    )

    def _v(key: str) -> str:
        return (payload.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("opposition_no")),
        (" Application No.", _v("madrid_application_no")),
        ("CTM Application No.", _v("ctm_application_no")),
        ("", _v("appeal_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="outgoing_trademark",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_out_trademark(
    *,
    matter_id: str,
    out_tm: dict | None = None,
    out_trademark: dict | None = None,
) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return
    if out_tm is None and out_trademark is None:
        return
    payload = (
        out_tm
        if isinstance(out_tm, dict)
        else out_trademark if isinstance(out_trademark, dict) else {}
    )

    def _v(key: str) -> str:
        return _date_only_str(payload.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        (" Filing date", _v("madrid_application_date")),
        ("CTM Filing date", _v("ctm_application_date")),
        ("Filing O/LSend", _v("application_ol_sent_date")),
        ("Publication date", _v("publication_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("", _v("novelty_grace_date")),
        ("  Deadline", _v("novelty_doc_deadline")),
        ("  ", _v("novelty_doc_submitted")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("Registration O/LSend", _v("reg_ol_sent_date")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" O/LSend", _v("appeal_ol_sent_date")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:outgoing_trademark",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_dom_design(*, matter_id: str, dom_design: dict) -> None:
    """
    Keep legacy identifier table in sync with domestic_design UI fields.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (dom_design.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Publication", _v("gazette_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("appeal_no")),
        ("", _v("opposition_no")),
        ("DefaultDesignApplication No.", _v("basic_application_no")),
        ("DefaultDesignRegistration No.", _v("basic_registration_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="domestic_design",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_dom_design(*, matter_id: str, dom_design: dict) -> None:
    """
    Keep legacy matter_event table in sync with domestic_design UI date fields.

    Note: We only manage rows written by this form (source_column='form:domestic_design').
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(dom_design.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("ForeignFilingDeadline", _v("foreign_filing_deadline")),
        ("Examination ", _v("expedited_request_date")),
        ("Examination ", _v("expedited_decision_date")),
        ("Publication ", _v("early_pub_request_date")),
        ("Publication date", _v("publication_date")),
        ("Publication", _v("gazette_date")),
        ("DefaultDesignFiling date", _v("basic_application_date")),
        ("DefaultDesignRegistration date", _v("basic_registration_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("", _v("novelty_grace_date")),
        ("  Deadline", _v("novelty_doc_deadline")),
        ("  ", _v("novelty_doc_submitted")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("RegistrationDue date", _v("reg_penalty_deadline")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:domestic_design",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_dom_patent(*, matter_id: str, dom_patent: dict) -> None:
    """
    Keep legacy identifier table in sync with domestic_patent UI fields.

    This is required for features that depend on normalized identifiers (e.g., related applications auto-linking).
    Uses bulk replace utility for performance (2 queries instead of 2N).
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="domestic_patent",
        id_pairs=[
            ("Application No.", (dom_patent.get("application_no") or "").strip()),
            ("Registration No.", (dom_patent.get("registration_no") or "").strip()),
            ("Publication No.", (dom_patent.get("publication_no") or "").strip()),
            ("Publication", (dom_patent.get("gazette_no") or "").strip()),
            ("Client ", (dom_patent.get("client_mgmt_no") or "").strip()),
            ("Priority", (dom_patent.get("priority_no") or "").strip()),
            ("Parent application No.", (dom_patent.get("parent_application_no") or "").strip()),
            ("", (dom_patent.get("appeal_no") or "").strip()),
            ("", (dom_patent.get("opposition_no") or "").strip()),
        ],
    )


def _sync_matter_events_from_dom_patent(*, matter_id: str, dom_patent: dict) -> None:
    """
    Keep legacy matter_event table in sync with domestic_patent UI date fields.

    This is required for:
    - list view columns that rely on matter_event (e.g., Filing date)
    - automatic status derivation (Deadline/Deadline )

    Note: We only manage rows written by this form (source_column='form:domestic_patent')
    to avoid destroying imported/normalized event history.
    Uses bulk replace utility for performance (2 queries instead of 2N).
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    _bulk_replace_matter_events(
        mid=mid,
        src="form:domestic_patent",
        event_pairs=[
            ("Filing date", dom_patent.get("application_date")),
            ("Filing deadline", dom_patent.get("filing_deadline")),
            ("ForeignFilingDeadline", dom_patent.get("foreign_filing_deadline")),
            ("PCTFilingDeadline", dom_patent.get("pct_deadline")),
            ("Examination request date", dom_patent.get("exam_request_date")),
            ("Examination request Due date", dom_patent.get("exam_deadline")),
            ("Notice of allowance Upload", dom_patent.get("reg_decision_received")),
            ("Notice of allowance", dom_patent.get("reg_decision_date")),
            ("RegistrationDue date", dom_patent.get("reg_deadline")),
            ("RegistrationDue date", dom_patent.get("reg_penalty_deadline")),
            ("Registration date", dom_patent.get("registration_date")),
            (" Period ", dom_patent.get("term_expiry_date")),
            ("PRIORITY_DATE", dom_patent.get("priority_date")),
            ("Filing date", dom_patent.get("parent_application_date")),
            ("Publication date", dom_patent.get("publication_date")),
            ("Publication", dom_patent.get("gazette_date")),
            ("ForeignFiling date", dom_patent.get("foreign_filing_date")),
            ("Abandoned/Withdrawn", dom_patent.get("abandon_date")),
            ("Done/Closed", dom_patent.get("complete_date")),
        ],
    )


def _sync_matter_party_roles(*, matter_id: str, data: dict) -> None:
    """
    Sync 'inventor_name' and 'applicant_name' from form data to matter_party_role table.
    This enables them to appear in the case list view columns.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    # 1. Prepare new roles
    changes = []

    # Inventor
    inventor_names = [
        x.strip()
        for x in (data.get("inventor_name") or "").replace(";", ",").split(",")
        if x.strip()
    ]
    for idx, name in enumerate(inventor_names, 1):
        changes.append({"role": "inventor", "name": name, "seq": idx})

    # Applicant
    applicant_names = [
        x.strip()
        for x in (data.get("applicant_name") or "").replace(";", ",").split(",")
        if x.strip()
    ]
    for idx, name in enumerate(applicant_names, 1):
        changes.append({"role": "applicant", "name": name, "seq": idx})

    # 2. Delete existing 'inventor' and 'applicant' roles for this matter
    try:
        db.session.execute(
            text(
                """
                DELETE FROM matter_party_role
                WHERE matter_id = :mid
                  AND role_code IN ('inventor', 'applicant')
                """
            ),
            {"mid": mid},
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.helpers._sync_matter_party_roles.delete_existing_roles",
            log_key="case.helpers._sync_matter_party_roles.delete_existing_roles",
            log_window_seconds=300,
        )
        raise

    # 3. Insert new roles
    objs = [
        MatterPartyRole(
            matter_id=mid,
            role_code=item["role"],
            raw_text=item["name"],
            seq=item["seq"],
        )
        for item in changes
    ]
    if objs:
        try:
            db.session.add_all(objs)
        except Exception:
            current_app.logger.exception("Error inserting roles in bulk")


def _sync_matter_identifiers_from_inc_patent(*, matter_id: str, inc_patent: dict) -> None:
    """
    Keep legacy identifier table in sync with incoming_patent UI fields.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return
    inc_patent = dict(inc_patent or {})
    _clear_duplicate_appeal_no(inc_patent)

    def _v(key: str) -> str:
        return (inc_patent.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Publication", _v("gazette_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("Appeal No.", _v("appeal_no")),
        ("Opposition No.", _v("opposition_no")),
        ("PCT Application No.", _v("pct_application_no")),
        ("EP Application No.", _v("ep_application_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="incoming_patent",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_inc_patent(*, matter_id: str, inc_patent: dict) -> None:
    """
    Keep legacy matter_event table in sync with incoming_patent UI date fields.

    Note: We only manage rows written by this form (source_column='form:incoming_patent').
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(inc_patent.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("PCT Filing date", _v("pct_application_date")),
        ("EP Filing date", _v("ep_application_date")),
        ("Examination request date", _v("exam_request_date")),
        ("Examination request Due date", _v("exam_deadline")),
        ("Examination ", _v("expedited_request_date")),
        ("Examination ", _v("expedited_decision_date")),
        ("Publication ", _v("early_pub_request_date")),
        ("Publication date", _v("publication_date")),
        ("Publication", _v("gazette_date")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("RegistrationDue date", _v("reg_penalty_deadline")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        ("Examination Billing Deadline", _v("reexam_request_deadline")),
        ("Examination Billing", _v("reexam_request_date")),
        ("Examination Final rejection Upload", _v("reexam_rejection_received")),
        ("Examination ", _v("reexam_decision_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Examination", _v("exam_deferment_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("", _v("trans_request_date")),
        ("", _v("trans_accept_date")),
        ("Filing", _v("apply_plan_date")),
        ("FinalDeadline", _v("review_end_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:incoming_patent",
        event_pairs=event_pairs,
    )


def _sync_matter_events_from_litigation(*, matter_id: str, litigation: dict) -> None:
    """
    Keep legacy matter_event table in sync with litigation UI date fields.

    Note: We only manage rows written by this form (source_column='form:litigation').
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(litigation.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("/Billing/ Deadline", _v("request_deadline")),
        ("/Billing/ ", _v("request_date")),
        ("  Deadline", _v("detailed_reason_deadline")),
        ("  ", _v("detailed_reason_date")),
        ("// ", _v("decision_date")),
        ("Due date", _v("judgment_appeal_deadline")),
        ("Billing", _v("judgment_appeal_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:litigation",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_inc_design(*, matter_id: str, inc_design: dict) -> None:
    """
    Keep legacy identifier table in sync with incoming_design UI fields.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (inc_design.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Publication", _v("gazette_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("appeal_no")),
        ("", _v("opposition_no")),
        ("DefaultDesignApplication No.", _v("basic_application_no")),
        ("DefaultDesignRegistration No.", _v("basic_registration_no")),
        ("EP Application No.", _v("ep_application_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="incoming_design",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_inc_design(*, matter_id: str, inc_design: dict) -> None:
    """
    Keep legacy matter_event table in sync with incoming_design UI date fields.

    Note: We only manage rows written by this form (source_column='form:incoming_design').
    """
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(inc_design.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("EP Filing date", _v("ep_application_date")),
        ("Examination ", _v("expedited_request_date")),
        ("Examination ", _v("expedited_decision_date")),
        ("Publication ", _v("early_pub_request_date")),
        ("Publication date", _v("publication_date")),
        ("Publication", _v("gazette_date")),
        ("DefaultDesignFiling date", _v("basic_application_date")),
        ("DefaultDesignRegistration date", _v("basic_registration_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("", _v("novelty_grace_date")),
        ("  Deadline", _v("novelty_doc_deadline")),
        ("  ", _v("novelty_doc_submitted")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("RegistrationDue date", _v("reg_penalty_deadline")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("", _v("trans_request_date")),
        ("", _v("trans_accept_date")),
        ("Filing", _v("apply_plan_date")),
        ("FinalDeadline", _v("review_end_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:incoming_design",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_out_patent(*, matter_id: str, out_patent: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (out_patent.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("appeal_no")),
        ("", _v("opposition_no")),
        ("PCT Application No.", _v("pct_application_no")),
        ("EP Registration No.", _v("ep_registration_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="outgoing_patent",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_out_patent(*, matter_id: str, out_patent: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(out_patent.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("Filing O/LSend", _v("application_ol_sent_date")),
        ("PCT Filing date", _v("pct_application_date")),
        ("EP Registration date", _v("ep_registration_date")),
        ("Examination request date", _v("exam_request_date")),
        ("Examination request Due date", _v("exam_deadline")),
        ("Publication date", _v("publication_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("", _v("novelty_grace_date")),
        ("  Deadline", _v("novelty_doc_deadline")),
        ("  ", _v("novelty_doc_submitted")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("Registration O/LSend", _v("reg_ol_sent_date")),
        ("RegistrationPayment", _v("reg_fee_paid_date")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" O/LSend", _v("appeal_ol_sent_date")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:outgoing_patent",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_pct(*, matter_id: str, pct: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (pct.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Publication No.", _v("publication_no")),
        ("Priority", _v("priority_no")),
        ("Client ", _v("client_mgmt_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]
    id_types = [k for (k, _v0) in id_pairs]

    if id_types:
        (
            MatterIdentifier.query.filter(MatterIdentifier.matter_id == mid)
            .filter(MatterIdentifier.id_type.in_(id_types))
            .delete(synchronize_session=False)
        )

    for id_type, id_value in id_pairs:
        if not id_value:
            continue
        db.session.add(
            MatterIdentifier(
                matter_id=mid,
                id_type=id_type,
                id_value=id_value,
                country=None,
                raw_text=id_value,
                source_column="pct",
                raw_id=None,
            )
        )


def _sync_matter_events_from_pct(*, matter_id: str, pct: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(pct.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("Preliminary examination Billing", _v("preliminary_exam_request_date")),
        ("Preliminary examination Due date", _v("preliminary_exam_deadline")),
        ("Publication date", _v("publication_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("", _v("novelty_grace_date")),
        (" Due date", _v("translation_deadline")),
        (" ", _v("translation_submitted_date")),
        (" Upload", _v("international_search_report_received_date")),
        ("Billing Deadline", _v("claim_amendment_deadline")),
        ("Domestic ", _v("national_phase_last_entry_date")),
        ("Domestic Deadline Guidance Due date", _v("national_phase_notice_deadline")),
        ("Domestic Due date", _v("national_phase_deadline")),
        ("Domestic Deadline 1  Notice", _v("national_phase_19m_deadline")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:pct",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_out_design(*, matter_id: str, out_design: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return (out_design.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        (" Application No.", _v("hague_application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("", _v("appeal_no")),
        ("", _v("opposition_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]
    id_types = [k for (k, _v0) in id_pairs]

    if id_types:
        (
            MatterIdentifier.query.filter(MatterIdentifier.matter_id == mid)
            .filter(MatterIdentifier.id_type.in_(id_types))
            .delete(synchronize_session=False)
        )

    for id_type, id_value in id_pairs:
        if not id_value:
            continue
        db.session.add(
            MatterIdentifier(
                matter_id=mid,
                id_type=id_type,
                id_value=id_value,
                country=None,
                raw_text=id_value,
                source_column="outgoing_design",
                raw_id=None,
            )
        )


def _sync_matter_events_from_out_design(*, matter_id: str, out_design: dict) -> None:
    mid = (matter_id or "").strip()
    if not mid:
        return

    def _v(key: str) -> str:
        return _date_only_str(out_design.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        (" Filing date", _v("hague_application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("Filing O/LSend", _v("application_ol_sent_date")),
        ("Publication date", _v("publication_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("", _v("novelty_grace_date")),
        ("  Deadline", _v("novelty_doc_deadline")),
        ("  ", _v("novelty_doc_submitted")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("Registration O/LSend", _v("reg_ol_sent_date")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("", _v("appeal_decision_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:outgoing_design",
        event_pairs=event_pairs,
    )


def _sync_matter_identifiers_from_inc_trademark(
    *,
    matter_id: str,
    inc_tm: dict | None = None,
    inc_trademark: dict | None = None,
) -> None:
    """
    Keep legacy identifier table in sync with incoming_trademark UI fields.
    """
    mid = (matter_id or "").strip()
    if not mid:
        return
    if inc_tm is None and inc_trademark is None:
        return
    payload = (
        inc_tm
        if isinstance(inc_tm, dict)
        else inc_trademark if isinstance(inc_trademark, dict) else {}
    )

    def _v(key: str) -> str:
        return (payload.get(key) or "").strip()

    id_pairs = [
        ("Application No.", _v("application_no")),
        ("Registration No.", _v("registration_no")),
        ("Publication No.", _v("publication_no")),
        ("Publication", _v("gazette_no")),
        ("Priority", _v("priority_no")),
        ("Parent application No.", _v("parent_application_no")),
        ("Registration No.", _v("original_registration_no")),
        ("", _v("opposition_no")),
        (" Application No.", _v("madrid_application_no")),
        ("CTM Application No.", _v("ctm_application_no")),
    ]
    id_pairs = [(k, v) for (k, v) in id_pairs if k]

    _bulk_replace_matter_identifiers(
        mid=mid,
        source_column="incoming_trademark",
        id_pairs=id_pairs,
    )


def _sync_matter_events_from_inc_trademark(
    *,
    matter_id: str,
    inc_tm: dict | None = None,
    inc_trademark: dict | None = None,
) -> None:
    """
    Keep legacy matter_event table in sync with incoming_trademark UI date fields.

    Note: We only manage rows written by this form (source_column='form:incoming_trademark').
    """
    mid = (matter_id or "").strip()
    if not mid:
        return
    if inc_tm is None and inc_trademark is None:
        return
    payload = (
        inc_tm
        if isinstance(inc_tm, dict)
        else inc_trademark if isinstance(inc_trademark, dict) else {}
    )

    def _v(key: str) -> str:
        return _date_only_str(payload.get(key))

    event_pairs: list[tuple[str, str]] = [
        ("Filing date", _v("application_date")),
        ("Filing deadline", _v("filing_deadline")),
        ("Examination ", _v("expedited_request_date")),
        ("Examination ", _v("expedited_decision_date")),
        ("Publication date", _v("publication_date")),
        ("Publication", _v("gazette_date")),
        ("Publication decision notice Upload", _v("gazette_decision_received")),
        ("Publication decision", _v("gazette_decision_date")),
        ("  Deadline", _v("special_claim_doc_deadline")),
        (" ", _v("exhibition_date")),
        ("PRIORITY_DATE", _v("priority_date")),
        ("Filing date", _v("parent_application_date")),
        ("Registration date", _v("original_registration_date")),
        (" Filing date", _v("madrid_application_date")),
        ("CTM Filing date", _v("ctm_application_date")),
        ("Notice of allowance Upload", _v("reg_decision_received")),
        ("Notice of allowance", _v("reg_decision_date")),
        ("RegistrationDue date", _v("reg_deadline")),
        ("RegistrationPeriod", _v("reg_extension_date")),
        ("Registration date", _v("registration_date")),
        (" Period ", _v("term_expiry_date")),
        ("Final rejection Upload", _v("rejection_received_date")),
        ("", _v("rejection_date")),
        (" BillingDeadline", _v("appeal_deadline")),
        (" Billing", _v("appeal_date")),
        ("Trademark Cancel Billing", _v("cancellation_request_date")),
        ("", _v("opposition_date")),
        ("", _v("opposition_decision_date")),
        ("Abandoned/Withdrawn", _v("abandon_date")),
        ("Done/Closed", _v("complete_date")),
        ("", _v("trans_request_date")),
        ("", _v("trans_accept_date")),
        ("Filing", _v("apply_plan_date")),
        ("FinalDeadline", _v("review_end_date")),
        ("Expense", _v("trans_expend_date")),
    ]

    _bulk_replace_matter_events(
        mid=mid,
        src="form:incoming_trademark",
        event_pairs=event_pairs,
    )


def _apply_auto_status_from_db(
    *,
    matter: Matter,
    dom_patent: dict | None = None,
    **kwargs: dict,
) -> None:
    """Compatibility wrapper for older case helper call sites."""
    apply_auto_status_from_db(matter=matter, dom_patent=dom_patent, **kwargs)


def _auto_complete_workflows_from_events(*, matter_id: str) -> None:
    """Compatibility wrapper for older tests/imports."""
    _service_auto_complete_workflows_from_events(matter_id=matter_id)


def _build_staff_picker_context() -> dict:
    """
    Build staff dropdown options from active local app users linked to active staff records.

    This is best-effort and must never block the create/edit page.
    """

    domain = ""
    local_users = []
    management_local_users = []
    attorney_local_users = []
    processing_local_users = []
    try:
        q = (
            db.session.query(User)
            .filter(User.is_active.is_(True))
            .filter(User.staff_party_id.isnot(None))
            .filter(User.staff_party_id != "")
            .join(PartyStaff, PartyStaff.party_id == User.staff_party_id)
            .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
        )
        rows = q.distinct().order_by(User.department.asc(), User.username.asc()).all()
        for u in rows:
            display_name = (u.display_name or "").strip()
            username = (u.username or "").strip()
            email = (u.email or "").strip()

            # Value: use display_name if available, else username, else email
            value = display_name or username or email

            # Label: DisplayName(Staff code)
            if display_name and username:
                label = f"{display_name}({username})"
            elif display_name:
                label = display_name
            else:
                label = username or email or f"User#{u.id}"

            local_users.append(
                {
                    "id": u.id,
                    "staff_party_id": (
                        str(u.staff_party_id).strip() if u.staff_party_id is not None else None
                    ),
                    "value": value,
                    "label": label,
                    "dept": (u.department or "").strip() or None,
                    "email": email.lower() if email else "",
                }
            )
        try:
            lists = build_staff_assignment_lists()
            management_ids = {
                int(u.id) for u in (lists.get("management_users") or []) if getattr(u, "id", None)
            }
            attorney_ids = {
                int(u.id)
                for u in (
                    (lists.get("attorney_users") or []) or (lists.get("professional_users") or [])
                )
                if getattr(u, "id", None)
            }
            processing_ids = {
                int(u.id)
                for u in ((lists.get("processing_users") or []) or (lists.get("all_users") or []))
                if getattr(u, "id", None)
            }
            management_local_users = [
                u for u in local_users if int(u.get("id") or 0) in management_ids
            ]
            attorney_local_users = [u for u in local_users if int(u.get("id") or 0) in attorney_ids]
            processing_local_users = [
                u for u in local_users if int(u.get("id") or 0) in processing_ids
            ]
        except Exception:
            management_local_users = list(local_users)
            attorney_local_users = list(local_users)
            processing_local_users = list(local_users)
    except Exception:
        local_users = []
        management_local_users = []
        attorney_local_users = []
        processing_local_users = []

    return {
        "domain": domain or "",
        "local_users": local_users,
        "management_local_users": management_local_users or local_users,
        "attorney_local_users": attorney_local_users or local_users,
        "processing_local_users": processing_local_users or local_users,
        "groups": [],
        "org_units": [],
        "has_any": bool(local_users),
    }


def _build_staff_assignment_context() -> dict:
    """
    Build categorized staff lists for DOM/PATENT create/edit pages.

    Uses local login users, optionally filtered by staff-role configs via
    `build_staff_assignment_lists()`.
    """

    def _to_opt(u: User) -> dict:
        email = (u.email or "").strip()
        username = (u.username or "").strip()
        display_name = (u.display_name or "").strip()

        # Value: use display_name if available, else username, else email
        value = display_name or username or email

        # Label: DisplayName(Staff code)
        if display_name and username:
            label = f"{display_name}({username})"
        elif display_name:
            label = display_name
        else:
            label = username or email or f"User#{u.id}"

        return {
            "id": u.id,
            "staff_party_id": (
                str(u.staff_party_id).strip() if u.staff_party_id is not None else None
            ),
            "value": value,
            "label": label,
            "email": email.lower() if email else "",
            "dept": (u.department or "").strip(),
        }

    try:
        lists = build_staff_assignment_lists()
        all_users = [_to_opt(u) for u in (lists.get("all_users") or [])]
        management_users = [_to_opt(u) for u in (lists.get("management_users") or [])]
        professional_users = [_to_opt(u) for u in (lists.get("professional_users") or [])]
        attorney_users = [_to_opt(u) for u in (lists.get("attorney_users") or [])]
        processing_users = [_to_opt(u) for u in (lists.get("processing_users") or [])]
    except Exception:
        all_users = []
        management_users = []
        professional_users = []
        attorney_users = []
        processing_users = []

    return {
        "all_users": all_users,
        "management_users": management_users,
        "professional_users": professional_users,
        "attorney_users": attorney_users or professional_users,
        "processing_users": processing_users or all_users,
        "has_any": bool(all_users),
    }


# Moved from upload.py
class _MatterWrapper:
    """Wrapper to make Matter objects compatible with Case-based deadline creation."""

    def __init__(self, matter):
        self._matter = matter
        # Use matter_id as id for DocketItem-based deadline
        self.id = matter.matter_id
        self.matter_id = matter.matter_id
        # Matter doesn't have manager_id/attorney_id, use None
        self.manager_id = None
        self.attorney_id = None
        self.our_ref = matter.our_ref
        self.is_matter = True  # Flag to indicate this is a Matter, not a Case


_OUR_REF_RE = re.compile(
    r"(?i)(\d{2})\s*[-_/]?\s*([A-Z]{2})\s*[-_/]?\s*(\d{4})\s*(?:[-_/]?\s*([A-Z]{2}))?"
)


def _strip_ref_noise(token: str) -> str:
    """Trim non-alphanumeric characters wrapped around a token (e.g., {{REF}})."""
    return re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", token or "")


def _parse_refs_from_text(src_text: str):
    refs = set()
    # Handle ranges e.g., 22PD0147US~22PD0150US and whitespace separated
    tokens = re.split(r"\s+", src_text or "")
    for tok in tokens:
        tok = _strip_ref_noise(tok.strip())
        if not tok:
            continue
        if "~" in tok:
            start, end = tok.split("~", 1)
            refs |= _expand_ref_range(_strip_ref_noise(start), _strip_ref_noise(end))
        else:
            for m in _OUR_REF_RE.finditer(tok):
                yy, ss, num, cc = m.groups()
                cc = cc or ""
                refs.add(f"{yy}{ss}{int(num):04d}{cc}".upper())
    return refs


def _normalize_ref(token: str):
    m = _OUR_REF_RE.search(token or "")
    if not m:
        return None
    yy, ss, num, cc = m.groups()
    cc = cc or ""
    return f"{yy}{ss}{int(num):04d}{cc}".upper()


def _normalize_our_ref_input(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[\s\-_\/]+", "", raw)
    return cleaned.upper()


def _normalize_date_input(value: str | None, label: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        flash(f"{label}   .", "warning")
        return None


def _expand_ref_range(start: str, end: str):
    s = _OUR_REF_RE.search(_strip_ref_noise(start))
    e = _OUR_REF_RE.search(_strip_ref_noise(end))
    results = set()
    if not s or not e:
        return results
    sy, ss, sn, sc = s.groups()
    ey, es, en, ec = e.groups()
    sc = sc or ""
    ec = ec or ""
    # Only expand when year, type and country match
    if sy.upper() != ey.upper() or ss.upper() != es.upper() or sc.upper() != ec.upper():
        return results
    s_n = int(sn)
    e_n = int(en)
    if e_n < s_n:
        s_n, e_n = e_n, s_n
    for n in range(s_n, e_n + 1):
        results.add(f"{sy}{ss}{n:04d}{sc}".upper())
    return results


def _parse_int(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(str(value).strip())
    except Exception:
        return default


def _to_int(value):
    try:
        return int(value) if value not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def _get_case_date(case: Case, field_name: str):
    """Return a date-like field from Case or its extended_info JSON."""
    if not field_name:
        return None
    if hasattr(case, field_name):
        val = getattr(case, field_name, None)
    else:
        info = getattr(case, "extended_info", None) or {}
        val = info.get(field_name)

    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except Exception:
            return None
    return val


def _to_int_list(csv_str):
    vals = []
    for token in (csv_str or "").split(","):
        token = token.strip()
        if token:
            try:
                vals.append(int(token))
            except ValueError:
                pass
    return vals


def _ensure_case_type_column():
    try:
        insp = inspect(db.engine)
        cols = [c["name"] for c in insp.get_columns("cases")]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.helpers._ensure_case_type_column.inspect",
            log_key="case.helpers._ensure_case_type_column.inspect",
            log_window_seconds=300,
        )
        return
    if "case_type" not in cols:
        report_swallowed_exception(
            RuntimeError("cases.case_type column missing; apply migrations before using case_type"),
            context="case.helpers._ensure_case_type_column.missing_column",
            log_key="case.helpers._ensure_case_type_column.missing_column",
            log_window_seconds=300,
        )


def _ensure_attorney_column():
    try:
        insp = inspect(db.engine)
        cols = [c["name"] for c in insp.get_columns("cases")]
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.helpers._ensure_attorney_column.inspect",
            log_key="case.helpers._ensure_attorney_column.inspect",
            log_window_seconds=300,
        )
        return
    if "attorney_id" not in cols:
        report_swallowed_exception(
            RuntimeError(
                "cases.attorney_id column missing; apply migrations before using attorney_id"
            ),
            context="case.helpers._ensure_attorney_column.missing_column",
            log_key="case.helpers._ensure_attorney_column.missing_column",
            log_window_seconds=300,
        )


def _get_special_template(div, ctype):
    if ctype == "UTILITY":
        ctype = "PATENT"
    if div == "DOM" and ctype == "PATENT":
        return "case/create_dom_patent.html"
    if div == "DOM" and ctype == "TRADEMARK":
        return "case/create_dom_tm.html"
    if div == "DOM" and ctype == "DESIGN":
        return "case/create_dom_design.html"
    if div == "DOM" and ctype == "LITIGATION":
        return "case/create_dom_litigation.html"

    if div == "INC" and ctype == "PATENT":
        return "case/create_inc_patent.html"
    if div == "INC" and ctype == "TRADEMARK":
        return "case/create_inc_tm.html"
    if div == "INC" and ctype == "DESIGN":
        return "case/create_inc_design.html"
    if div == "INC" and ctype == "LITIGATION":
        return "case/create_inc_litigation.html"

    if div == "OUT" and ctype == "PATENT":
        return "case/create_out_patent.html"
    if div == "OUT" and ctype == "TRADEMARK":
        return "case/create_out_tm.html"
    if div == "OUT" and ctype == "DESIGN":
        return "case/create_out_design.html"
    if div == "OUT" and ctype == "LITIGATION":
        return "case/create_out_litigation.html"
    return "case/create.html"


def _save_case_data(obj, form, request_form):
    ctype = form.category.data
    obj.case_type = ctype
    obj.ref_no = form.our_ref.data
    obj.app_no = form.filing_no.data
    obj.title = form.title.data or ""
    obj.division = form.in_out_type.data
    obj.country = form.country.data
    obj.client_id = _to_int(form.client_id.data)
    obj.attorney_id = form.attorney_id.data
    obj.manager_id = form.manager_id.data or (
        current_user.id if current_user.is_authenticated else None
    )
    obj.filing_date = form.filing_date.data

    # Extended fields (CasePatent specific)
    if ctype in PATENT_LIKE_TYPES:
        obj.app_type = request_form.get("app_type")
        obj.grade = request_form.get("grade")
        obj.exam_req_yn = request_form.get("exam_req_yn") == "Y"
        obj.exam_req_date = _parse_date(request_form.get("exam_req_date"))
        obj.pub_date = _parse_date(request_form.get("pub_date"))
        obj.pub_no = request_form.get("pub_no")
        obj.claims_total = _to_int(request_form.get("claims_total"))
        obj.claims_indep = _to_int(request_form.get("claims_indep"))
        obj.claims_dep = _to_int(request_form.get("claims_dep"))
        obj.page_count = _to_int(request_form.get("page_count"))

        # New fields for INC/General
        obj.app_route = request_form.get("app_route")
        obj.reg_date = _parse_date(request_form.get("reg_date"))
        obj.reg_no = request_form.get("reg_no")
        obj.original_app_date = _parse_date(request_form.get("original_app_date"))
        obj.original_app_no = request_form.get("original_app_no")

    # Extended fields (CaseTrademark specific)
    elif ctype == "TRADEMARK":
        obj.app_type = request_form.get("app_type")
        obj.tm_type = request_form.get("tm_type")
        obj.tm_name = request_form.get("tm_name")
        obj.nice_classes = request_form.get("nice_classes")
        obj.designated_goods = request_form.get("designated_goods")
        obj.pub_date = _parse_date(request_form.get("pub_date"))
        obj.pub_no = request_form.get("pub_no")
        obj.reg_date = _parse_date(request_form.get("reg_date"))
        obj.reg_no = request_form.get("reg_no")
        obj.priority_date = _parse_date(request_form.get("priority_date"))
        obj.priority_no = request_form.get("priority_no")

    elif ctype == "DESIGN":
        obj.exam_type = request_form.get("exam_type")
        obj.is_partial = request_form.get("custom_field_is_partial") == "Y"
        # obj.is_multiple = request_form.get("custom_field_is_multiple") == "Y" # If we had it in model
        obj.article_name = form.title.data
        obj.drawing_count = _to_int(request_form.get("drawing_count"))
        obj.related_app_no = request_form.get("related_app_no")

        obj.priority_claim_yn = request_form.get("priority_claim_yn") == "Y"
        obj.priority_date = _parse_date(request_form.get("priority_date"))
        obj.priority_no = request_form.get("priority_no")

        obj.pub_date = _parse_date(request_form.get("pub_date"))
        obj.reg_date = _parse_date(request_form.get("reg_date"))
        obj.reg_no = request_form.get("reg_no")

    elif ctype == "LITIGATION":
        obj.trial_type = request_form.get("trial_type")
        obj.court = request_form.get("court")
        obj.plaintiff = request_form.get("plaintiff")
        obj.defendant = request_form.get("defendant")
        obj.result = request_form.get("result")
        obj.judgment_date = _parse_date(request_form.get("judgment_date"))
        obj.related_case_id = _to_int(request_form.get("related_case_id"))

    custom_data = obj.extended_info or {}
    if form.client_ref.data:
        custom_data["client_ref"] = form.client_ref.data
    if form.reg_date.data:
        custom_data["reg_date"] = form.reg_date.data.isoformat()
    if form.reg_no.data:
        custom_data["reg_no"] = form.reg_no.data
    if form.summary.data:
        custom_data["summary"] = form.summary.data

    # Capture ANY custom_field_* from request_form
    for k, v in request_form.items():
        if k.startswith("custom_field_"):
            field_name = k.replace("custom_field_", "")
            stripped = v.strip()
            if stripped:
                custom_data[field_name] = stripped
            elif field_name in custom_data:
                del custom_data[field_name]

    obj.extended_info = custom_data or None


def _save_foreign_info(obj, request_form, *, commit: bool = False):
    f_agent_id = request_form.get("foreign_agent_id")
    pct_date = request_form.get("pct_app_date")
    pct_no = request_form.get("pct_app_no")
    ep_date = request_form.get("ep_app_date")
    ep_no = request_form.get("ep_app_no")

    if any([f_agent_id, pct_date, pct_no, ep_date, ep_no]):
        from app.models.case_details import CaseForeignInfo

        fi = CaseForeignInfo.query.filter_by(case_id=obj.id).first()
        if not fi:
            fi = CaseForeignInfo(case_id=obj.id)
            db.session.add(fi)

        fi.foreign_agent_id = _to_int(f_agent_id) if f_agent_id else None
        fi.pct_app_date = _parse_date(pct_date)
        fi.pct_app_no = pct_no
        fi.ep_app_date = _parse_date(ep_date)
        fi.ep_app_no = ep_no
        if commit:
            db.session.commit()
        else:
            db.session.flush()


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # e.g. Feb 29 -> Feb 28
        return d.replace(month=2, day=28, year=d.year + years)


def _parse_int_strict(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


_FORM_IGNORE_KEYS = {
    "csrf_token",
    "idempotency_key",
    "submit",
    "image_file",
    "family_link_target_id",
}
_FORM_CORE_KEYS = {
    "our_ref",
    "old_our_ref",
    "your_ref",
    "right_name",
    "inhouse_status",
    "retained_at",
    "entered_at",
    "memo",
    "division",
    "type",
    "case_type",
    "matter_id",
    "case_id",
    "popup",
    "invoice_id",
    "client_id",
    "client_name",
    "applicant_name",
    "applicant_id",
    "applicant_registrant",
    "same_client",
    "applicant_same_as_client",
    "attorney_id",
    "manager_id",
    "handler_id",
}


def _is_date_field(key: str) -> bool:
    if not key:
        return False
    try:
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
        field = registry.get(key)
    except Exception:
        field = None
    if field and (field.input_type == "date" or field.serializer == "date"):
        return True
    return key.endswith("_date") or key.endswith("_deadline")


def _normalize_date_strict(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return ""
    allowed_formats = (
        (r"^\d{4}-\d{2}-\d{2}$", "%Y-%m-%d"),
        (r"^\d{4}/\d{2}/\d{2}$", "%Y/%m/%d"),
        (r"^\d{4}\.\d{2}\.\d{2}$", "%Y.%m.%d"),
        (r"^\d{8}$", "%Y%m%d"),
    )
    for pattern, fmt in allowed_formats:
        if not re.match(pattern, raw):
            continue
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _validate_custom_field_updates(
    *,
    matter_id: str,
    namespace: str,
    form_data: dict,
    allowed_keys: list[str],
    strict_dates: bool = True,
) -> dict:
    from app.services.case.form_support import validate_custom_field_updates

    return validate_custom_field_updates(
        matter_id=matter_id,
        namespace=namespace,
        form_data=form_data,
        allowed_keys=allowed_keys,
        strict_dates=strict_dates,
    )


def _log_custom_field_filtering(
    *,
    matter_id: str,
    namespace: str,
    form_data: dict,
    allowed_keys: list[str],
) -> None:
    try:
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
    except Exception:
        registry = None

    if not registry:
        return

    allowed_set = set(allowed_keys)
    dropped: list[str] = []
    unknown: list[str] = []

    for key in form_data.keys():
        if should_skip_custom_field_filter_key(key, allowed_set):
            continue
        if registry.exists(key):
            if key not in allowed_set:
                dropped.append(key)
        else:
            unknown.append(key)

    if dropped or unknown:
        current_app.logger.warning(
            "Case update key filtering (matter_id=%s, namespace=%s): allowed=%s, dropped=%s, unknown=%s",
            str(matter_id),
            namespace,
            len(allowed_set),
            ",".join(sorted(set(dropped))),
            ",".join(sorted(set(unknown))),
        )


def _apply_same_client_logic_helper(form_data: dict, context_data: dict | None) -> None:
    if not context_data:
        return
    if _is_yes(form_data.get("same_client")):
        form_data["client_id"] = context_data.get("client_id") or ""
        form_data["client_name"] = context_data.get("client_name") or ""
        form_data["applicant_id"] = context_data.get("applicant_id") or ""
        form_data["applicant_name"] = context_data.get("applicant_name") or ""
        form_data["applicant_registrant"] = context_data.get("applicant_registrant") or ""


_ASSISTANT_PREFILL_FIELDS = {
    "our_ref",
    "old_our_ref",
    "your_ref",
    "right_name",
    "client_name",
    "client_id",
    "applicant_name",
    "application_no",
    "application_country",
    "publication_no",
    "registration_no",
    "application_date",
    "registration_date",
    "inhouse_status",
    "retained_at",
    "entered_at",
    "manager",
    "attorney",
    "handler",
    "memo",
    "country",
    "tm_name",
    "tm_type",
    "nice_classes",
    "designated_goods",
}


def _extract_prefill_params(args) -> dict:
    prefill = {}
    for key in _ASSISTANT_PREFILL_FIELDS:
        value = (args.get(key) or "").strip()
        if value:
            prefill[key] = value
    return prefill


def _hx_hard_redirect_response(endpoint: str, **url_values):
    if request.method != "GET":
        return None
    if (request.headers.get("HX-Request") or "").lower() != "true":
        return None
    params = {}
    for key, value in (url_values or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        params[key] = value
    return ("", 200, {"HX-Redirect": url_for(endpoint, **params)})


def _normalize_our_ref_input(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[\s\-_\/]+", "", raw)
    return cleaned.upper()


def _normalize_date_input(value: str | None, label: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        flash(f"{label}   .", "warning")
        return None


_CREATE_ALLOWED_DIVISIONS = {"DOM", "INC", "OUT", "ETC"}
_CREATE_ALLOWED_TYPES = PATENT_LIKE_TYPES | {
    "DESIGN",
    "TRADEMARK",
    "PCT",
    "MADRID",
    "HAGUE",
    "COPYRIGHT",
    "LITIGATION",
    "MISC",
}


def _is_valid_create_kind(division: str, case_type: str) -> bool:
    if not case_type:
        return False
    try:
        from app.services.case.case_menu_config import is_configured_case_menu_kind

        if is_configured_case_menu_kind(division, case_type):
            return True
    except Exception:
        current_app.logger.debug("case menu kind validation lookup failed", exc_info=True)
    if case_type == "PCT":
        return division in {"ETC", "OUT"}
    if case_type in {"MADRID", "HAGUE", "COPYRIGHT"}:
        return division == "ETC"
    if case_type in {"LITIGATION", "MISC"}:
        return division in {"", "ETC"}
    if case_type not in _CREATE_ALLOWED_TYPES:
        return False
    return division in _CREATE_ALLOWED_DIVISIONS
