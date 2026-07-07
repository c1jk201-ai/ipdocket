from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from flask import current_app, flash, g, has_request_context, request, url_for
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased, joinedload, load_only, noload

# Helpers from parent package
from app.blueprints.case.helpers import *
from app.blueprints.case.helpers import _sync_matter_events_from_dom_design

# Audit section default preview stays at .limit(5); see detail_audit.py.
from app.blueprints.case.services.detail_annuity import (
    build_annuity_empty_hint as _build_annuity_empty_hint,
)
from app.blueprints.case.services.detail_audit import build_audit_section as _build_audit_section
from app.blueprints.case.services.detail_finance import build_costs_section as _build_costs_section
from app.blueprints.case.services.detail_file_manager import (
    build_file_manager_section as _build_file_manager_section,
)
from app.blueprints.case.services.detail_history import (
    build_history_dataset as _build_history_dataset,
)
from app.blueprints.case.services.detail_history import (
    build_history_panel_context as _build_history_panel_context,
)
from app.blueprints.case.services.detail_history import (
    format_history_section_merge_groups as _format_history_section_merge_groups,
)
from app.blueprints.case.services.detail_history import (
    load_notice_send_prompt_communications as _load_notice_send_prompt_communications,
)
from app.blueprints.case.services.detail_light_sections import (
    build_deadlines_panel_context as _build_deadlines_panel_context,
)
from app.blueprints.case.services.detail_light_sections import (
    build_memo_panel_context as _build_memo_panel_context,
)
from app.extensions import db
from app.models.case_audit_log import CaseAuditLog
from app.models.matter_facts import MatterFacts
from app.models.ip_records import (
    AnnuityItem,
    DocketItem,
    ExternalInvoiceCaseLink,
    Family,
    FileAsset,
    Matter,
    MatterCustomField,
    MatterFamily,
    MatterFileAsset,
    MatterIdentifier,
    MatterMemo,
    MatterMemoFileAsset,
    MatterStaffAssignment,
    RawImportField,
    VMatterOverview,
)
from app.models.user import User
from app.models.workflow import Workflow
from app.services.case.case_kind import (
    format_public_case_kind_label,
    is_uspto_managed_matter,
    resolve_public_case_kind,
    resolve_public_case_kind_for_matter,
)
from app.services.case.case_parameter_service import CaseParameterService
from app.services.citations.cited_reference_service import matter_office_action_citation_groups
from app.services.core.staff_options import build_staff_assignment_lists
from app.services.matter.matter_auto_status import date_only_str as _date_only_str
from app.services.matter.matter_auto_status import (
    derive_auto_status,
    has_supporting_red_signal,
    is_known_deadline_red_label,
)
from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter
from app.services.matter.pct_related_application import (
    build_related_application_suggestion,
)
from app.services.workflow.status_sync import workflow_display_values
from app.utils.annuity_deadline_routing import calendar_endpoint_for_docket
from app.utils.coercion import coerce_int
from app.utils.docket_dates import effective_due_for_work, effective_due_text_expr
from app.utils.docket_visibility import is_visible_by_date
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text
from app.utils.renewal_labels import (
    normalize_renewal_jurisdiction,
    normalize_renewal_right_type,
    renewal_cycle_label,
)

# Import category constants from central location
from app.utils.task_classification import MGMT_CATEGORIES, WORK_CATEGORIES
from app.utils.task_distribution_rules import resolve_distribution_decision
from app.utils.workflow_list_status import compute_workflow_list_status
from app.utils.workflow_semantics import (
    derive_workflow_category,
    normalize_workflow_category,
    workflow_owner_role_codes,
)

# Defined locally to ensure self-containment as per migration
_BASIC_CANONICAL_STAFF_KEYS = {"attorney", "manager", "handler"}


def _overlay_basic_staff_fields(data: dict, basic_data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    if not isinstance(basic_data, dict):
        return data

    for k in _BASIC_CANONICAL_STAFF_KEYS:
        if k in basic_data:
            data[k] = (basic_data.get(k) or "").strip()
    return data


def _rollback_session() -> None:
    try:
        db.session.rollback()
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_context.rollback_session",
            log_key="case.detail_context.rollback_session",
            log_window_seconds=300,
        )


def _is_missing_table_error(err: Exception, table: str) -> bool:
    """
    Best-effort detection for missing-table errors across Postgres/SQLite.
    - Postgres: psycopg2.errors.UndefinedTable / 'relation "<table>" does not exist'
    - SQLite: 'no such table: <table>'
    """
    msg = (str(err) or "").lower()
    t = (table or "").lower()
    if t and t not in msg:
        return False
    return ("undefinedtable" in msg) or ("does not exist" in msg) or ("no such table" in msg)


def _collapse_spaces(value: str) -> str:
    return " ".join(str(value or "").split())


def _detail_int_cfg(key: str, default: int, *, min_v: int = 1, max_v: int = 5000) -> int:
    raw = current_app.config.get(key, default)
    val = coerce_int(raw, default) or int(default)
    return max(min_v, min(max_v, val))


def _as_date_only(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _as_datetime(value: date | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return None


def _extract_task_source_from_docket_item(docket_item: DocketItem | None) -> str | None:
    if not docket_item:
        return None
    raw_source = (getattr(docket_item, "source", None) or "").strip()
    if raw_source:
        return raw_source.lower()

    memo = (getattr(docket_item, "memo", None) or "").strip()
    payload: dict[str, object] = {}
    if memo:
        try:
            parsed = json.loads(memo)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
            src = str(payload.get("source") or "").strip()
            if src:
                return src.lower()
            if isinstance(payload.get("events"), list):
                return "upload_automation"
            trigger = str(payload.get("trigger") or "").strip()
            if trigger in {"REGISTRATION_CERTIFICATE", "Notice of allowance", "Final rejection"}:
                return "uspto_notice"

    category = (getattr(docket_item, "category", None) or "").strip().upper()
    name_ref = (getattr(docket_item, "name_ref", None) or "").strip().upper()
    if name_ref.startswith("USPTO:"):
        return "uspto_notice"
    if name_ref.startswith("USPTO_OA:"):
        return "upload_automation"
    if category == "USPTO_OA":
        return "upload_automation"
    return None


def _resolve_case_view_flow_role_codes(
    *,
    wf: Workflow,
    linked_docket_item: DocketItem | None,
) -> tuple[str, ...]:
    hint_category = (
        getattr(linked_docket_item, "category", None)
        if linked_docket_item is not None
        else getattr(wf, "category", None)
    )
    hint_name_ref = (
        getattr(linked_docket_item, "name_ref", None) if linked_docket_item is not None else None
    )
    hint_name_free = (
        getattr(linked_docket_item, "name_free", None)
        if linked_docket_item is not None
        else getattr(wf, "name", None)
    )
    source = _extract_task_source_from_docket_item(linked_docket_item)
    handler_id = getattr(wf, "assignee_id", None)
    attorney_id = getattr(wf, "attorney_assignee_id", None)
    manager_id = getattr(wf, "inspector_id", None)

    decision = resolve_distribution_decision(
        category=hint_category,
        name_ref=hint_name_ref,
        name_free=hint_name_free,
        source=source,
    )
    if decision.distribute_to == "owner":
        if handler_id:
            return ("handler",)
        if attorney_id:
            return ("attorney",)
        if manager_id:
            return ("manager",)
        return ()
    if decision.distribute_to == "all_staff":
        candidates = (
            ("attorney", attorney_id),
            ("manager", manager_id),
            ("handler", handler_id),
        )
        return tuple(role for role, raw_id in candidates if raw_id)
    if decision.distribute_to == "role_set" and decision.role_codes:
        role_id_map = {
            "attorney": attorney_id,
            "manager": manager_id,
            "handler": handler_id,
        }
        return tuple(role for role in decision.role_codes if role_id_map.get(role))

    resolved_category = derive_workflow_category(
        case_id=str(getattr(wf, "case_id", None) or "") or None,
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
        manual_category=getattr(wf, "category", None),
        hint_category=hint_category,
        hint_name_ref=hint_name_ref,
        hint_name_free=hint_name_free,
        source=source,
    )
    return workflow_owner_role_codes(
        category=resolved_category,
        handler_id=handler_id,
        attorney_id=attorney_id,
        manager_id=manager_id,
    )


def _workflow_occurrence_date(wf: Workflow) -> date | None:
    return _as_date_only(getattr(wf, "request_start_date", None)) or _as_date_only(
        getattr(wf, "created_at", None)
    )


def _workflow_display_due_date(wf: Workflow) -> date | None:
    return (
        _as_date_only(getattr(wf, "_display_legal_due_date", None))
        or _as_date_only(getattr(wf, "_display_due_date", None))
        or _as_date_only(getattr(wf, "legal_due_date", None))
        or _as_date_only(getattr(wf, "due_date", None))
    )


def _workflow_occurrence_sort_key(wf: Workflow) -> tuple[date, date, datetime, int]:
    occurrence_date = _workflow_occurrence_date(wf) or date.max
    display_due_date = _workflow_display_due_date(wf) or date.max
    created_at = _as_datetime(getattr(wf, "created_at", None)) or datetime.max
    try:
        workflow_id = int(getattr(wf, "id", 0) or 0)
    except Exception:
        workflow_id = 0
    return (occurrence_date, display_due_date, created_at, workflow_id)


def _resolve_case_view_workflow_category(
    raw_category: str | None,
    *,
    has_manager: bool = False,
    has_work_assignee: bool = False,
) -> str | None:
    normalized = normalize_workflow_category(raw_category)
    if normalized == "MGMT_WORK":
        return "hybrid"
    if normalized == "MGMT":
        return "mgmt"
    if normalized == "WORK":
        return "work"
    if has_manager and has_work_assignee:
        return "hybrid"
    return None


def _split_party_names(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    for token in ("\n", ",", ";", "/", "|", "&", "\u00b7", "\u30fb"):
        s = s.replace(token, ",")
    parts = [p.strip() for p in s.split(",") if p and p.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = _collapse_spaces(p)
        if p and p not in seen:
            out.append(p)
            seen.add(p)
    return out


def _client_name_variants(name: str) -> list[str]:
    base = _collapse_spaces(str(name or "").strip())
    if not base:
        return []

    prefixes = ("Company ", "\uc8fc\uc2dd\ud68c\uc0ac ", "(\uc8fc)", "\u321c")

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
        variants.add(f"(\uc8fc){core}")
        variants.add(f"㈜{core}")

    return [v for v in variants if v]


def _resolve_applicant_client_ids(applicant_names: list[str]) -> dict[str, int]:
    """
    Best-effort, conservative applicant->CRM client matching.

    - Only links when a single active CRM Client matches by exact-name variants.
    - Otherwise, the UI falls back to CRM search.
    """
    if not applicant_names:
        return {}

    try:
        from app.models.client import Client
    except ImportError:
        return {}

    active = (Client.is_deleted.is_(False)) | (Client.is_deleted.is_(None))

    by_name: dict[str, int] = {}
    variants_by_applicant: dict[str, set[str]] = {}
    all_variants: set[str] = set()
    for nm in applicant_names:
        vars_ = set(_client_name_variants(nm))
        if not vars_:
            continue
        variants_by_applicant[nm] = vars_
        all_variants.update(vars_)

    if not all_variants:
        return {}

    rows = (
        Client.query.filter(active)
        .filter(func.trim(Client.name).in_(sorted(all_variants)))
        .with_entities(Client.id, Client.name)
        .all()
    )
    ids_by_variant: dict[str, list[int]] = {}
    for cid, cname in rows:
        v = _collapse_spaces(str(cname or "").strip())
        if not v:
            continue
        ids_by_variant.setdefault(v, []).append(int(cid))

    for nm, vars_ in variants_by_applicant.items():
        candidate_ids: set[int] = set()
        for v in vars_:
            for cid in ids_by_variant.get(v, []):
                candidate_ids.add(int(cid))
        if len(candidate_ids) == 1:
            by_name[nm] = next(iter(candidate_ids))

    return by_name


def _load_application_applicant_names(
    *,
    matter: Matter,
    overview: VMatterOverview | None,
) -> list[str]:
    """
    Prefer the official application-form applicant when a USPTO application document has
    populated `application_applicant_name`. Older intake/applicant party rows can
    otherwise keep showing a pre-filing client name after filing.
    """
    mid = str(getattr(matter, "matter_id", "") or "").strip()
    if not mid:
        return []

    try:
        rows = (
            MatterCustomField.query.with_entities(
                MatterCustomField.namespace,
                MatterCustomField.data,
            )
            .filter(MatterCustomField.matter_id == mid)
            .all()
        )
    except SQLAlchemyError as exc:
        _rollback_session()
        report_swallowed_exception(
            exc,
            context="case.detail_context._load_application_applicant_names",
            log_key="case.detail_context.application_applicant_names",
            log_window_seconds=300,
        )
        return []

    data_by_ns: dict[str, dict] = {}
    for namespace, data in rows:
        ns = str(namespace or "").strip()
        if ns and isinstance(data, dict):
            data_by_ns[ns] = data

    if not data_by_ns:
        return []

    preferred_ns = ""
    try:
        preferred_ns = CaseParameterService.get_namespace(
            getattr(matter, "right_group", None) or getattr(overview, "right_group", None) or "",
            getattr(matter, "matter_type", None) or getattr(overview, "matter_type", None) or "",
        )
    except Exception:
        preferred_ns = ""

    namespace_order = [
        preferred_ns,
        "domestic_patent",
        "domestic_design",
        "domestic_trademark",
        "pct",
        "incoming_patent",
        "incoming_design",
        "incoming_trademark",
        "outgoing_patent",
        "outgoing_design",
        "outgoing_trademark",
        "litigation",
        "misc",
    ]
    seen_ns: set[str] = set()
    for ns in namespace_order + sorted(data_by_ns.keys()):
        ns = (ns or "").strip()
        if not ns or ns in seen_ns:
            continue
        seen_ns.add(ns)
        raw = str((data_by_ns.get(ns) or {}).get("application_applicant_name") or "").strip()
        names = _split_party_names(raw)
        if names:
            return names

    return []


def build_case_detail_context(
    case_id: int,
    request_args: dict,
    current_user,
    *,
    view_mode: str = "page",
) -> dict:
    mid_str = str(case_id)
    ctx = {}
    mode = str(view_mode or "page").strip().lower() or "page"

    # 1. Base (Matter, Overview, Basic Data, Staff, Identifiers)
    ctx.update(_build_base(mid_str, request_args, current_user))

    if mode == "history_panel":
        ctx.update(_build_history_panel_context(ctx))
        return ctx

    if mode == "files_panel":
        ctx.update(_build_file_manager_section(ctx, request_args))
        return ctx

    if mode == "deadlines_panel":
        ctx.update(_build_deadlines_panel_context(ctx))
        return ctx

    if mode == "memo_panel":
        ctx.update(_build_memo_panel_context(ctx))
        return ctx

    if mode == "cost_panel":
        ctx.update(_build_costs_section(ctx))
        return ctx

    if mode == "annuity_panel":
        ctx.update(
            _build_history_section(
                ctx,
                include_history_details=False,
                include_memos=False,
                include_annuity=True,
            )
        )
        return ctx

    if mode == "alarm_panel":
        ctx.update(_build_alarm_section(ctx))
        return ctx

    # 2. Auto Status (Calculation & Update)
    ctx.update(_build_auto_status_section(ctx))

    # 3. Activity (Docket, Annuity, Memos, Workflows, DUE count)
    # History rows are lazy-loaded via HTMX, so keep the initial page render light.
    ctx.update(
        _build_history_section(
            ctx,
            include_history_details=False,
            include_memos=False,
            include_annuity=False,
        )
    )

    # 4. Family (Related Matters, Priority Rows)
    ctx.update(_build_family_section(ctx))

    # 5. Specific Fields (Case Type specific logic & Custom Text Data)
    ctx.update(_build_specific_fields_section(ctx))

    # 6. Costs (Invoices, Expenses)
    # Full finance rows are lazy-loaded; keep only summary/actions for the sticky header.
    ctx.update(_build_costs_section(ctx, summary_only=True))
    ctx.update(_build_case_view_display_context(ctx))

    # 7. File Manager (Split View)
    ctx.update(_build_file_manager_section(ctx, request_args, counts_only=True))

    # 8. Audit Log
    ctx.update(_build_audit_section(ctx))

    # 9. Alarm/Notification History
    ctx.update(_build_alarm_section(ctx, counts_only=True))

    return ctx


def _resolve_case_primary_custom_data(ctx: dict, *, is_pct: bool = False) -> dict:
    candidates = (
        ("is_domestic_patent", "dom_patent"),
        ("is_domestic_design", "dom_design"),
        ("is_domestic_trademark", "dom_trademark"),
        ("is_incoming_patent", "inc_patent"),
        ("is_incoming_design", "inc_design"),
        ("is_incoming_trademark", "inc_trademark"),
        ("is_outgoing_patent", "out_patent"),
        ("is_outgoing_design", "out_design"),
        ("is_outgoing_trademark", "out_trademark"),
        ("is_pct", "pct"),
        ("is_litigation", "litigation"),
        ("is_misc", "misc"),
    )
    for flag_key, data_key in candidates:
        flag_enabled = bool(ctx.get(flag_key))
        if flag_key == "is_pct":
            flag_enabled = flag_enabled or is_pct
        if not flag_enabled:
            continue
        data = ctx.get(data_key)
        if isinstance(data, dict):
            return data
        return {}
    return {}


def _build_case_view_display_context(ctx: dict) -> dict:
    matter = ctx["matter"]
    overview = ctx.get("overview")

    matter_type_upper = (getattr(matter, "matter_type", "") or "").strip().upper()
    right_group_upper = (getattr(matter, "right_group", "") or "").strip().upper()
    is_pct = bool(ctx.get("is_pct")) or matter_type_upper == "PCT"
    custom_data = _resolve_case_primary_custom_data(ctx, is_pct=is_pct)

    is_utility = matter_type_upper == "UTILITY"
    patent_label = "Utility" if is_utility else "Patent"
    is_copyright = bool(ctx.get("is_copyright"))
    public_division, public_type = resolve_public_case_kind(
        getattr(matter, "right_group", None),
        getattr(matter, "matter_type", None),
        is_madrid=bool(ctx.get("is_madrid")),
        is_hague=bool(ctx.get("is_hague")),
        is_copyright=is_copyright,
    )

    badge_label = f"{getattr(matter, 'right_group', '') or ''} {getattr(matter, 'matter_type', '') or ''}".strip()
    badge_class = "badge bg-secondary"

    if ctx.get("is_domestic_patent"):
        badge_label = f"US · {patent_label}"
        badge_class = "badge bg-secondary"
    elif ctx.get("is_domestic_design"):
        badge_label = "US · Design"
        badge_class = "badge bg-secondary"
    elif ctx.get("is_domestic_trademark"):
        badge_label = "US · Trademark"
        badge_class = "badge bg-secondary"
    elif ctx.get("is_incoming_patent"):
        badge_label = f"Inbound US · {patent_label}"
        badge_class = "badge border border-primary text-primary bg-transparent"
    elif ctx.get("is_incoming_design"):
        badge_label = "Inbound US · Design"
        badge_class = "badge border border-primary text-primary bg-transparent"
    elif ctx.get("is_incoming_trademark"):
        badge_label = "Inbound US · Trademark"
        badge_class = "badge border border-primary text-primary bg-transparent"
    elif ctx.get("is_outgoing_patent"):
        badge_label = f"Foreign · {patent_label}"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif ctx.get("is_outgoing_design") and ctx.get("is_hague"):
        badge_label = "Hague · Design"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif ctx.get("is_outgoing_design"):
        badge_label = "Foreign · Design"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif ctx.get("is_outgoing_trademark") and ctx.get("is_madrid"):
        badge_label = "Madrid · Trademark"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif ctx.get("is_outgoing_trademark"):
        badge_label = "Foreign · Trademark"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif right_group_upper == "OUT" and matter_type_upper == "TRADEMARK":
        badge_label = "Foreign · Trademark"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif is_pct:
        badge_label = "PCT"
        badge_class = "badge border border-dark text-dark bg-transparent"
    elif ctx.get("is_litigation"):
        badge_label = "Proceedings / Litigation"
        badge_class = "badge bg-danger"
    elif is_copyright:
        badge_label = "Copyright"
        badge_class = "badge bg-warning text-dark"
    elif ctx.get("is_misc"):
        badge_label = "Other"
        badge_class = "badge bg-secondary"
    elif right_group_upper in {"OUT", "INC"}:
        badge_class = "badge border border-secondary text-secondary bg-transparent"

    case_title = (
        (custom_data.get("proposal_title") or "").strip()
        or (custom_data.get("title") or "").strip()
        or (getattr(matter, "right_name", "") or "").strip()
        or (getattr(overview, "right_name", "") or "").strip()
    )
    applicant_names = ctx.get("applicant_names") or []
    if not applicant_names:
        applicant_names = _split_party_names(
            (custom_data.get("application_applicant_name") or "").strip()
            or (custom_data.get("applicant_name") or "").strip()
            or (getattr(overview, "applicants", "") if overview else "")
            or ""
        )

    app_no = (
        (ctx.get("app_no") or "").strip()
        or (custom_data.get("application_no") or "").strip()
        or (custom_data.get("app_no") or "").strip()
    )
    reg_no = (
        (ctx.get("reg_no") or "").strip()
        or (custom_data.get("registration_no") or "").strip()
        or (custom_data.get("reg_no") or "").strip()
    )
    pub_no = (
        (ctx.get("pub_no") or "").strip()
        or (custom_data.get("publication_no") or "").strip()
        or (custom_data.get("pub_no") or "").strip()
    )

    next_docket = ctx.get("next_docket")
    if next_docket is None:
        docket_due = ctx.get("docket_due") or []
        next_docket = docket_due[0] if docket_due else None

    finance_summary = (
        ctx.get("case_finance_summary") if isinstance(ctx.get("case_finance_summary"), dict) else {}
    )

    return {
        "_custom_data": custom_data,
        "_badge_label": badge_label,
        "_badge_class": badge_class,
        "case_title": case_title,
        "case_division": public_division or (getattr(matter, "right_group", "") or ""),
        "case_type": public_type or (getattr(matter, "matter_type", "") or ""),
        "case_division_raw": getattr(matter, "right_group", "") or "",
        "case_type_raw": getattr(matter, "matter_type", "") or "",
        "case_status": getattr(matter, "inhouse_status", "") or "",
        "applicant_names": applicant_names,
        "applicant_client_ids": _resolve_applicant_client_ids(applicant_names),
        "app_no": app_no,
        "reg_no": reg_no,
        "pub_no": pub_no,
        "_next_docket": next_docket,
        "_invoice_summary": finance_summary.get("ar") or {},
    }


def _build_alarm_section(ctx: dict, *, counts_only: bool = False) -> dict:
    """
    Case-scoped alarm/notification history.

    Currently includes:
    - Deadline/annuity notification send logs (notification_log)
    """

    mid_str = ctx.get("_mid_str") or ""
    if not mid_str:
        return {
            "alarm_deadline_logs": [],
            "alarm_deadline_total_count": 0,
            "alarm_total_count": 0,
        }

    # Keep these lists small to avoid ballooning case detail payloads.
    limit = 200

    deadline_logs: list[dict] = []
    deadline_total_count = 0

    # --- Deadline/annuity notification logs (best-effort) ---
    try:
        from app.models.notification import NotificationLog

        docket_total_count = (
            db.session.query(func.count(NotificationLog.id))
            .join(DocketItem, NotificationLog.entity_id == DocketItem.docket_id)
            .filter(NotificationLog.entity_type == "docket_item")
            .filter(DocketItem.matter_id == mid_str)
            .scalar()
            or 0
        )

        if not counts_only:
            # Docket notifications
            docket_rows = (
                db.session.query(NotificationLog, DocketItem)
                .join(DocketItem, NotificationLog.entity_id == DocketItem.docket_id)
                .filter(NotificationLog.entity_type == "docket_item")
                .filter(DocketItem.matter_id == mid_str)
                .order_by(NotificationLog.sent_at.desc())
                .limit(limit)
                .all()
            )
            for log, d in docket_rows:
                deadline_logs.append(
                    {
                        "entity_type": "docket_item",
                        "entity_id": getattr(log, "entity_id", None),
                        "channel": getattr(log, "channel", None),
                        "days_before": getattr(log, "days_before", None),
                        "status": getattr(log, "status", None),
                        "sent_at": getattr(log, "sent_at", None),
                        "recipient": getattr(log, "recipient", None),
                        "error_message": getattr(log, "error_message", None),
                        "title": (
                            getattr(d, "name_free", None) or getattr(d, "name_ref", None) or ""
                        ),
                        "due_date": (
                            getattr(log, "due_date", None)
                            or getattr(d, "extended_due_date", None)
                            or getattr(d, "due_date", None)
                            or ""
                        ),
                        "owner_staff_party_id": getattr(d, "owner_staff_party_id", None) or "",
                        "docket_id": getattr(d, "docket_id", None),
                    }
                )
        annuity_total_count = (
            db.session.query(func.count(NotificationLog.id))
            .join(AnnuityItem, NotificationLog.entity_id == AnnuityItem.annuity_id)
            .filter(NotificationLog.entity_type == "annuity_item")
            .filter(AnnuityItem.matter_id == mid_str)
            .scalar()
            or 0
        )

        if not counts_only:
            # Annuity notifications
            annuity_rows = (
                db.session.query(NotificationLog, AnnuityItem, Matter, MatterFacts)
                .join(AnnuityItem, NotificationLog.entity_id == AnnuityItem.annuity_id)
                .outerjoin(Matter, AnnuityItem.matter_id == Matter.matter_id)
                .outerjoin(MatterFacts, MatterFacts.matter_id == AnnuityItem.matter_id)
                .filter(NotificationLog.entity_type == "annuity_item")
                .filter(AnnuityItem.matter_id == mid_str)
                .order_by(NotificationLog.sent_at.desc())
                .limit(limit)
                .all()
            )
            for log, a, matter, facts in annuity_rows:
                cycle_no = getattr(a, "cycle_no", None)
                cycle_label = renewal_cycle_label(
                    cycle_no,
                    right_type=normalize_renewal_right_type(
                        getattr(facts, "right_type_norm", None),
                        getattr(matter, "matter_type", None),
                        getattr(matter, "right_group", None),
                        getattr(matter, "our_ref", None),
                    ),
                    jurisdiction=normalize_renewal_jurisdiction(
                        getattr(matter, "right_group", None),
                        getattr(matter, "matter_type", None),
                        getattr(matter, "our_ref", None),
                    ),
                )
                deadline_logs.append(
                    {
                        "entity_type": "annuity_item",
                        "entity_id": getattr(log, "entity_id", None),
                        "channel": getattr(log, "channel", None),
                        "days_before": getattr(log, "days_before", None),
                        "status": getattr(log, "status", None),
                        "sent_at": getattr(log, "sent_at", None),
                        "recipient": getattr(log, "recipient", None),
                        "error_message": getattr(log, "error_message", None),
                        "title": cycle_label,
                        "due_date": (
                            getattr(log, "due_date", None)
                            or getattr(a, "extended_due_date", None)
                            or getattr(a, "due_date", None)
                            or ""
                        ),
                        "owner_staff_party_id": getattr(a, "owner_staff_party_id", None) or "",
                        "annuity_id": getattr(a, "annuity_id", None),
                    }
                )

        # Merge (docket + annuity) and sort by sent_at desc (stable for mixed sources).
        deadline_logs.sort(
            key=lambda r: (r.get("sent_at") is not None, r.get("sent_at")), reverse=True
        )
        if len(deadline_logs) > limit:
            deadline_logs = deadline_logs[:limit]
        deadline_total_count = int(docket_total_count + annuity_total_count)

    except Exception as exc:
        if not _is_missing_table_error(exc, "notification_log"):
            report_swallowed_exception(
                exc,
                context="case.detail_context._build_alarm_section.notification_log",
                log_key="case.detail_context._build_alarm_section.notification_log",
                log_window_seconds=300,
            )
        _rollback_session()
        deadline_logs = []
        deadline_total_count = 0

    return {
        "alarm_deadline_logs": deadline_logs,
        "alarm_deadline_total_count": int(deadline_total_count),
        "alarm_total_count": int(deadline_total_count),
    }


def _build_base(mid_str: str, request_args: dict, current_user) -> dict:
    matter = Matter.query.get_or_404(mid_str)
    overview = VMatterOverview.query.get(mid_str)
    staff_lists = build_staff_assignment_lists()
    users = staff_lists.get("all_users") or []

    basic_row = MatterCustomField.query.filter_by(matter_id=mid_str, namespace="basic").first()
    basic_data = (basic_row.data or {}) if basic_row else {}

    # Identifiers (Pre-load here as they are used in multiple sections)
    id_rows = MatterIdentifier.query.filter_by(matter_id=mid_str).all()
    identifiers: dict[str, list[str]] = {}
    for r in id_rows:
        k = (r.id_type or "").strip() or "Other"
        v = (r.id_value or "").strip()
        if not v:
            continue
        identifiers.setdefault(k, [])
        if v not in identifiers[k]:
            identifiers[k].append(v)

    def _first(*keys: str) -> str:
        for k in keys:
            vals = identifiers.get(k) or []
            if vals:
                return vals[0]
        return ""

    current_assignee_id = None
    try:
        msa = MatterStaffAssignment.query.filter(
            MatterStaffAssignment.matter_id == mid_str,
            func.lower(func.trim(MatterStaffAssignment.staff_role_code)) == "attorney",
        ).first()
        if msa and msa.staff_party_id:
            user = User.query.filter_by(staff_party_id=msa.staff_party_id).first()
            if user:
                current_assignee_id = user.id
    except SQLAlchemyError as e:
        _rollback_session()
        current_app.logger.error(f"Error in assignee lookup: {e}")
        current_assignee_id = None

    applicant_names = _load_application_applicant_names(
        matter=matter,
        overview=overview,
    ) or _split_party_names((overview.applicants if overview else "") or "")

    # Permission flags for the case view template (UI gating; server-side checks still apply).
    can_edit_case = False
    can_assign_staff = False
    can_delete_case = False
    can_invoice = False
    try:
        from app.utils.permissions import can_access_matter

        can_edit_case = can_access_matter(current_user, mid_str, action="edit_case")
        can_assign_staff = can_access_matter(current_user, mid_str, action="assign_staff")
        can_delete_case = can_access_matter(current_user, mid_str, action="delete_case")
        can_invoice = can_access_matter(current_user, mid_str, action="invoice")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_context._build_base.can_access_matter",
            log_key="case.detail_context._build_base.can_access_matter",
            log_window_seconds=300,
        )
    return {
        "matter": matter,
        "overview": overview,
        "staff_lists": staff_lists,  # Internal use
        "basic_data": basic_data,  # Internal use
        "applicant_names": applicant_names,
        "applicant_client_ids": _resolve_applicant_client_ids(applicant_names),
        "identifiers": identifiers,
        "app_no": _first("Application No.", "APP_NO", "application_no", "app_no"),
        "reg_no": _first("Registration No.", "REG_NO", "registration_no", "reg_no"),
        "pub_no": _first("Publication No.", "PUB_NO", "publication_no", "pub_no"),
        "users": users,
        "staff_users_all": staff_lists.get("all_users") or [],
        "staff_users_management": staff_lists.get("management_users") or [],
        "staff_users_professional": staff_lists.get("professional_users") or [],
        "staff_users_attorney": staff_lists.get("attorney_users")
        or staff_lists.get("professional_users")
        or [],
        "staff_users_processing": staff_lists.get("processing_users")
        or staff_lists.get("all_users")
        or [],
        "current_assignee_id": current_assignee_id,
        "can_edit_case": can_edit_case,
        "can_assign_staff": can_assign_staff,
        "can_delete_case": can_delete_case,
        "can_invoice": can_invoice,
        "today": date.today().isoformat(),
        # Store mid_str in ctx for convenience
        "_mid_str": mid_str,
        "_current_user": current_user,
    }


def _build_auto_status_section(ctx: dict) -> dict:
    matter = ctx["matter"]
    overview = ctx["overview"]
    mid_str = ctx["_mid_str"]

    auto_status = None
    try:
        # IMPORTANT (P1): never mutate DB on safe methods (GET/HEAD/OPTIONS).
        # Case detail is a GET endpoint protected by "view" permission; any self-heal must not
        # allow state mutation on view-only access.
        allow_db_write = False
        try:
            allow_db_write = bool(
                has_request_context()
                and (request.method or "GET").upper() not in {"GET", "HEAD", "OPTIONS"}
            )
        except RuntimeError:
            allow_db_write = False

        if allow_db_write:
            try:
                from flask_login import current_user as _login_user

                from app.utils.permissions import can_access_matter

                allow_db_write = can_access_matter(_login_user, mid_str, action="edit_case")
            except (ImportError, RuntimeError, SQLAlchemyError):
                allow_db_write = False

        events_changed = False

        old_snapshot = None
        if allow_db_write:
            # Change  Statusvalue(Auto self-heal )
            old_snapshot = {
                "status_red": (matter.status_red or ""),
                "status_red_related_date": (matter.status_red_related_date or ""),
                "status_blue": (matter.status_blue or ""),
            }

        if allow_db_write:
            # Backfill matter_event from custom fields when missing (imported data often lacks rows).
            if not _has_any_matter_events(mid_str):
                events_changed = _maybe_backfill_matter_events_from_custom_fields(
                    mid_str, matter=matter, overview=overview
                )

            # If allowance/rejection events are missing, backfill from office_action
            # notice dates, preferring notified_date over received_date.
            events_changed = (
                _maybe_backfill_matter_events_from_office_actions(mid_str) or events_changed
            )

            if events_changed:
                try:
                    db.session.flush()
                except SQLAlchemyError:
                    _rollback_session()

        cur_red_for_calc = (
            matter.status_red or (overview.next_due_name if overview else "") or ""
        ).strip()
        if cur_red_for_calc and is_known_deadline_red_label(cur_red_for_calc):
            try:
                if not has_supporting_red_signal(matter_id=mid_str, red_label=cur_red_for_calc):
                    cur_red_for_calc = ""
            except SQLAlchemyError:
                _rollback_session()

        cur_red_date_for_calc = (
            matter.status_red_related_date or (overview.next_due_date if overview else "") or ""
        ).strip()

        if allow_db_write:
            sync_result = apply_auto_status_cache_to_matter(
                matter=matter,
                current_red=cur_red_for_calc,
                current_red_date=cur_red_date_for_calc,
                current_blue=(matter.status_blue or "").strip(),
                memo=(matter.memo or "").strip(),
            )
            auto_status = sync_result.auto_status
            status_cache_changed = sync_result.changed
        else:
            auto_status = derive_auto_status(
                matter_id=mid_str,
                current_red=cur_red_for_calc,
                current_red_date=cur_red_date_for_calc,
                current_blue=(matter.status_blue or "").strip(),
                memo=(matter.memo or "").strip(),
            )
            status_cache_changed = False

        # Self-healing: if derived status differs from DB, update DB to match.
        # This ensures the list view (which reads raw DB) stays in sync.
        if allow_db_write and auto_status:
            # Keep inhouse_status as a manual/user-driven status override.
            # Self-heal only synchronizes derived auto-status fields (red/date/blue).

            if (status_cache_changed or events_changed) and auto_status is not None:
                try:
                    # SYSTEM Auto Change (=SYSTEM, =detail_context self-heal)
                    # NOTE: actor_user_id is best-effort; for system-driven healing it's acceptable
                    # to be None, but when invoked inside an authenticated request we try to capture it.
                    actor_user_id = None
                    try:
                        from flask_login import current_user as _login_user

                        actor_user_id = getattr(_login_user, "id", None)
                    except RuntimeError:
                        actor_user_id = None
                    new_snapshot = {
                        "status_red": (matter.status_red or ""),
                        "status_red_related_date": (matter.status_red_related_date or ""),
                        "status_blue": (matter.status_blue or ""),
                    }
                    if new_snapshot != old_snapshot or events_changed:
                        db.session.add(
                            CaseAuditLog(
                                case_id=str(mid_str),
                                actor_user_id=actor_user_id,
                                action="SYSTEM",
                                field_name="auto_status.self_heal",
                                old_value=old_snapshot,
                                new_value={
                                    "snapshot": new_snapshot,
                                    "events_changed": bool(events_changed),
                                },
                                request_id=getattr(g, "request_id", None),
                                created_at=datetime.utcnow(),
                            )
                        )
                    db.session.commit()
                except SQLAlchemyError:
                    _rollback_session()
                    current_app.logger.warning(
                        "Failed to self-heal matter status (mid=%s)", mid_str
                    )

    except Exception:
        _rollback_session()
        auto_status = None

    return {"auto_status": auto_status}


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


def _has_any_matter_events(matter_id: str) -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False
    try:
        row = db.session.execute(
            text("""
                SELECT 1
                FROM matter_event
                WHERE matter_id = :mid
                LIMIT 1
                """).execution_options(policy_bypass=True),
            {"mid": mid},
        ).first()
        return bool(row)
    except Exception:
        _rollback_session()
        return False


def _pick_event_sync_payload(
    *,
    matter: Matter,
    overview: VMatterOverview | None,
    data_by_ns: dict[str, dict],
) -> tuple[str | None, dict | None]:
    if not data_by_ns:
        return None, None
    if len(data_by_ns) == 1:
        ns = next(iter(data_by_ns))
        return ns, data_by_ns[ns]

    ns_hint = None
    try:
        div, typ = _infer_case_kind(matter, overview)
        if div == "DOM" and typ in PATENT_LIKE_TYPES:
            ns_hint = "domestic_patent"
        elif div == "DOM" and typ == "DESIGN":
            ns_hint = "domestic_design"
        elif div == "DOM" and typ == "TRADEMARK":
            ns_hint = "domestic_trademark"
        elif div == "INC" and typ in PATENT_LIKE_TYPES:
            ns_hint = "incoming_patent"
        elif div == "INC" and typ == "DESIGN":
            ns_hint = "incoming_design"
        elif div == "INC" and typ == "TRADEMARK":
            ns_hint = "incoming_trademark"
        elif div == "OUT" and typ in PATENT_LIKE_TYPES:
            ns_hint = "outgoing_patent"
        elif div == "OUT" and typ == "DESIGN":
            ns_hint = "outgoing_design"
        elif div == "OUT" and typ == "TRADEMARK":
            ns_hint = "outgoing_trademark"
        elif typ == "PCT":
            ns_hint = "pct"
        elif typ == "LITIGATION":
            ns_hint = "litigation"
    except Exception:
        ns_hint = None

    if ns_hint and ns_hint in data_by_ns:
        return ns_hint, data_by_ns[ns_hint]

    for ns in _EVENT_SYNC_NAMESPACE_ORDER:
        if ns in data_by_ns:
            return ns, data_by_ns[ns]

    return None, None


def _maybe_backfill_matter_events_from_custom_fields(
    matter_id: str, *, matter: Matter, overview: VMatterOverview | None
) -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False

    if _has_any_matter_events(mid):
        return False

    try:
        rows = (
            MatterCustomField.query.filter_by(matter_id=mid)
            .filter(MatterCustomField.namespace.in_(_EVENT_SYNC_NAMESPACE_ORDER))
            .all()
        )
    except SQLAlchemyError:
        _rollback_session()
        return False

    data_by_ns: dict[str, dict] = {}
    for row in rows or []:
        ns = (getattr(row, "namespace", "") or "").strip()
        if not ns:
            continue
        data = row.data
        if isinstance(data, dict) and data:
            data_by_ns[ns] = data

    ns, payload = _pick_event_sync_payload(matter=matter, overview=overview, data_by_ns=data_by_ns)
    if not (ns and isinstance(payload, dict)):
        return False

    sync_map: dict[str, tuple[callable, str]] = {
        "domestic_patent": (_sync_matter_events_from_dom_patent, "dom_patent"),
        "domestic_design": (_sync_matter_events_from_dom_design, "dom_design"),
        "domestic_trademark": (_sync_matter_events_from_dom_trademark, "dom_trademark"),
        "incoming_patent": (_sync_matter_events_from_inc_patent, "inc_patent"),
        "incoming_design": (_sync_matter_events_from_inc_design, "inc_design"),
        "incoming_trademark": (_sync_matter_events_from_inc_trademark, "inc_tm"),
        "outgoing_patent": (_sync_matter_events_from_out_patent, "out_patent"),
        "outgoing_design": (_sync_matter_events_from_out_design, "out_design"),
        "outgoing_trademark": (_sync_matter_events_from_out_trademark, "out_tm"),
        "pct": (_sync_matter_events_from_pct, "pct"),
        "litigation": (_sync_matter_events_from_litigation, "litigation"),
    }

    fn_entry = sync_map.get(ns)
    if not fn_entry:
        return False
    fn, arg_name = fn_entry
    try:
        fn(matter_id=mid, **{arg_name: payload})
    except (SQLAlchemyError, TypeError, ValueError, KeyError):
        _rollback_session()
        return False

    return _has_any_matter_events(mid)


def _maybe_backfill_matter_events_from_office_actions(matter_id: str) -> bool:
    mid = (matter_id or "").strip()
    if not mid:
        return False

    try:
        rows = db.session.execute(
            text("""
                SELECT event_key
                FROM matter_event
                WHERE matter_id = :mid
                """).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
        existing_keys = {str(r[0]).strip() for r in rows if r and r[0]}
    except SQLAlchemyError:
        _rollback_session()
        return False

    allowance_keys = {
        "ALLOWANCE_RECEIVED_DATE",
        "ALLOWANCE_DATE",
        "Notice of allowance Upload",
        "Notice of allowance ",
        "Notice of allowance",
    }
    rejection_keys = {
        "REJECTION_RECEIVED_DATE",
        "REJECTION_DATE",
        "Final rejection Upload",
        "Final rejection ",
        "",
    }

    needs_allowance = not (allowance_keys & existing_keys)
    needs_rejection = not (rejection_keys & existing_keys)
    if not (needs_allowance or needs_rejection):
        return False

    try:
        oa_rows = db.session.execute(
            text("""
                SELECT doc_name, notified_date, received_date
                FROM office_action
                WHERE matter_id = :mid
                  AND (
                    (notified_date IS NOT NULL AND TRIM(notified_date) <> '')
                    OR (received_date IS NOT NULL AND TRIM(received_date) <> '')
                  )
                """).execution_options(policy_bypass=True),
            {"mid": mid},
        ).all()
    except SQLAlchemyError:
        _rollback_session()
        return False

    if not oa_rows:
        return False

    allowance_terms = ("Notice of allowance", "Patent", "SettingsRegistration", "Registration", "PatentPayment", "RegistrationPayment")
    rejection_terms = (
        "",
        "Patent",
        "Utility model",
        "Design",
        "Trademark",
    )

    candidates: dict[str, str] = {}
    for doc_name, notified_date, received_date in oa_rows:
        doc = (doc_name or "").strip()
        notified_dt = _date_only_str(notified_date)
        received_dt = _date_only_str(received_date)
        if not (doc and (notified_dt or received_dt)):
            continue
        if needs_allowance and any(term in doc for term in allowance_terms):
            event_key = "ALLOWANCE_DATE" if notified_dt else "ALLOWANCE_RECEIVED_DATE"
            dt = notified_dt or received_dt
            prev = candidates.get(event_key)
            if not prev or dt < prev:
                candidates[event_key] = dt
        if needs_rejection and any(term in doc for term in rejection_terms):
            event_key = "REJECTION_DATE" if notified_dt else "REJECTION_RECEIVED_DATE"
            dt = notified_dt or received_dt
            prev = candidates.get(event_key)
            if not prev or dt < prev:
                candidates[event_key] = dt

    if not candidates:
        return False

    inserted = False
    for ek, dt in candidates.items():
        if ek in existing_keys:
            continue
        try:
            db.session.execute(
                text("""
                    INSERT INTO matter_event (mevent_id, matter_id, event_key, event_at, source_column)
                    VALUES (:id, :mid, :ek, :at, :src)
                    """).execution_options(policy_bypass=True),
                {
                    "id": uuid.uuid4().hex,
                    "mid": mid,
                    "ek": ek,
                    "at": dt,
                    "src": "office_action:auto",
                },
            )
            inserted = True
            existing_keys.add(ek)
        except SQLAlchemyError:
            _rollback_session()
            return False

    return inserted


@lru_cache(maxsize=1)
def _load_ledger_hidden_keys() -> set[str]:
    keys: set[str] = set()
    try:
        from app.services.case_fields.unified_config import load_unified_registry_data

        data, _meta = load_unified_registry_data()
        extra = (data or {}).get("ledger_only_fields") or []
        if isinstance(extra, list):
            for k in extra:
                if k:
                    keys.add(str(k))
        # Keep high-signal document-derived fields visible in the default case view.
        keys.discard("application_applicant_name")
        keys.discard("application_applicant_customer_no")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_context._load_ledger_hidden_keys",
            log_key="case.detail_context._load_ledger_hidden_keys",
            log_window_seconds=300,
        )

    return keys


def _matter_rows_with_params(mid_str: str, sql: str, params: dict | None = None) -> list[dict]:
    merged = {"mid": mid_str}
    merged.update(params or {})
    return [
        dict(r._mapping)
        for r in db.session.execute(text(sql).execution_options(policy_bypass=True), merged).all()
    ]


def _build_history_section(
    ctx: dict,
    *,
    include_history_details: bool = True,
    include_memos: bool = True,
    include_annuity: bool = True,
) -> dict:
    matter = ctx["matter"]
    overview = ctx["overview"]
    mid_str = ctx["_mid_str"]

    history_limit = _detail_int_cfg("CASE_DETAIL_HISTORY_LIMIT", 200, min_v=20, max_v=2000)
    due_limit = _detail_int_cfg("CASE_DETAIL_DUE_LIMIT", 200, min_v=20, max_v=2000)

    # USPTO due-date computation is only safe for USPTO-managed matters.
    normalized_div = _normalize_case_division(matter.right_group) or _normalize_case_division(
        overview.right_group if overview else ""
    )
    is_uspto = is_uspto_managed_matter(matter, overview)
    if not is_uspto and not normalized_div:
        # Fallback: infer from our_ref pattern (YY + (Type)(Division) + ... + Country)
        our_ref = (
            ((matter.our_ref or "") or (overview.our_ref if overview else "") or "").strip().upper()
        )
        if len(our_ref) >= 4 and our_ref[:2].isdigit():
            code = our_ref[2:4]
            if len(code) == 2 and code[1:2] in ("D", "I"):
                is_uspto = True
        if not is_uspto and our_ref.endswith("US"):
            is_uspto = True

    effective_due = effective_due_text_expr(
        DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
    )
    docket_q = DocketItem.query.filter_by(matter_id=mid_str)
    if hasattr(DocketItem, "is_deleted"):
        docket_q = docket_q.filter(
            or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None))
        )
    docket_items = docket_q.order_by(effective_due.asc(), DocketItem.docket_id.asc()).all()
    docket_open = [d for d in docket_items if not (d.done_date or "").strip()]
    docket_done = [d for d in docket_items if (d.done_date or "").strip()]
    docket_due = [d for d in docket_open if is_visible_by_date(d)]
    docket_scheduled = [d for d in docket_open if not is_visible_by_date(d)]
    next_docket = None
    next_docket_due = None
    for d in docket_due:
        eff_due = effective_due_for_work(
            getattr(d, "due_date", None),
            getattr(d, "extended_due_date", None),
        )
        if not eff_due:
            continue
        eff_due_str = eff_due.isoformat()
        if next_docket_due is None or eff_due_str < next_docket_due:
            next_docket_due = eff_due_str
            next_docket = d
    docket_history = docket_items
    notice_send_semi_close_prompt = None

    annuity_management_disabled = False
    annuity_items = []
    annuity_default_visible_cycle_nos: list[str] = []
    annuity_autogen_hint: dict = {"kind": "generic"}
    if include_annuity:
        from app.services.annuity.annuity_management import (
            is_annuity_management_disabled_for_matter,
        )

        annuity_management_disabled = is_annuity_management_disabled_for_matter(mid_str)

        if not annuity_management_disabled:
            annuity_items = (
                AnnuityItem.query.filter_by(matter_id=mid_str)
                .filter(or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None)))
                .order_by(AnnuityItem.cycle_no.asc(), AnnuityItem.due_date.asc())
                .all()
            )
            if not annuity_items:
                annuity_autogen_hint = _build_annuity_empty_hint(matter, mid_str)
        # Default visibility: show "next annuity" + limited look-ahead to reduce clutter.
        # Keep full rows available via "Show all".
        try:
            from app.services.annuity.annuity_policy import (
                compute_status,
                effective_due_date_str,
                parse_date,
            )
            from app.services.annuity.annuity_visibility import get_visible_cycle_count

            visible_n = get_visible_cycle_count()

            today = date.today()
            open_rows: list[tuple[date, int]] = []
            anchor_cycle: int | None = None

            for ai in annuity_items or []:
                st = compute_status(ai, today=today)
                eff_str = effective_due_date_str(ai) or ""
                eff_due = parse_date(eff_str) if eff_str else None

                # Build display label/badge for case view.
                due_dt = parse_date(getattr(ai, "due_date", None))
                ext_dt = parse_date(getattr(ai, "extended_due_date", None))
                internal_dt = parse_date(getattr(ai, "internal_due_date", None))
                legal_dt = due_dt or ext_dt

                label = st
                badge_class = "bg-primary"
                if st == "paid":
                    label = "Paid"
                    badge_class = "bg-success"
                elif st == "giveup":
                    label = "Abandoned"
                    badge_class = "bg-secondary"
                elif st == "overdue":
                    # Distinguish internal overdue vs legal overdue to avoid confusion.
                    if (
                        internal_dt
                        and legal_dt
                        and internal_dt < legal_dt
                        and internal_dt < today
                        and today <= legal_dt
                    ):
                        label = "Internal deadline "
                        badge_class = "bg-warning text-dark"
                    elif due_dt and ext_dt and due_dt < today and today <= ext_dt:
                        # Grace period: normal due passed, but still legally payable with surcharge.
                        label = "Period"
                        badge_class = "bg-warning text-dark"
                    else:
                        label = "overdue"
                        badge_class = "bg-danger"
                else:
                    # pending
                    if not eff_str:
                        label = "None"
                        badge_class = "bg-secondary"
                    elif due_dt and ext_dt and due_dt < today and today <= ext_dt:
                        label = "Period"
                        badge_class = "bg-warning text-dark"
                    else:
                        label = "In Progress"
                        badge_class = "bg-primary"

                # Jinja-friendly computed fields (fallback when DB fields are blank).
                ai._computed_status = st
                ai._computed_status_label = label
                ai._computed_badge_class = badge_class
                ai._effective_due_date = eff_str

                if st not in ("pending", "overdue"):
                    continue
                if not eff_due:
                    continue
                cycle_no = coerce_int(getattr(ai, "cycle_no", None), 0) or 0
                if cycle_no <= 0:
                    continue
                open_rows.append((eff_due, cycle_no))

            if open_rows:
                # "Next annuity" == the earliest upcoming effective due date (>= today).
                upcoming = [t for t in open_rows if t[0] >= today]
                visible: list[tuple[date, int]] = []
                if upcoming:
                    upcoming.sort(key=lambda t: (t[0], t[1]))
                    anchor_cycle = upcoming[0][1]
                    open_by_cycle = sorted(open_rows, key=lambda t: t[1])
                    visible = [t for t in open_by_cycle if t[1] >= anchor_cycle][:visible_n]
                else:
                    # No upcoming items: show most-recent overdue items.
                    visible = sorted(open_rows, key=lambda t: (t[0], t[1]), reverse=True)[
                        :visible_n
                    ]
                annuity_default_visible_cycle_nos = [str(cycle_no) for _, cycle_no in visible]

            # Heuristic: when a "next" annuity exists, earlier overdue rows are often
            # legacy/missing payment info rather than truly actionable overdue items.
            # Avoid showing them as red "overdue"; users can still open "Show all".
            if anchor_cycle and anchor_cycle > 0:
                for ai in annuity_items or []:
                    cycle_no = coerce_int(getattr(ai, "cycle_no", None), 0) or 0
                    if cycle_no <= 0:
                        continue
                    if cycle_no >= int(anchor_cycle):
                        continue
                    st = str(getattr(ai, "_computed_status", "") or "")
                    if st != "overdue":
                        continue
                    ai._computed_status_label = "Confirm()"
                    ai._computed_badge_class = "bg-secondary"
        except Exception:
            # Be resilient: annuity section should not break case view rendering.
            annuity_default_visible_cycle_nos = []

    memos = []
    if include_memos:
        memos = (
            MatterMemo.query.filter_by(matter_id=mid_str)
            .options(joinedload(MatterMemo.created_by))
            .order_by(MatterMemo.created_at.desc(), MatterMemo.id.desc())
            .all()
        )

        memo_attachments = {}
        memo_ids = [m.id for m in memos if m.id]
        if memo_ids:
            try:
                rows = (
                    db.session.query(MatterMemoFileAsset, FileAsset)
                    .join(FileAsset, FileAsset.file_asset_id == MatterMemoFileAsset.file_asset_id)
                    .filter(MatterMemoFileAsset.memo_id.in_(memo_ids))
                    .order_by(
                        MatterMemoFileAsset.created_at.asc(),
                        MatterMemoFileAsset.memo_file_id.asc(),
                    )
                    .all()
                )
                for mmfa, fa in rows:
                    memo_attachments.setdefault(mmfa.memo_id, []).append(
                        {
                            "memo_file_id": mmfa.memo_file_id,
                            "file_asset_id": fa.file_asset_id,
                            "original_name": fa.original_name,
                            "byte_size": fa.byte_size,
                            "mime_type": fa.mime_type,
                            "created_at": mmfa.created_at,
                        }
                    )
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error loading memo attachments: {e}")
                memo_attachments = {}

        for memo in memos:
            memo.attachments = memo_attachments.get(memo.id, [])

    workflows = (
        Workflow.query.filter_by(case_id=mid_str)
        # Exclude annuity (Renewal) workflows: case screen has a dedicated "Renewal " section.
        .filter(or_(Workflow.business_code.is_(None), Workflow.business_code.notlike("ANNUITY:%")))
        .options(
            noload(Workflow.matter),
            load_only(
                Workflow.id,
                Workflow.case_id,
                Workflow.name,
                Workflow.status,
                Workflow.business_code,
                Workflow.category,
                Workflow.priority,
                Workflow.request_start_date,
                Workflow.legal_due_date,
                Workflow.source_docket_due_date,
                Workflow.source_docket_legal_due_date,
                Workflow.due_date,
                Workflow.completed_date,
                Workflow.assignee_id,
                Workflow.attorney_assignee_id,
                Workflow.inspector_id,
                Workflow.created_by_id,
                Workflow.note,
                Workflow.work_hours,
                Workflow.created_at,
            ),
            joinedload(Workflow.assignee).load_only(
                User.id,
                User.username,
                User.display_name,
                User.staff_party_id,
            ),
            joinedload(Workflow.attorney_assignee).load_only(
                User.id,
                User.username,
                User.display_name,
                User.staff_party_id,
            ),
            joinedload(Workflow.inspector).load_only(
                User.id,
                User.username,
                User.display_name,
                User.staff_party_id,
            ),
            joinedload(Workflow.created_by).load_only(
                User.id,
                User.username,
                User.display_name,
                User.staff_party_id,
            ),
        )
        .order_by(Workflow.created_at.asc(), Workflow.id.asc())
        .all()
    )
    docket_map = {d.docket_id: d for d in docket_items if d.docket_id}
    # Use imported MGMT_CATEGORIES and WORK_CATEGORIES from task_classification
    staff_role_map = {}
    try:
        role_rows = db.session.execute(
            text("""
                SELECT msa.staff_party_id, msa.staff_role_code
                FROM matter_staff_assignment msa
                WHERE msa.matter_id = :mid
                  AND LOWER(TRIM(msa.staff_role_code)) IN ('attorney', 'retainer', 'handler', 'staff', 'draftsman', 'manager', 'mgmt')
                """).execution_options(policy_bypass=True),
            {"mid": mid_str},
        ).all()
        for staff_party_id, role_code in role_rows:
            r = (role_code or "").strip().lower()
            spid = str(staff_party_id) if staff_party_id is not None else ""
            if not spid:
                continue
            if r in ("manager", "mgmt"):
                staff_role_map[spid] = "mgmt"
            elif r in ("attorney", "retainer"):
                staff_role_map[spid] = "work"
            elif r in ("handler", "staff", "draftsman"):
                staff_role_map[spid] = "work"
    except Exception:
        _rollback_session()
        staff_role_map = {}

    def _classify_docket_item(
        *,
        category: str | None,
        name_ref: str | None,
        name_free: str | None,
        owner_staff_party_id: str | None = None,
    ) -> str:
        """Classify docket item using unified classify_task_type."""
        from app.utils.task_classification import classify_task_type

        # Get staff_role from staff_role_map if available
        staff_role = None
        if owner_staff_party_id:
            sr = staff_role_map.get(str(owner_staff_party_id))
            # Convert internal map values to standard role names
            if sr == "mgmt":
                staff_role = "manager"
            elif sr == "work":
                staff_role = "attorney"

        return classify_task_type(
            staff_role=staff_role,
            category=category,
            name_ref=name_ref,
            name_free=name_free,
            matter_id=mid_str,
        )

    def _workflow_category(wf: Workflow) -> str:
        """Resolve workflow category from explicit value or current assignee mix."""
        title = (getattr(wf, "name", None) or "").strip()
        biz = (getattr(wf, "business_code", None) or "").strip()
        linked_docket_item = None

        if biz.upper().startswith("DOCKET:"):
            docket_id = biz.split(":", 1)[1].strip()
            linked_docket_item = docket_map.get(docket_id)

        resolved_category = derive_workflow_category(
            case_id=mid_str,
            handler_id=getattr(wf, "assignee_id", None),
            attorney_id=getattr(wf, "attorney_assignee_id", None),
            manager_id=getattr(wf, "inspector_id", None),
            manual_category=getattr(wf, "category", None),
            hint_category=(
                getattr(linked_docket_item, "category", None)
                if linked_docket_item is not None
                else getattr(wf, "category", None)
            ),
            hint_name_ref=(
                getattr(linked_docket_item, "name_ref", None)
                if linked_docket_item is not None
                else biz
            ),
            hint_name_free=(
                getattr(linked_docket_item, "name_free", None)
                if linked_docket_item is not None
                else title
            ),
            source=_extract_task_source_from_docket_item(linked_docket_item),
        )
        normalized = normalize_workflow_category(resolved_category)
        if normalized == "MGMT_WORK":
            return "hybrid"
        if normalized == "MGMT":
            return "mgmt"
        return "work"

    # Assign category_type to docket items
    for di in docket_items:
        di.category_type = _classify_docket_item(
            category=di.category,
            name_ref=di.name_ref,
            name_free=di.name_free,
            owner_staff_party_id=di.owner_staff_party_id,
        )
        di.calendar_month_endpoint = calendar_endpoint_for_docket(
            name_ref=getattr(di, "name_ref", None),
            title=getattr(di, "name_free", None),
        )

    today_dt = date.today()
    try:
        urgent_window_days = int(current_app.config.get("WORKFLOW_URGENT_WINDOW_DAYS", 7) or 7)
    except Exception:
        urgent_window_days = 7
    urgent_window_days = max(0, min(urgent_window_days, 3650))

    for wf in workflows:
        # Linked docket id (DOCKET:<docket_id>:...)
        bc = (getattr(wf, "business_code", None) or "").strip()
        docket_id = None
        if bc.upper().startswith("DOCKET:"):
            try:
                docket_id = bc.split(":", 1)[1].strip().split(":", 1)[0].strip() or None
            except Exception:
                docket_id = None
        wf._linked_docket_id = docket_id
        di = docket_map.get(docket_id) if docket_id else None
        display = workflow_display_values(wf, linked_docket_item=di)
        wf._display_name = display.get("name")
        wf._display_legal_due_date = display.get("legal_due_date")
        wf._display_internal_due_date = display.get("internal_due_date")
        wf._display_due_date = display.get("due_date")
        wf._flow_role_codes = _resolve_case_view_flow_role_codes(
            wf=wf,
            linked_docket_item=di,
        )
        wf.category_type = _workflow_category(wf)
        # Task / workflow  Deadline , linked docket value fallback .
        wf._list_status = compute_workflow_list_status(
            status=getattr(wf, "status", None),
            due_date=_as_date_only(wf._display_due_date),
            today=today_dt,
            urgent_window_days=urgent_window_days,
        )

    workflows.sort(key=_workflow_occurrence_sort_key)

    history_total_count = 0
    due_total_count = 0
    history_rows: list[dict] = []
    history_merge_groups: list[dict] = []
    communications: list[dict] = []
    history_dataset = _build_history_dataset(
        mid_str=mid_str,
        history_limit=history_limit,
        include_details=include_history_details,
        log_context="_build_history_section",
    )
    history_total_count = history_dataset.total_count or len(history_dataset.rows)
    communications = history_dataset.communications
    if include_history_details:
        history_rows = history_dataset.rows
        history_merge_groups = _format_history_section_merge_groups(
            history_dataset.merge_group_infos
        )

    if bool(ctx.get("can_edit_case")):
        try:
            from app.services.deadlines.notice_send_semi_close import (
                get_notice_send_prompt_candidate,
                infer_notice_send_prompt_candidate_from_communications,
            )

            notice_send_semi_close_prompt = get_notice_send_prompt_candidate(docket_due)
            if not notice_send_semi_close_prompt:
                prompt_communications = communications
                if not include_history_details:
                    prompt_communications = _load_notice_send_prompt_communications(
                        mid_str=mid_str,
                        limit=min(history_limit, 50),
                    )
                notice_send_semi_close_prompt = (
                    infer_notice_send_prompt_candidate_from_communications(
                        docket_items=docket_due,
                        communications=prompt_communications,
                    )
                )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="case.detail_context.notice_send_semi_close_prompt",
                log_key="case.detail_context.notice_send_semi_close_prompt",
                log_window_seconds=300,
            )

    def _first_date(*vals: str | None) -> str:
        for v in vals:
            s = (v or "").strip()
            if s:
                return s
        return ""

    due_rows = []
    try:
        due_total_count = (
            db.session.execute(
                text("""
                    SELECT COUNT(*)
                    FROM office_action oa
                    WHERE oa.matter_id = :mid
                      AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
                      AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
                      AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
                      AND COALESCE(TRIM(oa.doc_name), '') != ''
                      AND (
                        COALESCE(TRIM(oa.due_date), '') != ''
                        OR COALESCE(TRIM(oa.extended_due_date), '') != ''
                        OR COALESCE(TRIM(oa.notified_date), '') != ''
                        OR COALESCE(TRIM(oa.received_date), '') != ''
                        OR COALESCE(TRIM(oa.done_date), '') != ''
                      )
                    """).execution_options(policy_bypass=True),
                {"mid": mid_str},
            ).scalar()
            or 0
        )

        due_rows_raw = _matter_rows_with_params(
            mid_str,
            """
            SELECT
              oa.oa_id AS id,
              oa.doc_name AS doc_name,
              oa.received_date AS received_date,
              oa.notified_date AS notified_date,
              oa.due_date AS due_date,
              oa.extended_due_date AS extended_due_date,
              oa.done_date AS done_date
            FROM office_action oa
            WHERE oa.matter_id = :mid
              AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
              AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
              AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
              AND COALESCE(TRIM(oa.doc_name), '') != ''
              AND (
                COALESCE(TRIM(oa.due_date), '') != ''
                OR COALESCE(TRIM(oa.extended_due_date), '') != ''
                OR COALESCE(TRIM(oa.notified_date), '') != ''
                OR COALESCE(TRIM(oa.received_date), '') != ''
                OR COALESCE(TRIM(oa.done_date), '') != ''
              )
            ORDER BY
              COALESCE(
                NULLIF(TRIM(oa.due_date), ''),
                NULLIF(TRIM(oa.extended_due_date), ''),
                NULLIF(TRIM(oa.notified_date), ''),
                NULLIF(TRIM(oa.received_date), ''),
                NULLIF(TRIM(oa.done_date), '')
              ) ASC,
              oa.oa_id DESC
            LIMIT :limit
            """,
            {"limit": due_limit},
        )
        for r in due_rows_raw or []:
            doc_name = (r.get("doc_name") or "").strip()
            if not doc_name:
                continue
            notified_date = (r.get("notified_date") or "").strip()
            received_date = (r.get("received_date") or "").strip()
            due_date = (r.get("due_date") or "").strip()
            extended_due_date = (r.get("extended_due_date") or "").strip()

            due_rows.append(
                {
                    "id": r.get("id") or "",
                    "doc_name": doc_name,
                    "notified_date": notified_date,
                    "due_date": due_date,
                    "extended_due_date": extended_due_date,
                    "done_date": (r.get("done_date") or "").strip(),
                }
            )
    except Exception as e:
        _rollback_session()
        if not _is_missing_table_error(e, "office_action"):
            current_app.logger.error(f"Error in due_rows query: {e}")
        due_rows = []
        due_total_count = 0

    due_rows.sort(
        key=lambda d: (
            (
                1
                if not _first_date(
                    d.get("due_date"),
                    d.get("extended_due_date"),
                    d.get("notified_date"),
                    d.get("done_date"),
                )
                else 0
            ),
            _first_date(
                d.get("due_date"),
                d.get("extended_due_date"),
                d.get("notified_date"),
                d.get("done_date"),
            ),
        )
    )

    return {
        "docket_items": docket_items,
        "docket_open": docket_open,
        "docket_done": docket_done,
        "docket_due": docket_due,
        "docket_scheduled": docket_scheduled,
        "next_docket": next_docket,
        "docket_history": docket_history,
        "notice_send_semi_close_prompt": notice_send_semi_close_prompt,
        "annuity_items": annuity_items,
        "annuity_default_visible_cycle_nos": annuity_default_visible_cycle_nos,
        "annuity_management_disabled": annuity_management_disabled,
        "annuity_autogen_hint": annuity_autogen_hint,
        "memos": memos,
        "workflows": workflows,
        "history_rows": history_rows,
        "history_merge_groups": history_merge_groups,
        "history_total_count": history_total_count or len(history_rows),
        "history_truncated": bool(
            include_history_details
            and history_total_count
            and history_total_count > len(history_rows)
        ),
        "due_rows": due_rows,
        "due_total_count": due_total_count or len(due_rows),
        "due_truncated": bool(due_total_count and due_total_count > len(due_rows)),
        "file_rows": [],
        "_history_count": history_total_count or len(history_rows),
        "today_iso": date.today().isoformat(),
    }


def _build_family_section(ctx: dict) -> dict:
    mid_str = ctx["_mid_str"]
    identifiers = ctx["identifiers"]
    matter = ctx["matter"]
    overview = ctx["overview"]

    APP_IDENTIFIER_TYPES = [
        "Application No.",
        "APP_NO",
        "application_no",
        "app_no",
        "PCT Application No.",
        "PCTApplication No.",
        "pct_application_no",
        "EP Application No.",
        "EPApplication No.",
        "ep_application_no",
    ]
    PRIORITY_IDENTIFIER_TYPES = ["Text", "Priority", "priority_no"]
    ORIGIN_IDENTIFIER_TYPES = ["Parent application No.", "parent_application_no"]
    REFERENCE_IDENTIFIER_TYPES = [
        *ORIGIN_IDENTIFIER_TYPES,
        "PCT Application No.",
        "PCTApplication No.",
        "pct_application_no",
        "EP Application No.",
        "EPApplication No.",
        "ep_application_no",
    ]
    AUTO_RELATION_LABELS = {"Priority", "Parent application", "", "Priority()", "Priority()"}
    FAMILY_EXCLUDE_NAMESPACE = "family"
    FAMILY_EXCLUDE_KEY = "excluded_related_matter_ids"

    related_by_mid: dict[str, dict] = {}
    family_keys = []
    family_key_by_id: dict[str, str] = {}
    family_id_order: list[str] = []
    family_count = 0

    def _active_matter_filter():
        return or_(Matter.is_deleted.is_(False), Matter.is_deleted.is_(None))

    def _normalize_identifier(raw: str) -> str:
        return "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()

    def _split_identifier_values(raw_values: list[str] | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in raw_values or []:
            text_val = str(raw or "").strip()
            if not text_val:
                continue
            text_val = (
                text_val.replace("\r\n", "\n")
                .replace("\r", "\n")
                .replace("，", ",")
                .replace(";", ",")
                .replace("|", ",")
                .replace("\n", ",")
            )
            for tok in text_val.split(","):
                token = (tok or "").strip()
                if not token or token in seen:
                    continue
                seen.add(token)
                out.append(token)
        return out

    def _collect_identifier_values(*keys: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for key in keys:
            for raw in identifiers.get(key) or []:
                val = (raw or "").strip()
                if not val or val in seen:
                    continue
                seen.add(val)
                out.append(val)
        return out

    def _collect_custom_field_values(*keys: str) -> list[str]:
        """
        Fallback for migrated/legacy matters where identifiers are missing but canonical
        custom-field keys were saved.
        """
        out: list[str] = []
        seen: set[str] = set()
        if not keys:
            return out
        try:
            rows = MatterCustomField.query.filter_by(matter_id=mid_str).all()
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in family custom-field fallback query: {e}")
            return out

        for row in rows or []:
            payload = row.data if isinstance(row.data, dict) else {}
            if not isinstance(payload, dict):
                continue
            for key in keys:
                raw = payload.get(key)
                if isinstance(raw, str):
                    v = raw.strip()
                    if v and v not in seen:
                        seen.add(v)
                        out.append(v)
                elif isinstance(raw, list) and key == "related_applications":
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        v = str(item.get("number") or "").strip()
                        if v and v not in seen:
                            seen.add(v)
                            out.append(v)
        return out

    def _load_auto_excluded_related_ids() -> set[str]:
        excluded: set[str] = set()
        try:
            row = MatterCustomField.query.filter_by(
                matter_id=mid_str, namespace=FAMILY_EXCLUDE_NAMESPACE
            ).first()
            data = row.data if row and isinstance(row.data, dict) else {}
            raw_values = data.get(FAMILY_EXCLUDE_KEY)
            if isinstance(raw_values, list):
                for raw in raw_values:
                    rel_mid = str(raw or "").strip()
                    if rel_mid and rel_mid != mid_str:
                        excluded.add(rel_mid)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in family auto-exclude query: {e}")
        return excluded

    def _normalized_identifier_expr(column):
        expr = func.upper(func.coalesce(column, ""))
        for token in ("-", " ", ".", "/", "_", "(", ")", "[", "]", ","):
            expr = func.replace(expr, token, "")
        return expr

    def _register_related(
        m: Matter | None,
        ov: VMatterOverview | None,
        relation_label: str = "",
        family_ids: set[str] | None = None,
    ) -> None:
        if not m or not getattr(m, "matter_id", None):
            return
        if bool(getattr(m, "is_deleted", False)):
            return
        rid = str(m.matter_id).strip()
        if not rid or rid == mid_str:
            return

        current = related_by_mid.get(rid)
        if not current:
            current = {
                "matter": m,
                "overview": ov,
                "relation_labels": set(),
                "family_ids": set(),
            }
            related_by_mid[rid] = current
        else:
            if current.get("overview") is None and ov is not None:
                current["overview"] = ov

        label = (relation_label or "").strip()
        if label:
            current["relation_labels"].add(label)
        if family_ids:
            current.setdefault("family_ids", set()).update(
                fid for fid in family_ids if (fid or "").strip()
            )

    def _find_related_by_identifier(
        *,
        id_types: list[str],
        raw_candidates: list[str],
        norm_candidates: set[str],
    ) -> list[tuple[Matter, VMatterOverview | None, MatterIdentifier]]:
        if not id_types or (not raw_candidates and not norm_candidates):
            return []

        q_base = (
            db.session.query(Matter, VMatterOverview, MatterIdentifier)
            .join(MatterIdentifier, MatterIdentifier.matter_id == Matter.matter_id)
            .outerjoin(VMatterOverview, VMatterOverview.matter_id == Matter.matter_id)
            .filter(MatterIdentifier.matter_id != mid_str)
            .filter(_active_matter_filter())
            .filter(MatterIdentifier.id_type.in_(id_types))
            .order_by(Matter.our_ref.asc(), MatterIdentifier.mid_id.asc())
        )

        rows: list[tuple[Matter, VMatterOverview | None, MatterIdentifier]] = []
        seen: set[tuple[str, str]] = set()

        def _append(candidate_rows):
            for m, ov, mi in candidate_rows or []:
                key = ((m.matter_id or "").strip(), (mi.mid_id or "").strip())
                if key in seen:
                    continue
                seen.add(key)
                rows.append((m, ov, mi))

        if raw_candidates:
            exact_rows = q_base.filter(MatterIdentifier.id_value.in_(raw_candidates)).all()
            _append(exact_rows)

        lookup_norms = {n for n in norm_candidates if n}
        if lookup_norms:
            norm_expr = _normalized_identifier_expr(MatterIdentifier.id_value)
            norm_rows = q_base.filter(norm_expr.in_(sorted(lookup_norms))).all()
            _append(norm_rows)

        return rows

    def _collect_connected_family_component(start_matter_id: str) -> tuple[set[str], set[str]]:
        start = (start_matter_id or "").strip()
        if not start:
            return set(), set()

        known_mids: set[str] = {start}
        known_fams: set[str] = set()
        for _ in range(64):
            changed = False
            if known_mids:
                fam_rows = (
                    db.session.query(MatterFamily.family_id)
                    .filter(MatterFamily.matter_id.in_(sorted(known_mids)))
                    .distinct()
                    .all()
                )
                for (fam_id,) in fam_rows or []:
                    fid = (fam_id or "").strip()
                    if fid and fid not in known_fams:
                        known_fams.add(fid)
                        changed = True
            if known_fams:
                mid_rows = (
                    db.session.query(MatterFamily.matter_id)
                    .join(Matter, Matter.matter_id == MatterFamily.matter_id)
                    .filter(MatterFamily.family_id.in_(sorted(known_fams)))
                    .filter(_active_matter_filter())
                    .distinct()
                    .all()
                )
                for (matter_id,) in mid_rows or []:
                    m_id = (matter_id or "").strip()
                    if m_id and m_id not in known_mids:
                        known_mids.add(m_id)
                        changed = True
            if not changed:
                break
        return known_fams, known_mids

    # Explicit family links
    try:
        component_family_ids, component_mids = _collect_connected_family_component(mid_str)
        if component_family_ids:
            for f in Family.query.filter(Family.family_id.in_(sorted(component_family_ids))).all():
                fid = (f.family_id or "").strip()
                key = (f.family_key or "").strip()
                if not fid or not key or fid in family_key_by_id:
                    continue
                family_key_by_id[fid] = key
                family_id_order.append(fid)

            family_ids_by_mid: dict[str, set[str]] = {}
            for mf in (
                MatterFamily.query.filter(MatterFamily.family_id.in_(sorted(component_family_ids)))
                .order_by(MatterFamily.created_at.asc(), MatterFamily.mf_id.asc())
                .all()
            ):
                rel_mid = (mf.matter_id or "").strip()
                fam_id = (mf.family_id or "").strip()
                if rel_mid and fam_id:
                    family_ids_by_mid.setdefault(rel_mid, set()).add(fam_id)

            rows = (
                db.session.query(Matter, VMatterOverview)
                .outerjoin(VMatterOverview, VMatterOverview.matter_id == Matter.matter_id)
                .filter(Matter.matter_id.in_(sorted(component_mids)))
                .filter(_active_matter_filter())
                .order_by(Matter.our_ref.asc())
                .all()
            )
            for m, ov in rows:
                _register_related(
                    m,
                    ov,
                    "Family",
                    family_ids=family_ids_by_mid.get(str(m.matter_id).strip()) or set(),
                )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in family explicit query: {e}")
        related_by_mid = {}
        family_key_by_id = {}
        family_id_order = []

    # Auto-link logic
    try:
        priority_vals = _split_identifier_values(
            _collect_identifier_values(*PRIORITY_IDENTIFIER_TYPES)
            + _collect_custom_field_values("priority_no")
        )
        origin_vals = _split_identifier_values(
            _collect_identifier_values(*ORIGIN_IDENTIFIER_TYPES)
            + _collect_custom_field_values("parent_application_no", "related_applications")
        )
        app_vals = _split_identifier_values(
            _collect_identifier_values(*APP_IDENTIFIER_TYPES)
            + _collect_custom_field_values(
                "application_no", "pct_application_no", "ep_application_no"
            )
        )

        priority_norms = {
            _normalize_identifier(v) for v in priority_vals if _normalize_identifier(v)
        }
        origin_norms = {_normalize_identifier(v) for v in origin_vals if _normalize_identifier(v)}

        candidate_vals = list(dict.fromkeys(priority_vals + origin_vals))
        candidate_norms = {
            _normalize_identifier(v) for v in candidate_vals if _normalize_identifier(v)
        }

        if candidate_vals:
            rows = _find_related_by_identifier(
                id_types=APP_IDENTIFIER_TYPES,
                raw_candidates=candidate_vals,
                norm_candidates=candidate_norms,
            )
            for m, ov, mi in rows:
                matched_norm = _normalize_identifier(mi.id_value)
                labels = []
                if matched_norm and matched_norm in priority_norms:
                    labels.append("Priority")
                if matched_norm and matched_norm in origin_norms:
                    labels.append("Parent application")
                if not labels:
                    labels = ["Parent application"]
                for label in labels:
                    _register_related(m, ov, label)

        if app_vals:
            app_norms = {_normalize_identifier(v) for v in app_vals if _normalize_identifier(v)}
            rows = _find_related_by_identifier(
                id_types=PRIORITY_IDENTIFIER_TYPES,
                raw_candidates=app_vals,
                norm_candidates=app_norms,
            )
            for m, ov, _mi in rows:
                _register_related(m, ov, "Priority()")

            rows = _find_related_by_identifier(
                id_types=REFERENCE_IDENTIFIER_TYPES,
                raw_candidates=app_vals,
                norm_candidates=app_norms,
            )
            for m, ov, _mi in rows:
                _register_related(m, ov, "")

        if priority_vals:
            rows = _find_related_by_identifier(
                id_types=PRIORITY_IDENTIFIER_TYPES,
                raw_candidates=priority_vals,
                norm_candidates=priority_norms,
            )
            for m, ov, _mi in rows:
                _register_related(m, ov, "Priority()")

        if origin_vals:
            rows = _find_related_by_identifier(
                id_types=REFERENCE_IDENTIFIER_TYPES,
                raw_candidates=origin_vals,
                norm_candidates=origin_norms,
            )
            for m, ov, _mi in rows:
                _register_related(m, ov, "")
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in family auto-link query: {e}")

    # Apply user exclusions to automatic relation labels only.
    auto_excluded_ids = _load_auto_excluded_related_ids()
    if auto_excluded_ids:
        for rel_mid in list(auto_excluded_ids):
            payload = related_by_mid.get(rel_mid)
            if not payload:
                continue
            labels = {x for x in (payload.get("relation_labels") or set()) if x}
            if not labels:
                related_by_mid.pop(rel_mid, None)
                continue
            kept = {lbl for lbl in labels if lbl not in AUTO_RELATION_LABELS}
            if kept:
                payload["relation_labels"] = kept
            else:
                related_by_mid.pop(rel_mid, None)

    # Build View Rows
    related_matters = []
    related_family_rows = []
    try:
        relation_order = {
            "Family": 1,
            "Priority": 2,
            "Parent application": 3,
            "": 4,
            "Priority()": 5,
            "Priority()": 6,
        }
        rel_list = sorted(
            [r for r in related_by_mid.values() if r.get("matter")],
            key=lambda r: (
                ((getattr(r.get("matter"), "our_ref", "") or "").strip()),
                ((getattr(r.get("matter"), "matter_id", "") or "").strip()),
            ),
        )
        acl_user = ctx.get("_current_user")
        if acl_user is not None:
            try:
                from app.utils.permissions import can_access_matter

                rel_list = [
                    r
                    for r in rel_list
                    if can_access_matter(acl_user, str(r["matter"].matter_id), action="view")
                ]
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error filtering related matters by ACL: {e}")
                rel_list = []
        family_count = len(rel_list)
        visible_family_ids: set[str] = set()
        for r in rel_list:
            visible_family_ids.update(r.get("family_ids") or set())
        family_keys = [
            family_key_by_id[fid]
            for fid in family_id_order
            if fid in visible_family_ids and family_key_by_id.get(fid)
        ]
        for r in rel_list:
            labels = sorted(
                [x for x in (r.get("relation_labels") or set()) if x],
                key=lambda x: (relation_order.get(x, 99), x),
            )
            related_matters.append(
                {
                    "matter": r.get("matter"),
                    "overview": r.get("overview"),
                    "relation_label": ", ".join(labels),
                }
            )

        rel_overviews = [r.get("overview") for r in rel_list if r.get("overview")]
        rel_extras = _build_case_list_extras(rel_overviews) if rel_overviews else {}

        rel_ids = [r["matter"].matter_id for r in rel_list]
        rel_ident_rows = (
            MatterIdentifier.query.filter(MatterIdentifier.matter_id.in_(rel_ids)).all()
            if rel_ids
            else []
        )
        rel_ident_map: dict[str, dict[str, list[str]]] = {}
        for rr in rel_ident_rows:
            mid = (rr.matter_id or "").strip()
            if not mid:
                continue
            k = (rr.id_type or "").strip()
            v = (rr.id_value or "").strip()
            if not k or not v:
                continue
            rel_ident_map.setdefault(mid, {})
            rel_ident_map[mid].setdefault(k, [])
            if v not in rel_ident_map[mid][k]:
                rel_ident_map[mid][k].append(v)

        def _first_ident(mid: str, *keys: str) -> str:
            m = rel_ident_map.get(mid) or {}
            for k in keys:
                vals = m.get(k) or []
                if vals:
                    return vals[0]
            return ""

        def _case_type_label(m: Matter, overview_row: VMatterOverview | None = None) -> str:
            public_div, public_typ = resolve_public_case_kind_for_matter(m, overview_row)
            if public_div or public_typ:
                return format_public_case_kind_label(public_div, public_typ)
            return m.right_name or "Matter"

        for r in rel_list:
            m = r["matter"]
            ov = r.get("overview")
            mid = m.matter_id
            x = rel_extras.get(mid) or {}
            relation_labels = sorted(
                [x for x in (r.get("relation_labels") or set()) if x],
                key=lambda x: (relation_order.get(x, 99), x),
            )
            relation_label = ", ".join(relation_labels)
            type_label = _case_type_label(m, ov)
            work_label = f"{type_label} ({relation_label})" if relation_label else type_label
            fam_auto = None
            try:
                fam_auto = derive_auto_status(
                    matter_id=str(mid),
                    current_red=(m.status_red or "").strip(),
                    current_red_date=(m.status_red_related_date or "").strip(),
                    current_blue=(m.status_blue or "").strip(),
                    memo=(m.memo or "").strip(),
                )
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error in family auto status: {e}")
                fam_auto = None
            related_family_rows.append(
                {
                    "matter_id": mid,
                    "our_ref": m.our_ref or "",
                    "relation_label": relation_label,
                    "work_label": work_label,
                    "auto_status_red": (fam_auto.display_red if fam_auto else "")
                    or (m.status_red or "")
                    or (ov.status_red if ov else "")
                    or "",
                    "auto_status_blue": (fam_auto.display_blue if fam_auto else "")
                    or (m.status_blue or "")
                    or (ov.status_blue if ov else "")
                    or "",
                    "application_date": _date_only_str(x.get("application_date") or ""),
                    "application_no": (x.get("application_no") or "").strip()
                    or _first_ident(mid, "Application No.", "APP_NO", "application_no", "app_no"),
                    "origin_application_no": _first_ident(mid, "Parent application No."),
                    "registration_no": _first_ident(mid, "Registration No.", "registration_no", "reg_no"),
                    "title": (m.right_name or "").strip()
                    or ((ov.right_name if ov else "") or "").strip()
                    or (x.get("proposal_title") or "").strip(),
                }
            )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in family view rows build: {e}")
        related_family_rows = []

    related_application_suggestion = None
    pct_related_application_suggestion = None
    try:
        related_application_suggestion = build_related_application_suggestion(
            matter=matter,
            related_family_rows=related_family_rows,
        )
        if (related_application_suggestion or {}).get("target") == "pct":
            pct_related_application_suggestion = related_application_suggestion
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in related application suggestion: {e}")

    # Priority Rows
    priority_rows = []
    try:
        priority_nos = _split_identifier_values(
            _collect_identifier_values(*PRIORITY_IDENTIFIER_TYPES)
        )
        if priority_nos:
            claim_date = ""
            try:
                claim_date = (
                    db.session.execute(
                        text("""
                            SELECT event_at
                            FROM matter_event
                            WHERE matter_id = :mid
                              AND event_key IN ('PRIORITY_DATE', 'Text', '')
                            ORDER BY event_at DESC
                            LIMIT 1
                            """),
                        {"mid": mid_str},
                    ).scalar()
                    or ""
                )
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error in claim_date query: {e}")
                claim_date = ""

            # Fallback: imported/migrated cases may have custom fields filled but no matter_event rows.
            # Avoid relying on "self-heal" (DB writes) during a view-only GET.
            if not (claim_date or "").strip():
                try:
                    cf_rows = (
                        MatterCustomField.query.filter_by(matter_id=mid_str)
                        .filter(MatterCustomField.namespace.in_(_EVENT_SYNC_NAMESPACE_ORDER))
                        .all()
                    )
                    data_by_ns: dict[str, dict] = {}
                    for row in cf_rows or []:
                        ns = (getattr(row, "namespace", "") or "").strip()
                        payload = getattr(row, "data", None)
                        if ns and isinstance(payload, dict) and payload:
                            data_by_ns[ns] = payload

                    _ns, picked = _pick_event_sync_payload(
                        matter=matter, overview=overview, data_by_ns=data_by_ns
                    )
                    if isinstance(picked, dict):
                        claim_date = _date_only_str(picked.get("priority_date") or "") or claim_date
                    if not (claim_date or "").strip():
                        for payload in data_by_ns.values():
                            dt = _date_only_str(payload.get("priority_date") or "")
                            if dt:
                                claim_date = dt
                                break
                except Exception as e:
                    _rollback_session()
                    current_app.logger.error(f"Error in claim_date custom field fallback: {e}")

            input_date = _date_only_str((matter.entered_at or "") or (matter.retained_at or ""))

            priority_country_map: dict[str, str] = {}
            try:
                current_prio_rows = (
                    MatterIdentifier.query.filter(MatterIdentifier.matter_id == mid_str)
                    .filter(MatterIdentifier.id_type.in_(PRIORITY_IDENTIFIER_TYPES))
                    .all()
                )
                for rr in current_prio_rows or []:
                    key = _normalize_identifier(rr.id_value)
                    country = (rr.country or "").strip()
                    if key and country and key not in priority_country_map:
                        priority_country_map[key] = country
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error in priority country map query: {e}")
                priority_country_map = {}

            def _infer_country_label(priority_no: str) -> str:
                v = (priority_no or "").strip()
                norm = _normalize_identifier(v)
                stored_country = (priority_country_map.get(norm) or "").strip()
                if stored_country:
                    country_code = stored_country.upper()
                    if len(country_code) == 2 and country_code.isalpha():
                        return country_code
                    return stored_country
                return ""

            link_map_raw: dict[str, dict] = {}
            link_map_norm: dict[str, dict] = {}
            try:
                rows = _find_related_by_identifier(
                    id_types=APP_IDENTIFIER_TYPES,
                    raw_candidates=priority_nos,
                    norm_candidates={
                        _normalize_identifier(v) for v in priority_nos if _normalize_identifier(v)
                    },
                )
                for m, ov, mi in rows:
                    if (m.matter_id or "").strip() == mid_str:
                        continue
                    acl_user = ctx.get("_current_user")
                    if acl_user is not None:
                        try:
                            from app.utils.permissions import can_access_matter

                            if not can_access_matter(acl_user, str(m.matter_id), action="view"):
                                continue
                        except Exception as e:
                            _rollback_session()
                            current_app.logger.error(
                                f"Error filtering priority linked case by ACL: {e}"
                            )
                            continue
                    key_raw = (mi.id_value or "").strip()
                    key_norm = _normalize_identifier(key_raw)
                    payload = {"matter": m, "overview": ov}
                    if key_raw and key_raw not in link_map_raw:
                        link_map_raw[key_raw] = payload
                    if key_norm and key_norm not in link_map_norm:
                        link_map_norm[key_norm] = payload
            except Exception as e:
                _rollback_session()
                current_app.logger.error(f"Error in link_map query: {e}")
                link_map_raw = {}
                link_map_norm = {}

            for i, pr_no in enumerate(priority_nos, start=1):
                pr_no = (pr_no or "").strip()
                if not pr_no:
                    continue
                linked = (
                    link_map_raw.get(pr_no) or link_map_norm.get(_normalize_identifier(pr_no)) or {}
                )
                m = linked.get("matter")
                ov = linked.get("overview")
                priority_rows.append(
                    {
                        "no": i,
                        "input_date": input_date,
                        "country": _infer_country_label(pr_no),
                        "claim_date": _date_only_str(claim_date),
                        "priority_no": pr_no,
                        "doc_deadline": "",
                        "doc_submitted_date": "",
                        "announcement_date": "",
                        "note": "",
                        "linked_case_id": (m.matter_id if m else ""),
                        "linked_our_ref": (m.our_ref if m else ""),
                        "linked_title": (m.right_name if m else "")
                        or ((ov.right_name if ov else "") or ""),
                    }
                )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Error in priority rows query: {e}")
        priority_rows = []

    return {
        "related_matters": related_matters,
        "related_family_rows": related_family_rows,
        "family_keys": family_keys,
        "family_count": family_count,
        "related_application_suggestion": related_application_suggestion,
        "pct_related_application_suggestion": pct_related_application_suggestion,
        "priority_rows": priority_rows,
    }


def _build_specific_fields_section(ctx: dict) -> dict:
    matter = ctx["matter"]
    overview = ctx["overview"]
    mid_str = ctx["_mid_str"]
    basic_data = ctx["basic_data"]
    app_no = ctx["app_no"]
    pub_no = ctx["pub_no"]
    reg_no = ctx["reg_no"]

    div, typ = _infer_case_kind(matter, overview)

    is_domestic_patent = div == "DOM" and typ in PATENT_LIKE_TYPES
    is_domestic_design = div == "DOM" and typ == "DESIGN"
    is_domestic_trademark = div == "DOM" and typ == "TRADEMARK"
    is_incoming_patent = div == "INC" and typ in PATENT_LIKE_TYPES
    is_incoming_design = div == "INC" and typ == "DESIGN"
    is_incoming_trademark = div == "INC" and typ == "TRADEMARK"
    is_outgoing_patent = div == "OUT" and typ in PATENT_LIKE_TYPES
    is_outgoing_design = div == "OUT" and typ == "DESIGN"
    is_outgoing_trademark = div == "OUT" and typ == "TRADEMARK"
    is_pct = typ == "PCT"
    is_litigation = typ == "LITIGATION"
    is_misc = typ == "MISC"

    normalized_div = _normalize_case_division(matter.right_group) or _normalize_case_division(
        overview.right_group if overview else ""
    )
    normalized_type = _normalize_case_type(matter.matter_type) or _normalize_case_type(
        overview.matter_type if overview else ""
    )

    # Force enable if normalization matches (Legacy logic preservation)
    if not is_domestic_patent and normalized_div == "DOM" and normalized_type in PATENT_LIKE_TYPES:
        is_domestic_patent = True
    if not is_domestic_design and normalized_div == "DOM" and normalized_type == "DESIGN":
        is_domestic_design = True
    if not is_domestic_trademark and normalized_div == "DOM" and normalized_type == "TRADEMARK":
        is_domestic_trademark = True
    if not is_incoming_patent and normalized_div == "INC" and normalized_type in PATENT_LIKE_TYPES:
        is_incoming_patent = True
    if not is_incoming_design and normalized_div == "INC" and normalized_type == "DESIGN":
        is_incoming_design = True
    if not is_incoming_trademark and normalized_div == "INC" and normalized_type == "TRADEMARK":
        is_incoming_trademark = True
    if not is_litigation and normalized_type == "LITIGATION":
        is_litigation = True
    if not is_misc and normalized_type == "MISC":
        is_misc = True
    if not is_outgoing_trademark and normalized_div == "OUT" and normalized_type == "TRADEMARK":
        is_outgoing_trademark = True
    if not is_pct and normalized_type == "PCT":
        is_pct = True

    # Also check custom field presence (Legacy preservation)
    def _has(ns):
        return bool(MatterCustomField.query.filter_by(matter_id=mid_str, namespace=ns).first())

    if _has("incoming_patent"):
        is_incoming_patent = True
    if _has("incoming_design"):
        is_incoming_design = True
    if _has("incoming_trademark"):
        is_incoming_trademark = True
    if _has("pct"):
        is_pct = True
    if _has("misc"):
        is_misc = True

    # Case kind normalization is handled on write paths; avoid mutations here.

    # Build Field dicts
    dom_patent = {}
    if is_domestic_patent:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="domestic_patent"
        ).first()
        dom_patent = (row.data or {}) if row else {}
        dom_patent.setdefault("application_no", app_no)
        dom_patent.setdefault("publication_no", pub_no)
        dom_patent.setdefault("client_name", (overview.clients if overview else "") or "")
        dom_patent.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        try:
            dom_patent = _fill_dom_patent_from_ipm(matter_obj=matter, dom_patent=dom_patent)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in dom_patent fill: {e}")
        dom_patent = _overlay_basic_staff_fields(dom_patent, basic_data)

    dom_design = {}
    if is_domestic_design and not is_domestic_patent:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="domestic_design"
        ).first()
        dom_design = (row.data or {}) if row else {}
        dom_design.setdefault("application_no", app_no)
        dom_design.setdefault("publication_no", pub_no)
        dom_design.setdefault("registration_no", reg_no)
        dom_design.setdefault("client_name", (overview.clients if overview else "") or "")
        dom_design.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        dom_design.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            dom_design = _fill_dom_design_from_ipm(matter_obj=matter, dom_design=dom_design)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in dom_design fill: {e}")
        dom_design = _overlay_basic_staff_fields(dom_design, basic_data)

    dom_trademark = {}
    if is_domestic_trademark and not (is_domestic_patent or is_domestic_design):
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="domestic_trademark"
        ).first()
        dom_trademark = (row.data or {}) if row else {}
        dom_trademark.setdefault("application_no", app_no)
        dom_trademark.setdefault("publication_no", pub_no)
        dom_trademark.setdefault("registration_no", reg_no)
        dom_trademark.setdefault("client_name", (overview.clients if overview else "") or "")
        dom_trademark.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        dom_trademark.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            dom_trademark = _fill_dom_trademark_from_ipm(matter_obj=matter, dom_tm=dom_trademark)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in dom_trademark fill: {e}")
        dom_trademark = _overlay_basic_staff_fields(dom_trademark, basic_data)

    inc_patent = {}
    if is_incoming_patent:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="incoming_patent"
        ).first()
        inc_patent = (row.data or {}) if row else {}
        inc_patent.setdefault("application_no", app_no)
        inc_patent.setdefault("publication_no", pub_no)
        inc_patent.setdefault("registration_no", reg_no)
        inc_patent.setdefault("client_name", (overview.clients if overview else "") or "")
        inc_patent.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        inc_patent.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            inc_patent = _fill_incoming_patent_from_ipm(matter_obj=matter, inc_patent=inc_patent)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in inc_patent fill: {e}")
        inc_patent = _overlay_basic_staff_fields(inc_patent, basic_data)

    inc_design = {}
    if is_incoming_design:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="incoming_design"
        ).first()
        inc_design = (row.data or {}) if row else {}
        inc_design.setdefault("application_no", app_no)
        inc_design.setdefault("publication_no", pub_no)
        inc_design.setdefault("registration_no", reg_no)
        inc_design.setdefault("client_name", (overview.clients if overview else "") or "")
        inc_design.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        inc_design.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            inc_design = _fill_incoming_design_from_ipm(matter_obj=matter, inc_design=inc_design)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in inc_design fill: {e}")
        inc_design = _overlay_basic_staff_fields(inc_design, basic_data)

    inc_trademark = {}
    if is_incoming_trademark:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="incoming_trademark"
        ).first()
        inc_trademark = (row.data or {}) if row else {}
        inc_trademark.setdefault("application_no", app_no)
        inc_trademark.setdefault("publication_no", pub_no)
        inc_trademark.setdefault("registration_no", reg_no)
        inc_trademark.setdefault("client_name", (overview.clients if overview else "") or "")
        inc_trademark.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        inc_trademark.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            inc_trademark = _fill_incoming_trademark_from_ipm(
                matter_obj=matter, inc_trademark=inc_trademark
            )
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in inc_trademark fill: {e}")
        inc_trademark = _overlay_basic_staff_fields(inc_trademark, basic_data)

    out_patent = {}
    if is_outgoing_patent:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="outgoing_patent"
        ).first()
        out_patent = (row.data or {}) if row else {}
        out_patent.setdefault("application_no", app_no)
        out_patent.setdefault("publication_no", pub_no)
        out_patent.setdefault("registration_no", reg_no)
        out_patent.setdefault("client_name", (overview.clients if overview else "") or "")
        out_patent.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        out_patent.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            out_patent = _fill_outgoing_patent_from_ipm(matter_obj=matter, out_patent=out_patent)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in out_patent fill: {e}")
        out_patent = _overlay_basic_staff_fields(out_patent, basic_data)

    out_design = {}
    if is_outgoing_design:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="outgoing_design"
        ).first()
        out_design = (row.data or {}) if row else {}
        out_design.setdefault("application_no", app_no)
        out_design.setdefault("publication_no", pub_no)
        out_design.setdefault("registration_no", reg_no)
        out_design.setdefault("client_name", (overview.clients if overview else "") or "")
        out_design.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        out_design.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            out_design = _fill_outgoing_design_from_ipm(matter_obj=matter, out_design=out_design)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in out_design fill: {e}")
        out_design = _overlay_basic_staff_fields(out_design, basic_data)

    out_trademark = {}
    if is_outgoing_trademark:
        row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="outgoing_trademark"
        ).first()
        out_trademark = (row.data or {}) if row else {}
        out_trademark.setdefault("application_no", app_no)
        out_trademark.setdefault("publication_no", pub_no)
        out_trademark.setdefault("registration_no", reg_no)
        out_trademark.setdefault("client_name", (overview.clients if overview else "") or "")
        out_trademark.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        out_trademark.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            out_trademark = _fill_outgoing_trademark_from_ipm(
                matter_obj=matter, out_trademark=out_trademark
            )
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in out_trademark fill: {e}")
        out_trademark = _overlay_basic_staff_fields(out_trademark, basic_data)

    identifiers = ctx.get("identifiers") or {}

    def _contains_madrid(value: str | None) -> bool:
        v = (value or "").strip()
        if not v:
            return False
        if "" in v:
            return True
        return "madrid" in v.lower()

    def _contains_hague(value: str | None) -> bool:
        v = (value or "").strip()
        if not v:
            return False
        if "" in v:
            return True
        return "hague" in v.lower()

    is_madrid = False
    if is_outgoing_trademark:
        if _contains_madrid(out_trademark.get("app_route")):
            is_madrid = True
        elif (out_trademark.get("madrid_application_no") or "").strip():
            is_madrid = True
        elif (out_trademark.get("madrid_application_date") or "").strip():
            is_madrid = True
        else:
            for key, values in (identifiers or {}).items():
                if not key:
                    continue
                if "" in key or "madrid" in key.lower():
                    if any((v or "").strip() for v in (values or [])):
                        is_madrid = True
                        break
        if not is_madrid:
            right_name = matter.right_name or (overview.right_name if overview else "") or ""
            if _contains_madrid(right_name):
                is_madrid = True

    is_hague = False
    if is_outgoing_design:
        if _contains_hague(out_design.get("app_route")):
            is_hague = True
        elif (out_design.get("hague_application_no") or "").strip():
            is_hague = True
        elif (out_design.get("hague_application_date") or "").strip():
            is_hague = True
        else:
            for key, values in (identifiers or {}).items():
                if not key:
                    continue
                if "" in key or "hague" in key.lower():
                    if any((v or "").strip() for v in (values or [])):
                        is_hague = True
                        break
        if not is_hague:
            right_name = matter.right_name or (overview.right_name if overview else "") or ""
            if _contains_hague(right_name):
                is_hague = True

    pct = {}
    if is_pct:
        row = MatterCustomField.query.filter_by(matter_id=mid_str, namespace="pct").first()
        pct = (row.data or {}) if row else {}
        pct.setdefault("application_no", app_no)
        pct.setdefault("publication_no", pub_no)
        pct.setdefault("client_name", (overview.clients if overview else "") or "")
        pct.setdefault("applicant_name", (overview.applicants if overview else "") or "")
        pct.setdefault("inhouse_status", matter.inhouse_status or "")
        try:
            pct = _fill_pct_from_ipm(matter_obj=matter, pct=pct)
        except Exception as e:
            _rollback_session()
            current_app.logger.error(f"Error in pct fill: {e}")
        pct = _overlay_basic_staff_fields(pct, basic_data)

    litigation = {}
    if is_litigation and not (is_domestic_patent or is_domestic_design or is_domestic_trademark):
        row = MatterCustomField.query.filter_by(matter_id=mid_str, namespace="litigation").first()
        litigation = (row.data or {}) if row else {}
        if "right_type" not in litigation and litigation.get("litigation_right_type"):
            litigation["right_type"] = litigation.get("litigation_right_type")
        if "title" not in litigation and litigation.get("litigation_title"):
            litigation["title"] = litigation.get("litigation_title")
        litigation.setdefault("client_name", (overview.clients if overview else "") or "")
        litigation.setdefault(
            "applicant_registrant", (overview.applicants if overview else "") or ""
        )
        litigation.setdefault(
            "title", matter.right_name or ((overview.right_name if overview else "") or "")
        )
        litigation.setdefault("old_our_ref", matter.old_our_ref or "")
        litigation = _overlay_basic_staff_fields(litigation, basic_data)

    misc = {}
    if is_misc:
        row = MatterCustomField.query.filter_by(matter_id=mid_str, namespace="misc").first()
        misc = (row.data or {}) if row else {}
        if not misc.get("client_name"):
            misc["client_name"] = (overview.clients if overview else "") or ""
        if not misc.get("applicant_name"):
            misc["applicant_name"] = (overview.applicants if overview else "") or ""
        if not misc.get("application_no"):
            misc["application_no"] = app_no
        if not misc.get("old_our_ref"):
            misc["old_our_ref"] = matter.old_our_ref or ""
        if not misc.get("inhouse_status"):
            misc["inhouse_status"] = matter.inhouse_status or ""
        misc = _overlay_basic_staff_fields(misc, basic_data)

    is_copyright = False
    if is_misc:
        for value in (
            misc.get("right_type"),
            misc.get("case_kind"),
            matter.right_name,
            (overview.right_name if overview else ""),
        ):
            value_text = str(value or "").strip()
            if not value_text:
                continue
            lowered = value_text.lower()
            if "" in value_text or "copyright" in lowered:
                is_copyright = True
                break

    registry_image_asset = None
    image_value = ""
    # Image logic
    if is_domestic_design and not is_domestic_patent:
        image_value = (dom_design.get("image") or "").strip()
    elif is_domestic_trademark and not (is_domestic_patent or is_domestic_design):
        image_value = (dom_trademark.get("image") or "").strip()
    elif is_incoming_design:
        image_value = (inc_design.get("image") or "").strip()
    elif is_incoming_trademark:
        image_value = (inc_trademark.get("image") or "").strip()
    elif is_outgoing_design:
        image_value = (out_design.get("image") or "").strip()
    elif is_outgoing_trademark:
        image_value = (out_trademark.get("image") or "").strip()

    if image_value:
        registry_image_asset = _load_linked_file_asset(
            matter_id=mid_str,
            file_asset_id=image_value,
            strict_link=False,
        )

    # Custom Text Namespaces (remaining non-progress ones)
    custom_text_namespaces = [
        ("priority", "Priority"),
        ("license", ""),
        ("transfer", "Previous"),
    ]
    custom_text_data = {}
    for ns, _label in custom_text_namespaces:
        row = MatterCustomField.query.filter_by(matter_id=mid_str, namespace=ns).first()
        custom_text_data[ns] = (row.data or {}) if row else {}

    # Unified Progress Entries (replaces progress_misc, progress, old_workflow)
    from app.models.matter import MatterProgress

    progress_entries = (
        MatterProgress.query.filter_by(matter_id=mid_str)
        .order_by(MatterProgress.created_at.desc())
        .all()
    )
    oa_citation_groups = matter_office_action_citation_groups(mid_str)

    return {
        "is_domestic_patent": is_domestic_patent,
        "dom_patent": dom_patent,
        "dom_patent_fields": DOMESTIC_PATENT_FIELDS,
        "is_domestic_design": is_domestic_design,
        "dom_design": dom_design,
        "dom_design_fields": DOMESTIC_DESIGN_FIELDS,
        "is_domestic_trademark": is_domestic_trademark,
        "dom_trademark": dom_trademark,
        "dom_trademark_fields": DOMESTIC_TRADEMARK_FIELDS,
        "is_incoming_patent": is_incoming_patent,
        "inc_patent": inc_patent,
        "inc_patent_fields": INCOMING_PATENT_FIELDS,
        "is_incoming_design": is_incoming_design,
        "inc_design": inc_design,
        "inc_design_fields": INCOMING_DESIGN_FIELDS,
        "is_incoming_trademark": is_incoming_trademark,
        "inc_trademark": inc_trademark,
        "inc_trademark_fields": INCOMING_TRADEMARK_FIELDS,
        "is_outgoing_patent": is_outgoing_patent,
        "out_patent": out_patent,
        "out_patent_fields": OUTGOING_PATENT_FIELDS,
        "is_outgoing_design": is_outgoing_design,
        "out_design": out_design,
        "out_design_fields": OUTGOING_DESIGN_FIELDS,
        "is_outgoing_trademark": is_outgoing_trademark,
        "out_trademark": out_trademark,
        "out_trademark_fields": OUTGOING_TRADEMARK_FIELDS,
        "is_madrid": is_madrid,
        "is_hague": is_hague,
        "is_pct": is_pct,
        "pct": pct,
        "pct_fields": PCT_FIELDS,
        "is_litigation": is_litigation,
        "litigation": litigation,
        "litigation_fields": LITIGATION_FIELDS,
        "is_misc": is_misc,
        "is_copyright": is_copyright,
        "misc": misc,
        "misc_fields": MISC_FIELDS,
        "registry_image_asset": registry_image_asset,
        "custom_text_namespaces": custom_text_namespaces,
        "custom_text_data": custom_text_data,
        "progress_entries": progress_entries,
        "oa_citation_groups": oa_citation_groups,
        "ledger_hidden_keys": _load_ledger_hidden_keys(),
    }
