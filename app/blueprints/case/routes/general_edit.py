from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import g, has_app_context

from app.blueprints.case.helpers import (
    _clear_duplicate_appeal_no,
    _sync_matter_events_from_dom_design,
    _sync_matter_identifiers_from_dom_design,
)
from app.models.operation import OperationChange
from app.models.ip_records import MatterStaffAssignment
from app.services.case.cascade_delete_service import (
    delete_matter_fk_children,
    delete_workflow_fk_children_for_matter,
)
from app.services.case.case_kind import resolve_public_case_kind_for_matter
from app.services.case.status_task_cleanup import (
    apply_case_status_side_effects,
    terminal_case_status_value,
)
from app.services.ops.operation_log import mark_operation_applied, reserve_operation
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import (
    can_manage_case_globally,
    require_matter_access,
    resolve_matter_id_for_case_ref,
)

from .general_shared import *


@dataclass(frozen=True)
class MatterEditNamespaceResolution:
    active_namespace: str | None
    fallback_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatterEditNamespaceStrategy:
    namespace: str
    data_key: str
    field_layout_fallback: Any
    allowed_keys_fallback: Any
    loader: Callable[["MatterEditLoadContext"], dict]
    apply_image: bool = False
    identifier_sync: Callable[..., None] | None = None
    identifier_arg_name: str | None = None
    sync_party_roles: bool = False
    event_sync: Callable[..., None] | None = None
    event_arg_name: str | None = None
    auto_status_payload_name: str | None = None
    carry_matter_inhouse_status: bool = False


@dataclass
class MatterEditLoadContext:
    matter_id: str
    matter: Matter
    overview: VMatterOverview | None
    basic_data: dict
    custom_rows: dict[str, MatterCustomField]
    identifier_map: dict[str, list[str]]

    def first_identifier(self, *keys: str) -> str:
        return _first_identifier_from_map(self.identifier_map, *keys)


@dataclass
class MatterEditSaveContext:
    matter_id: str
    matter: Matter
    div: str
    typ: str
    request_form: Any
    image_file: Any
    right_name: str
    strict_dates: bool
    custom_rows: dict[str, MatterCustomField]
    custom_changes: dict[str, dict]
    derived_errors: list[str]


_MATTER_EDIT_FLAG_BY_NAMESPACE = {
    "domestic_patent": "is_domestic_patent",
    "domestic_design": "is_domestic_design",
    "domestic_trademark": "is_domestic_trademark",
    "incoming_patent": "is_incoming_patent",
    "incoming_design": "is_incoming_design",
    "incoming_trademark": "is_incoming_trademark",
    "outgoing_patent": "is_outgoing_patent",
    "outgoing_design": "is_outgoing_design",
    "outgoing_trademark": "is_outgoing_trademark",
    "pct": "is_pct",
    "litigation": "is_litigation",
    "misc": "is_misc",
}

_MATTER_EDIT_FALLBACK_NAMESPACES = (
    "incoming_patent",
    "incoming_design",
    "incoming_trademark",
    "outgoing_patent",
    "outgoing_design",
    "outgoing_trademark",
    "pct",
    "misc",
)


def _load_matter_custom_rows(matter_id: str) -> dict[str, MatterCustomField]:
    rows = MatterCustomField.query.filter_by(matter_id=str(matter_id)).all() or []
    custom_rows: dict[str, MatterCustomField] = {}
    for row in rows:
        namespace = (row.namespace or "").strip()
        if namespace:
            custom_rows[namespace] = row
    return custom_rows


def _row_data(row: MatterCustomField | None) -> dict:
    return dict((row.data or {}) if row else {})


def _build_staff_id_map_for_matter(matter_id: str) -> dict[str, str]:
    staff_id_map: dict[str, str] = {}
    try:
        role_codes = {"attorney", "manager", "handler"}
        rows = (
            MatterStaffAssignment.query.filter(
                MatterStaffAssignment.matter_id == str(matter_id),
                func.lower(func.trim(MatterStaffAssignment.staff_role_code)).in_(tuple(role_codes)),
            ).all()
            or []
        )
        by_role: dict[str, set[str]] = {}
        for row in rows:
            role = (row.staff_role_code or "").strip().lower()
            staff_party_id = (row.staff_party_id or "").strip()
            if role not in role_codes or not staff_party_id:
                continue
            by_role.setdefault(role, set()).add(staff_party_id)
        if not by_role:
            return staff_id_map
        party_ids = set()
        for ids in by_role.values():
            party_ids.update(ids)
        if not party_ids:
            return staff_id_map
        users = (
            User.query.filter(
                User.staff_party_id.in_(party_ids),
                User.is_active.is_(True),
            ).all()
            or []
        )
        user_by_party_id = {
            str(u.staff_party_id): str(u.id) for u in users if u.staff_party_id is not None
        }
        for role, party_ids in by_role.items():
            if len(party_ids) != 1:
                continue
            party_id = next(iter(party_ids))
            user_id = user_by_party_id.get(party_id)
            if user_id:
                staff_id_map[f"{role}_id"] = user_id
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.general_edit.build_staff_id_map",
            log_key="case.general_edit.build_staff_id_map",
            log_window_seconds=300,
        )
    return staff_id_map


def _load_identifier_map(matter_id: str) -> dict[str, list[str]]:
    rows = (
        MatterIdentifier.query.filter_by(matter_id=str(matter_id))
        .order_by(MatterIdentifier.id_type.asc())
        .all()
    )
    ident_by_type: dict[str, list[str]] = {}
    for row in rows or []:
        ident_by_type.setdefault(row.id_type, []).append(row.id_value)
    return ident_by_type


def _first_identifier_from_map(ident_by_type: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = ident_by_type.get(key) or []
        if values:
            return (values[0] or "").strip()
    return ""


def _load_matter_edit_field_meta(storage_div: str, storage_typ: str):
    active_field_layout = []
    active_allowed_keys: set[str] = set()
    field_meta: dict[str, dict] = {}
    resolved_profile = None
    try:
        resolved_profile = CaseParameterService.get_case_profile(storage_div, storage_typ)
        active_field_layout, field_meta = CaseParameterService.get_field_layout_with_meta(
            storage_div, storage_typ
        )
        active_allowed_keys = set(resolved_profile.allowed_keys or [])
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.general_edit.field_meta",
            log_key="case.general_edit.field_meta",
            log_window_seconds=300,
        )
    return resolved_profile, active_field_layout, active_allowed_keys, field_meta


def _default_active_edit_namespace(*, resolved_profile, div: str, typ: str) -> str | None:
    profile_namespace = (
        (resolved_profile.namespace or "").strip()
        if resolved_profile is not None and getattr(resolved_profile, "namespace", None)
        else ""
    )
    if profile_namespace in _MATTER_EDIT_FLAG_BY_NAMESPACE:
        return profile_namespace
    if div == "DOM" and typ in PATENT_LIKE_TYPES:
        return "domestic_patent"
    if div == "DOM" and typ == "DESIGN":
        return "domestic_design"
    if div == "DOM" and typ == "TRADEMARK":
        return "domestic_trademark"
    if div == "INC" and typ in PATENT_LIKE_TYPES:
        return "incoming_patent"
    if div == "INC" and typ == "DESIGN":
        return "incoming_design"
    if div == "INC" and typ == "TRADEMARK":
        return "incoming_trademark"
    if div == "OUT" and typ in PATENT_LIKE_TYPES:
        return "outgoing_patent"
    if div == "OUT" and typ == "DESIGN":
        return "outgoing_design"
    if div == "OUT" and typ == "TRADEMARK":
        return "outgoing_trademark"
    if typ == "PCT":
        return "pct"
    if typ == "LITIGATION":
        return "litigation"
    if typ == "MISC":
        return "misc"
    return None


def _resolve_active_edit_namespace(
    *,
    resolved_profile,
    div: str,
    typ: str,
    custom_rows: dict[str, MatterCustomField],
) -> MatterEditNamespaceResolution:
    active_namespace = _default_active_edit_namespace(
        resolved_profile=resolved_profile,
        div=div,
        typ=typ,
    )
    if active_namespace:
        return MatterEditNamespaceResolution(active_namespace=active_namespace)

    candidates = tuple(ns for ns in _MATTER_EDIT_FALLBACK_NAMESPACES if ns in custom_rows)
    if len(candidates) == 1:
        return MatterEditNamespaceResolution(
            active_namespace=candidates[0],
            fallback_candidates=candidates,
        )
    return MatterEditNamespaceResolution(
        active_namespace=None,
        fallback_candidates=candidates,
    )


def _build_active_namespace_flags(active_namespace: str) -> dict[str, bool]:
    flags = {flag_name: False for flag_name in _MATTER_EDIT_FLAG_BY_NAMESPACE.values()}
    active_flag = _MATTER_EDIT_FLAG_BY_NAMESPACE.get(active_namespace)
    if active_flag:
        flags[active_flag] = True
    return flags


def _field_layout_for_strategy(
    strategy: MatterEditNamespaceStrategy,
    *,
    active_namespace: str,
    active_field_layout,
):
    if active_namespace == strategy.namespace and active_field_layout:
        return active_field_layout
    return strategy.field_layout_fallback


def _allowed_keys_for_strategy(
    strategy: MatterEditNamespaceStrategy,
    *,
    active_namespace: str,
    active_allowed_keys,
):
    if active_namespace == strategy.namespace and active_allowed_keys:
        return active_allowed_keys
    return strategy.allowed_keys_fallback


def _logger_or_none():
    if has_app_context():
        return current_app.logger
    return None


def _load_registry_edit_data(
    load_ctx: MatterEditLoadContext,
    *,
    namespace: str,
    fill_fn: Callable[..., dict],
    fill_arg_name: str,
    fill_log_context: str,
    include_registration_no: bool = True,
    include_inhouse_status: bool = False,
) -> dict:
    data = _row_data(load_ctx.custom_rows.get(namespace))

    if not data.get("application_no"):
        data["application_no"] = load_ctx.first_identifier("Application No.", "application_no", "app_no")
    if not data.get("publication_no"):
        data["publication_no"] = load_ctx.first_identifier("Publication No.", "publication_no", "pub_no")
    if include_registration_no and not data.get("registration_no"):
        data["registration_no"] = load_ctx.first_identifier("Registration No.", "registration_no", "reg_no")
    if not data.get("client_name"):
        data["client_name"] = (load_ctx.overview.clients if load_ctx.overview else "") or ""
    if not data.get("applicant_name"):
        data["applicant_name"] = (load_ctx.overview.applicants if load_ctx.overview else "") or ""
    if include_inhouse_status and not data.get("inhouse_status"):
        data["inhouse_status"] = load_ctx.matter.inhouse_status or ""

    try:
        data = fill_fn(matter_obj=load_ctx.matter, **{fill_arg_name: data})
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=fill_log_context,
            log_key=fill_log_context,
            log_window_seconds=300,
        )

    return _overlay_basic_staff_fields(data, load_ctx.basic_data)


def _load_domestic_patent_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="domestic_patent",
        fill_fn=_fill_dom_patent_from_ipm,
        fill_arg_name="dom_patent",
        fill_log_context="case.general_edit.fill_domestic_patent",
    )


def _load_domestic_design_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="domestic_design",
        fill_fn=_fill_dom_design_from_ipm,
        fill_arg_name="dom_design",
        fill_log_context="case.general_edit.fill_domestic_design",
        include_inhouse_status=True,
    )


def _load_domestic_trademark_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="domestic_trademark",
        fill_fn=_fill_dom_trademark_from_ipm,
        fill_arg_name="dom_tm",
        fill_log_context="case.general_edit.fill_domestic_trademark",
        include_inhouse_status=True,
    )


def _load_incoming_patent_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="incoming_patent",
        fill_fn=_fill_incoming_patent_from_ipm,
        fill_arg_name="inc_patent",
        fill_log_context="case.general_edit.fill_incoming_patent",
        include_inhouse_status=True,
    )


def _load_incoming_design_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="incoming_design",
        fill_fn=_fill_incoming_design_from_ipm,
        fill_arg_name="inc_design",
        fill_log_context="case.general_edit.fill_incoming_design",
        include_inhouse_status=True,
    )


def _load_incoming_trademark_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="incoming_trademark",
        fill_fn=_fill_incoming_trademark_from_ipm,
        fill_arg_name="inc_trademark",
        fill_log_context="case.general_edit.fill_incoming_trademark",
        include_inhouse_status=True,
    )


def _load_outgoing_patent_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="outgoing_patent",
        fill_fn=_fill_outgoing_patent_from_ipm,
        fill_arg_name="out_patent",
        fill_log_context="case.general_edit.fill_outgoing_patent",
        include_inhouse_status=True,
    )


def _load_outgoing_design_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="outgoing_design",
        fill_fn=_fill_outgoing_design_from_ipm,
        fill_arg_name="out_design",
        fill_log_context="case.general_edit.fill_outgoing_design",
        include_inhouse_status=True,
    )


def _load_outgoing_trademark_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="outgoing_trademark",
        fill_fn=_fill_outgoing_trademark_from_ipm,
        fill_arg_name="out_trademark",
        fill_log_context="case.general_edit.fill_outgoing_trademark",
        include_inhouse_status=True,
    )


def _load_pct_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    return _load_registry_edit_data(
        load_ctx,
        namespace="pct",
        fill_fn=_fill_pct_from_ipm,
        fill_arg_name="pct",
        fill_log_context="case.general_edit.fill_pct",
        include_registration_no=False,
        include_inhouse_status=True,
    )


def _load_litigation_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    data = _row_data(load_ctx.custom_rows.get("litigation"))
    if not data.get("client_name"):
        data["client_name"] = (load_ctx.overview.clients if load_ctx.overview else "") or ""
    if not data.get("applicant_registrant"):
        data["applicant_registrant"] = (
            load_ctx.overview.applicants if load_ctx.overview else ""
        ) or ""
    if not data.get("title"):
        data["title"] = load_ctx.matter.right_name or (
            (load_ctx.overview.right_name if load_ctx.overview else "") or ""
        )
    if not data.get("old_our_ref"):
        data["old_our_ref"] = load_ctx.matter.old_our_ref or ""
    return _overlay_basic_staff_fields(data, load_ctx.basic_data)


def _load_misc_edit_data(load_ctx: MatterEditLoadContext) -> dict:
    data = _row_data(load_ctx.custom_rows.get("misc"))
    if not data.get("client_name"):
        data["client_name"] = (load_ctx.overview.clients if load_ctx.overview else "") or ""
    if not data.get("applicant_name"):
        data["applicant_name"] = (load_ctx.overview.applicants if load_ctx.overview else "") or ""
    if not data.get("application_no"):
        data["application_no"] = load_ctx.first_identifier("Application No.", "application_no", "app_no")
    if not data.get("inhouse_status"):
        data["inhouse_status"] = load_ctx.matter.inhouse_status or ""
    return _overlay_basic_staff_fields(data, load_ctx.basic_data)


def _contains_badge_keyword(value: str | None, local_keyword: str, english_keyword: str) -> bool:
    normalized = (value or "").strip()
    if not normalized:
        return False
    if local_keyword and local_keyword in normalized:
        return True
    return english_keyword in normalized.lower()


def _detect_outgoing_badge(
    *,
    active_namespace: str,
    expected_namespace: str,
    active_data: dict,
    identifier_map: dict[str, list[str]],
    matter: Matter,
    overview: VMatterOverview | None,
    app_route_key: str,
    number_key: str,
    date_key: str,
    local_keyword: str,
    english_keyword: str,
) -> bool:
    if active_namespace != expected_namespace:
        return False
    if _contains_badge_keyword(active_data.get(app_route_key), local_keyword, english_keyword):
        return True
    if (active_data.get(number_key) or "").strip():
        return True
    if (active_data.get(date_key) or "").strip():
        return True
    for raw_key, values in (identifier_map or {}).items():
        if not raw_key:
            continue
        key_text = str(raw_key)
        if local_keyword in key_text or english_keyword in key_text.lower():
            if any((value or "").strip() for value in (values or [])):
                return True
    right_name = matter.right_name or (overview.right_name if overview else "") or ""
    return _contains_badge_keyword(right_name, local_keyword, english_keyword)


def _resolve_matter_edit_image_asset(
    *,
    matter_id: str,
    strategy: MatterEditNamespaceStrategy,
    active_data: dict,
):
    if not strategy.apply_image:
        return None
    image_value = (active_data.get("image") or "").strip()
    if not image_value:
        return None
    return _load_linked_file_asset(
        matter_id=str(matter_id),
        file_asset_id=image_value,
        strict_link=False,
    )


def _build_matter_edit_template_context(
    *,
    idempotency_key: str,
    matter: Matter,
    overview: VMatterOverview | None,
    display_division: str,
    display_case_type: str,
    active_namespace: str,
    active_data: dict,
    active_field_layout,
    staff_picker,
    staff_assignment,
    image_asset,
    staff_id_map: dict[str, str],
    recommended_fields,
    field_meta: dict[str, dict],
    missing_fields: list[dict] | None,
    is_copyright: bool,
    is_madrid: bool,
    is_hague: bool,
):
    context = {
        "idempotency_key": idempotency_key,
        "matter": matter,
        "overview": overview,
        "display_division": display_division,
        "display_case_type": display_case_type,
        "is_madrid": is_madrid,
        "is_hague": is_hague,
        "is_copyright": is_copyright,
        "staff_picker": staff_picker,
        "staff_assignment": staff_assignment,
        "image_asset": image_asset,
        "staff_id_map": staff_id_map,
        "recommended_fields": recommended_fields,
        "field_meta": field_meta,
        "missing_fields": missing_fields or [],
    }
    context.update(_build_active_namespace_flags(active_namespace))
    for strategy in _MATTER_EDIT_NAMESPACE_STRATEGIES.values():
        context[strategy.data_key] = active_data if strategy.namespace == active_namespace else {}
        context[f"{strategy.data_key}_fields"] = _field_layout_for_strategy(
            strategy,
            active_namespace=active_namespace,
            active_field_layout=active_field_layout,
        )
    return context


def _capture_matter_edit_snapshot(matter: Matter) -> dict:
    return {
        "our_ref": matter.our_ref,
        "old_our_ref": matter.old_our_ref,
        "your_ref": matter.your_ref,
        "right_name": matter.right_name,
        "status_red": matter.status_red,
        "status_red_related_date": matter.status_red_related_date,
        "status_blue": matter.status_blue,
        "inhouse_status": matter.inhouse_status,
        "memo": matter.memo,
        "retained_at": matter.retained_at if matter.retained_at else None,
        "entered_at": matter.entered_at if matter.entered_at else None,
    }


def _diff_dict(before: dict, after: dict) -> dict:
    patch = {}
    for key in sorted(set(before.keys()) | set(after.keys())):
        if before.get(key) != after.get(key):
            patch[key] = {"before": before.get(key), "after": after.get(key)}
    return patch


def _run_matter_edit_derived(save_ctx: MatterEditSaveContext, label: str, fn) -> None:
    try:
        with db.session.begin_nested():
            fn()
    except Exception as exc:
        logger = _logger_or_none()
        if logger is not None:
            logger.error(
                "Derived update failed (%s) for %s: %s",
                label,
                save_ctx.matter_id,
                exc,
            )
        save_ctx.derived_errors.append(label)


def _update_matter_custom_namespace(
    save_ctx: MatterEditSaveContext,
    strategy: MatterEditNamespaceStrategy,
    allowed_keys,
) -> dict:
    row = save_ctx.custom_rows.get(strategy.namespace)
    if not row:
        row = MatterCustomField(
            matter_id=str(save_ctx.matter_id), namespace=strategy.namespace, data={}
        )
        db.session.add(row)
        save_ctx.custom_rows[strategy.namespace] = row

    before = dict(row.data or {})
    data = dict(before)

    _log_custom_field_filtering(
        matter_id=str(save_ctx.matter_id),
        namespace=strategy.namespace,
        form_data=save_ctx.request_form,
        allowed_keys=allowed_keys,
    )
    updates = _validate_custom_field_updates(
        matter_id=str(save_ctx.matter_id),
        namespace=strategy.namespace,
        form_data=save_ctx.request_form,
        allowed_keys=allowed_keys,
        strict_dates=save_ctx.strict_dates,
    )
    data.update(updates)

    from app.services.deadlines.exam_request_rules import (
        apply_exam_request_date_default_when_requested,
        apply_out_exam_request_defaults,
    )

    apply_out_exam_request_defaults(
        data,
        division=save_ctx.div,
        case_type=save_ctx.typ,
        allowed_keys=set(allowed_keys),
    )
    apply_exam_request_date_default_when_requested(
        data,
        allowed_keys=set(allowed_keys),
    )
    _clear_duplicate_appeal_no(data)

    if strategy.carry_matter_inhouse_status and not data.get("inhouse_status"):
        if (save_ctx.matter.inhouse_status or "").strip():
            data["inhouse_status"] = save_ctx.matter.inhouse_status or ""
    if save_ctx.right_name:
        data["proposal_title"] = save_ctx.right_name
    if strategy.apply_image:
        _attach_image_file_asset(
            matter_id=str(save_ctx.matter_id),
            file=save_ctx.image_file,
            data=data,
        )

    row.data = data
    patch = _diff_dict(before, data)
    if patch:
        save_ctx.custom_changes[strategy.namespace] = patch
    return data


def _save_active_namespace_data(
    strategy: MatterEditNamespaceStrategy,
    *,
    save_ctx: MatterEditSaveContext,
    allowed_keys,
) -> dict:
    data = _update_matter_custom_namespace(
        save_ctx,
        strategy,
        allowed_keys,
    )

    if strategy.identifier_sync and strategy.identifier_arg_name:

        def _sync_identifiers() -> None:
            strategy.identifier_sync(
                matter_id=str(save_ctx.matter_id),
                **{strategy.identifier_arg_name: data},
            )
            if strategy.sync_party_roles:
                _sync_matter_party_roles(matter_id=str(save_ctx.matter_id), data=data)

        _run_matter_edit_derived(save_ctx, "identifiers", _sync_identifiers)

    if strategy.event_sync and strategy.event_arg_name:
        _run_matter_edit_derived(
            save_ctx,
            "events",
            lambda: strategy.event_sync(
                matter_id=str(save_ctx.matter_id),
                **{strategy.event_arg_name: data},
            ),
        )

    auto_status_kwargs = {"matter": save_ctx.matter}
    if strategy.auto_status_payload_name:
        auto_status_kwargs[strategy.auto_status_payload_name] = data
    _run_matter_edit_derived(
        save_ctx,
        "auto_status",
        lambda: _apply_auto_status_from_db(**auto_status_kwargs),
    )
    return data


def _sync_core_dockets_from_custom_data(matter_id: str, data: dict) -> None:
    if not isinstance(data, dict):
        return

    today_token = date.today().isoformat()
    touched_refs: list[str] = []

    def _token(*keys: str) -> str:
        for key in keys:
            if key not in data:
                continue
            value = _date_only_str(data.get(key))
            if value:
                return value
        return ""

    filing_due = _token("filing_deadline")
    filing_due_type = str(data.get("filing_deadline_type") or "").strip()
    filing_done = _token("application_date", "filing_date")
    if filing_due:
        upsert_filing_docket(
            str(matter_id),
            filing_due,
            deadline_type=filing_due_type or None,
            commit=False,
        )
        touched_refs.extend(["Filing", "Filing (Process)", "MGMT:FILING"])
    if filing_done:
        complete_filing_docket(str(matter_id), filing_done, commit=False)
        touched_refs.extend(["Filing", "Filing (Process)", "MGMT:FILING", "MGMT:STATUS_RED:FilingDeadline"])

    exam_due = _token("exam_deadline", "exam_request_deadline")
    exam_done = _token("exam_request_date")
    has_exam_field = ("exam_deadline" in data) or ("exam_request_deadline" in data)
    if exam_due:
        upsert_exam_request_docket(str(matter_id), exam_due, commit=False)
        touched_refs.extend(["Examination request", "Examination request (Process)", "MGMT:EXAM_REQUEST"])
    if exam_done:
        complete_exam_request_docket(str(matter_id), exam_done, commit=False)
        touched_refs.extend(
            ["Examination request", "Examination request (Process)", "MGMT:EXAM_REQUEST", "MGMT:STATUS_RED:Examination requestDeadline"]
        )
    elif has_exam_field and not exam_due:
        complete_exam_request_docket(str(matter_id), f"AUTO_CANCELLED:{today_token}", commit=False)
        touched_refs.extend(
            ["Examination request", "Examination request (Process)", "MGMT:EXAM_REQUEST", "MGMT:STATUS_RED:Examination requestDeadline"]
        )

    reg_due = _token("reg_deadline")
    reg_done = _token("registration_date")
    has_reg_field = "reg_deadline" in data
    if reg_due:
        upsert_registration_docket(str(matter_id), reg_due, commit=False)
        touched_refs.extend(["Registration", "Registration (Process)", "MGMT:REGISTRATION"])
    if reg_done:
        complete_registration_docket(str(matter_id), reg_done, commit=False)
        touched_refs.extend(
            ["Registration", "Registration (Process)", "MGMT:REGISTRATION", "MGMT:STATUS_RED:RegistrationDeadline"]
        )
    elif has_reg_field and not reg_due:
        complete_registration_docket(str(matter_id), f"AUTO_CANCELLED:{today_token}", commit=False)
        touched_refs.extend(
            ["Registration", "Registration (Process)", "MGMT:REGISTRATION", "MGMT:STATUS_RED:RegistrationDeadline"]
        )

    _sync_touched_core_dockets_now(str(matter_id), touched_refs)


def _sync_touched_core_dockets_now(matter_id: str, refs: list[str]) -> None:
    clean_refs = [ref for ref in dict.fromkeys(refs) if ref]
    if not matter_id or not clean_refs:
        return
    try:
        rows = (
            DocketItem.query.filter(DocketItem.matter_id == str(matter_id))
            .filter(DocketItem.name_ref.in_(clean_refs))
            .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
            .order_by(DocketItem.name_ref.asc(), DocketItem.docket_id.asc())
            .all()
        )
        rank = {ref: idx for idx, ref in enumerate(clean_refs)}
        for docket_item in sorted(
            rows,
            key=lambda item: (
                rank.get((getattr(item, "name_ref", None) or "").strip(), 999),
                (getattr(item, "name_ref", None) or "").strip(),
                (getattr(item, "docket_id", None) or "").strip(),
            ),
        ):
            sync_from_docket_item(docket_item=docket_item, actor_id=None)
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.routes.general_edit._sync_touched_core_dockets_now",
            log_key="case.routes.general_edit._sync_touched_core_dockets_now",
            log_window_seconds=300,
        )


_MATTER_EDIT_NAMESPACE_STRATEGIES = {
    "domestic_patent": MatterEditNamespaceStrategy(
        namespace="domestic_patent",
        data_key="dom_patent",
        field_layout_fallback=DOMESTIC_PATENT_FIELDS,
        allowed_keys_fallback=DOMESTIC_PATENT_ALLOWED_KEYS,
        loader=_load_domestic_patent_edit_data,
        identifier_sync=_sync_matter_identifiers_from_dom_patent,
        identifier_arg_name="dom_patent",
        sync_party_roles=True,
        event_sync=_sync_matter_events_from_dom_patent,
        event_arg_name="dom_patent",
        auto_status_payload_name="dom_patent",
    ),
    "domestic_design": MatterEditNamespaceStrategy(
        namespace="domestic_design",
        data_key="dom_design",
        field_layout_fallback=DOMESTIC_DESIGN_FIELDS,
        allowed_keys_fallback=DOMESTIC_DESIGN_ALLOWED_KEYS,
        loader=_load_domestic_design_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_dom_design,
        identifier_arg_name="dom_design",
        event_sync=_sync_matter_events_from_dom_design,
        event_arg_name="dom_design",
    ),
    "domestic_trademark": MatterEditNamespaceStrategy(
        namespace="domestic_trademark",
        data_key="dom_trademark",
        field_layout_fallback=DOMESTIC_TRADEMARK_FIELDS,
        allowed_keys_fallback=DOMESTIC_TRADEMARK_ALLOWED_KEYS,
        loader=_load_domestic_trademark_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_dom_trademark,
        identifier_arg_name="dom_trademark",
        event_sync=_sync_matter_events_from_dom_trademark,
        event_arg_name="dom_trademark",
    ),
    "incoming_patent": MatterEditNamespaceStrategy(
        namespace="incoming_patent",
        data_key="inc_patent",
        field_layout_fallback=INCOMING_PATENT_FIELDS,
        allowed_keys_fallback=INCOMING_PATENT_ALLOWED_KEYS,
        loader=_load_incoming_patent_edit_data,
        identifier_sync=_sync_matter_identifiers_from_inc_patent,
        identifier_arg_name="inc_patent",
        event_sync=_sync_matter_events_from_inc_patent,
        event_arg_name="inc_patent",
        carry_matter_inhouse_status=True,
    ),
    "incoming_design": MatterEditNamespaceStrategy(
        namespace="incoming_design",
        data_key="inc_design",
        field_layout_fallback=INCOMING_DESIGN_FIELDS,
        allowed_keys_fallback=INCOMING_DESIGN_ALLOWED_KEYS,
        loader=_load_incoming_design_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_inc_design,
        identifier_arg_name="inc_design",
        event_sync=_sync_matter_events_from_inc_design,
        event_arg_name="inc_design",
    ),
    "incoming_trademark": MatterEditNamespaceStrategy(
        namespace="incoming_trademark",
        data_key="inc_trademark",
        field_layout_fallback=INCOMING_TRADEMARK_FIELDS,
        allowed_keys_fallback=INCOMING_TRADEMARK_ALLOWED_KEYS,
        loader=_load_incoming_trademark_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_inc_trademark,
        identifier_arg_name="inc_tm",
        event_sync=_sync_matter_events_from_inc_trademark,
        event_arg_name="inc_tm",
        carry_matter_inhouse_status=True,
    ),
    "outgoing_patent": MatterEditNamespaceStrategy(
        namespace="outgoing_patent",
        data_key="out_patent",
        field_layout_fallback=OUTGOING_PATENT_FIELDS,
        allowed_keys_fallback=OUTGOING_PATENT_ALLOWED_KEYS,
        loader=_load_outgoing_patent_edit_data,
        identifier_sync=_sync_matter_identifiers_from_out_patent,
        identifier_arg_name="out_patent",
        event_sync=_sync_matter_events_from_out_patent,
        event_arg_name="out_patent",
        carry_matter_inhouse_status=True,
    ),
    "outgoing_design": MatterEditNamespaceStrategy(
        namespace="outgoing_design",
        data_key="out_design",
        field_layout_fallback=OUTGOING_DESIGN_FIELDS,
        allowed_keys_fallback=OUTGOING_DESIGN_ALLOWED_KEYS,
        loader=_load_outgoing_design_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_out_design,
        identifier_arg_name="out_design",
        event_sync=_sync_matter_events_from_out_design,
        event_arg_name="out_design",
    ),
    "outgoing_trademark": MatterEditNamespaceStrategy(
        namespace="outgoing_trademark",
        data_key="out_trademark",
        field_layout_fallback=OUTGOING_TRADEMARK_FIELDS,
        allowed_keys_fallback=OUTGOING_TRADEMARK_ALLOWED_KEYS,
        loader=_load_outgoing_trademark_edit_data,
        apply_image=True,
        identifier_sync=_sync_matter_identifiers_from_out_trademark,
        identifier_arg_name="out_tm",
        event_sync=_sync_matter_events_from_out_trademark,
        event_arg_name="out_tm",
        carry_matter_inhouse_status=True,
    ),
    "pct": MatterEditNamespaceStrategy(
        namespace="pct",
        data_key="pct",
        field_layout_fallback=PCT_FIELDS,
        allowed_keys_fallback=PCT_ALLOWED_KEYS,
        loader=_load_pct_edit_data,
        identifier_sync=_sync_matter_identifiers_from_pct,
        identifier_arg_name="pct",
        sync_party_roles=True,
        event_sync=_sync_matter_events_from_pct,
        event_arg_name="pct",
        carry_matter_inhouse_status=True,
    ),
    "litigation": MatterEditNamespaceStrategy(
        namespace="litigation",
        data_key="litigation",
        field_layout_fallback=LITIGATION_FIELDS,
        allowed_keys_fallback=LITIGATION_ALLOWED_KEYS,
        loader=_load_litigation_edit_data,
        event_sync=_sync_matter_events_from_litigation,
        event_arg_name="litigation",
    ),
    "misc": MatterEditNamespaceStrategy(
        namespace="misc",
        data_key="misc",
        field_layout_fallback=MISC_FIELDS,
        allowed_keys_fallback=MISC_ALLOWED_KEYS,
        loader=_load_misc_edit_data,
    ),
}


@bp.route("/matter/<matter_id>/edit", methods=["GET", "POST"])
@login_required
def edit_matter(matter_id: str):
    matter = Matter.query.get_or_404(matter_id)
    # This is an edit form; enforce edit permission even for GET.
    require_matter_access(str(matter.matter_id), action="edit_case")
    # Full-page-only form: prevent boosted/partial navigation from skipping page boot scripts.
    hx_redirect = _hx_hard_redirect_response(
        "case_work.edit_matter",
        matter_id=matter_id,
        division=request.args.get("division"),
        type=request.args.get("type"),
    )
    if hx_redirect is not None:
        return hx_redirect
    overview = VMatterOverview.query.get(matter_id)
    staff_picker = _build_staff_picker_context()
    form = CaseForm()
    idempotency_key = (
        request.headers.get("Idempotency-Key") or request.form.get("idempotency_key") or ""
    ).strip()
    if request.method == "GET" and not idempotency_key:
        idempotency_key = uuid.uuid4().hex
    form.idempotency_key.data = idempotency_key
    custom_rows = _load_matter_custom_rows(str(matter_id))
    basic_data = _row_data(custom_rows.get("basic"))
    staff_id_map = _build_staff_id_map_for_matter(str(matter_id))
    identifier_map = _load_identifier_map(str(matter_id))
    div, typ = _infer_case_kind(matter, overview)
    storage_div, storage_typ = resolve_public_case_kind_for_matter(matter, overview)
    resolved_profile, active_field_layout, active_allowed_keys, field_meta = (
        _load_matter_edit_field_meta(storage_div, storage_typ)
    )
    is_copyright = storage_div == "ETC" and storage_typ == "COPYRIGHT"
    namespace_resolution = _resolve_active_edit_namespace(
        resolved_profile=resolved_profile,
        div=div,
        typ=typ,
        custom_rows=custom_rows,
    )
    active_namespace = namespace_resolution.active_namespace
    if not active_namespace:
        if len(namespace_resolution.fallback_candidates) > 1:
            current_app.logger.warning(
                "edit_matter: multiple custom-field namespaces exist for matter_id=%s: %s",
                str(matter_id),
                ",".join(namespace_resolution.fallback_candidates),
            )
        current_app.logger.error(
            "edit_matter: unable to resolve namespace for matter_id=%s",
            str(matter_id),
        )
        abort(400, "Ambiguous matter type. Please contact an administrator.")

    strategy = _MATTER_EDIT_NAMESPACE_STRATEGIES[active_namespace]
    load_ctx = MatterEditLoadContext(
        matter_id=str(matter_id),
        matter=matter,
        overview=overview,
        basic_data=basic_data,
        custom_rows=custom_rows,
        identifier_map=identifier_map,
    )
    active_data = strategy.loader(load_ctx)
    staff_assignment = _build_staff_assignment_context()

    is_madrid = False
    try:
        is_madrid = _detect_outgoing_badge(
            active_namespace=active_namespace,
            expected_namespace="outgoing_trademark",
            active_data=active_data,
            identifier_map=identifier_map,
            matter=matter,
            overview=overview,
            app_route_key="app_route",
            number_key="madrid_application_no",
            date_key="madrid_application_date",
            local_keyword="",
            english_keyword="madrid",
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.general_edit.is_madrid",
            log_key="case.general_edit.is_madrid",
            log_window_seconds=300,
        )

    is_hague = False
    try:
        is_hague = _detect_outgoing_badge(
            active_namespace=active_namespace,
            expected_namespace="outgoing_design",
            active_data=active_data,
            identifier_map=identifier_map,
            matter=matter,
            overview=overview,
            app_route_key="app_route",
            number_key="hague_application_no",
            date_key="hague_application_date",
            local_keyword="",
            english_keyword="hague",
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.general_edit.is_hague",
            log_key="case.general_edit.is_hague",
            log_window_seconds=300,
        )

    image_asset = _resolve_matter_edit_image_asset(
        matter_id=str(matter_id),
        strategy=strategy,
        active_data=active_data,
    )
    status_red_for_recommendation = (
        (matter.status_red or "") or ((overview.status_red or "") if overview else "") or ""
    ).strip()
    recommended_fields = EditRecommendationService.get_recommended_fields(
        status_red_for_recommendation
    )

    def _render_edit(*, missing_fields: list[dict] | None = None):
        return render_template(
            "case/matter_edit.html",
            **_build_matter_edit_template_context(
                idempotency_key=idempotency_key,
                matter=matter,
                overview=overview,
                display_division=storage_div,
                display_case_type=storage_typ,
                active_namespace=active_namespace,
                active_data=active_data,
                active_field_layout=active_field_layout,
                staff_picker=staff_picker,
                staff_assignment=staff_assignment,
                image_asset=image_asset,
                staff_id_map=staff_id_map,
                recommended_fields=recommended_fields,
                field_meta=field_meta,
                missing_fields=missing_fields,
                is_copyright=is_copyright,
                is_madrid=is_madrid,
                is_hague=is_hague,
            ),
        )

    if request.method == "POST":
        original_our_ref = _normalize_our_ref_input(matter.our_ref)
        our_ref = _normalize_our_ref_input(request.form.get("our_ref"))
        old_our_ref = (request.form.get("old_our_ref") or "").strip()
        your_ref = (request.form.get("your_ref") or "").strip()
        right_name = (request.form.get("right_name") or "").strip()
        inhouse_status = (request.form.get("inhouse_status") or "").strip()
        retained_at = _normalize_date_input(request.form.get("retained_at"), "Engagement date")
        entered_at = _normalize_date_input(request.form.get("entered_at"), "Entry date")
        memo = (request.form.get("memo") or "").strip()
        image_file = request.files.get("image_file")

        if not our_ref:
            flash("Our Ref. Required.", "warning")
            return _render_edit(
                missing_fields=[
                    {"key": "our_ref", "label": "Our Ref."},
                ]
            )

        if (
            strategy.apply_image
            and image_file
            and (image_file.filename or "").strip()
            and not _is_allowed_image_upload(image_file)
        ):
            flash("/Image Only image files can be uploaded.", "warning")
            return _render_edit()

        if our_ref and our_ref != original_our_ref:
            exists = Matter.query.filter_by(our_ref=our_ref).first()
            if exists and str(exists.matter_id) != str(matter_id):
                flash("  Our Ref. .", "danger")
                return _render_edit()

        if retained_at is None or entered_at is None:
            return _render_edit()

        op, created = reserve_operation(
            "case.update",
            request_id=idempotency_key or None,
            actor_id=getattr(current_user, "id", None),
            risk_level="LOW",
            targets_json={"matter_id": str(matter_id), "context": "edit_matter"},
            summary_json={"actor_type": "user"},
        )

        if op and not created and op.status == "applied":
            flash(" Process .", "warning")
            return redirect(url_for("case_work.case_detail", case_id=matter_id))

        matter_before = _capture_matter_edit_snapshot(matter)
        custom_changes = {}
        derived_errors = []

        strict_dates = bool(current_app.config.get("CASE_STRICT_DATE_VALIDATION", True))
        save_ctx = MatterEditSaveContext(
            matter_id=str(matter_id),
            matter=matter,
            div=div,
            typ=typ,
            request_form=request.form,
            image_file=image_file,
            right_name=right_name,
            strict_dates=strict_dates,
            custom_rows=custom_rows,
            custom_changes=custom_changes,
            derived_errors=derived_errors,
        )
        try:
            from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

            matter.our_ref = our_ref
            matter.old_our_ref = old_our_ref or None
            matter.your_ref = your_ref or None
            matter.right_name = right_name or None
            matter.inhouse_status = inhouse_status or None
            matter.memo = memo or None
            if retained_at:
                matter.retained_at = retained_at
            if entered_at:
                matter.entered_at = entered_at

            _apply_case_kind_to_matter(matter, storage_div or div, storage_typ or typ)
            _update_basic_matter_info(str(matter_id), request.form)
            active_custom_data = _save_active_namespace_data(
                strategy,
                save_ctx=save_ctx,
                allowed_keys=_allowed_keys_for_strategy(
                    strategy,
                    active_namespace=active_namespace,
                    active_allowed_keys=active_allowed_keys,
                ),
            )
            _run_matter_edit_derived(
                save_ctx,
                "core_dockets",
                lambda: _sync_core_dockets_from_custom_data(str(matter_id), active_custom_data),
            )
            _run_matter_edit_derived(
                save_ctx,
                "mgmt_deadlines",
                lambda: ensure_mgmt_deadlines_for_matter(str(matter_id), commit=False),
            )
            _run_matter_edit_derived(
                save_ctx,
                "auto_status_post_deadlines",
                lambda: _apply_auto_status_from_db(
                    matter=matter, deadline_refresh=active_custom_data
                ),
            )

            matter_after = _capture_matter_edit_snapshot(matter)
            matter_patch = _diff_dict(matter_before, matter_after)
            if op and op.id:
                if matter_patch:
                    db.session.add(
                        OperationChange(
                            operation_id=op.id,
                            entity_type="matter",
                            entity_id=str(matter_id),
                            change_type="update",
                            patch_json=matter_patch,
                        )
                    )
                for namespace, patch in custom_changes.items():
                    db.session.add(
                        OperationChange(
                            operation_id=op.id,
                            entity_type="matter_custom_field",
                            entity_id=f"{matter_id}:{namespace}",
                            change_type="update",
                            patch_json=patch,
                            meta_json={"namespace": namespace},
                        )
                    )
                summary_updates = {
                    "matter_id": str(matter_id),
                    "request_id": idempotency_key or getattr(g, "request_id", None),
                    "actor_type": "user",
                }
                if matter_patch:
                    summary_updates["matter_fields_changed"] = sorted(matter_patch.keys())
                if custom_changes:
                    summary_updates["custom_namespaces"] = sorted(custom_changes.keys())
                if derived_errors:
                    summary_updates["derived_errors"] = derived_errors
                mark_operation_applied(op, summary_updates=summary_updates)

            db.session.commit()
            apply_case_status_side_effects(
                matter_id=str(matter_id),
                old_status=terminal_case_status_value(
                    None,
                    inhouse_status=matter_before.get("inhouse_status"),
                    status_blue=matter_before.get("status_blue"),
                    status_red=matter_before.get("status_red"),
                    status_red_related_date=matter_before.get("status_red_related_date"),
                ),
                new_status=terminal_case_status_value(
                    None,
                    inhouse_status=matter_after.get("inhouse_status"),
                    status_blue=matter_after.get("status_blue"),
                    status_red=matter_after.get("status_red"),
                    status_red_related_date=matter_after.get("status_red_related_date"),
                ),
                actor_id=getattr(current_user, "id", None),
                logger_override=current_app.logger,
            )
            try:
                upsert_case_flat_index(str(matter_id))
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                current_app.logger.warning(
                    "Case flat index update failed for %s: %s", matter_id, exc
                )
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "warning")
            return _render_edit()
        except IntegrityError:
            db.session.rollback()
            flash("  Our Ref. .", "danger")
            return _render_edit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("edit_matter POST failed")
            flash("Save In Progress Error .", "danger")
            return _render_edit()

        flash("Matter Edit.", "success")
        return redirect(url_for("case_work.case_detail", case_id=matter_id))

    return _render_edit()


@bp.route("/<int:case_id>/edit", methods=["GET", "POST"])
@login_required
def edit(case_id):
    obj = Case.query.get_or_404(case_id)
    if not can_manage_case_globally(current_user) and getattr(current_user, "id", None) not in (
        obj.manager_id,
        obj.attorney_id,
    ):
        abort(403, "You do not have permission to edit this matter.")
    form = CaseForm()
    idempotency_key = (
        request.headers.get("Idempotency-Key") or request.form.get("idempotency_key") or ""
    ).strip()
    if request.method == "GET" and not idempotency_key:
        idempotency_key = uuid.uuid4().hex
    form.idempotency_key.data = idempotency_key

    # Populate choices
    try:
        from app.services.core.staff_options import build_staff_assignment_lists

        users = build_staff_assignment_lists().get("all_users") or []
    except Exception:
        users = []

    def _label(u):
        # Format: DisplayName(Staff code)
        if u.display_name and u.username:
            return f"{u.display_name}({u.username})"
        elif u.display_name:
            return u.display_name
        else:
            return u.username or u.email or f"User#{u.id}"

    attorney_choices = [(u.id, _label(u)) for u in users]
    manager_choices = [
        (u.id, _label(u)) for u in users if (u.role or "").lower() in ("admin", "manager")
    ]
    if not manager_choices:
        manager_choices = attorney_choices
    form.attorney_id.choices = attorney_choices
    form.manager_id.choices = manager_choices

    if request.method == "GET":
        # Pre-fill from existing case
        form.our_ref.data = obj.ref_no
        form.client_ref.data = (
            (obj.extended_info or {}).get("client_ref") if obj.extended_info else None
        )
        form.category.data = obj.case_type
        form.country.data = obj.country
        form.in_out_type.data = obj.division
        form.filing_date.data = obj.filing_date
        form.filing_no.data = obj.app_no
        form.title.data = obj.title
        form.client_id.data = str(obj.client_id or "")
        form.client_name.data = obj.client.name if obj.client else ""
        form.reg_date.data = None
        form.reg_no.data = None
        form.attorney_id.data = obj.attorney_id
        form.manager_id.data = obj.manager_id or (
            current_user.id if current_user.is_authenticated else None
        )
        form.summary.data = (obj.extended_info or {}).get("summary") if obj.extended_info else None

    if form.validate_on_submit():
        op, created = reserve_operation(
            "case.update.legacy",
            request_id=idempotency_key or None,
            actor_id=getattr(current_user, "id", None),
            risk_level="LOW",
            targets_json={"case_id": str(obj.id), "context": "edit"},
            summary_json={"actor_type": "user"},
        )
        if op and not created and op.status == "applied":
            flash(" Process .", "warning")
            target_matter_id = resolve_matter_id_for_case_ref(getattr(obj, "ref_no", None))
            if target_matter_id:
                return redirect(url_for("case_work.case_detail", case_id=target_matter_id))
            return redirect(url_for("case_work.edit", case_id=obj.id))

        case_before = {
            "ref_no": obj.ref_no,
            "case_type": obj.case_type,
            "division": obj.division,
            "country": obj.country,
            "app_no": obj.app_no,
            "title": obj.title,
            "client_id": obj.client_id,
            "attorney_id": obj.attorney_id,
            "manager_id": obj.manager_id,
            "filing_date": obj.filing_date.isoformat() if obj.filing_date else None,
            "reg_date": obj.reg_date.isoformat() if obj.reg_date else None,
            "reg_no": obj.reg_no,
        }

        def _diff_dict(before, after):
            patch = {}
            for key in sorted(set(before.keys()) | set(after.keys())):
                if before.get(key) != after.get(key):
                    patch[key] = {"before": before.get(key), "after": after.get(key)}
            return patch

        try:
            with db.session.begin():
                _save_case_data(obj, form, request.form)
                _save_foreign_info(obj, request.form, commit=False)

                case_after = {
                    "ref_no": obj.ref_no,
                    "case_type": obj.case_type,
                    "division": obj.division,
                    "country": obj.country,
                    "app_no": obj.app_no,
                    "title": obj.title,
                    "client_id": obj.client_id,
                    "attorney_id": obj.attorney_id,
                    "manager_id": obj.manager_id,
                    "filing_date": obj.filing_date.isoformat() if obj.filing_date else None,
                    "reg_date": obj.reg_date.isoformat() if obj.reg_date else None,
                    "reg_no": obj.reg_no,
                }
                case_patch = _diff_dict(case_before, case_after)
                if op and op.id:
                    if case_patch:
                        db.session.add(
                            OperationChange(
                                operation_id=op.id,
                                entity_type="case",
                                entity_id=str(obj.id),
                                change_type="update",
                                patch_json=case_patch,
                            )
                        )
                    summary_updates = {
                        "case_id": str(obj.id),
                        "request_id": idempotency_key or getattr(g, "request_id", None),
                        "actor_type": "user",
                    }
                    if case_patch:
                        summary_updates["case_fields_changed"] = sorted(case_patch.keys())
                    mark_operation_applied(op, summary_updates=summary_updates)
        except IntegrityError:
            flash("  Our Ref. .", "danger")
        except Exception:
            current_app.logger.exception("legacy case edit failed")
            flash("Save In Progress Error .", "danger")
        else:
            flash("Matter Edit.", "success")
            target_matter_id = resolve_matter_id_for_case_ref(getattr(obj, "ref_no", None))
            if target_matter_id:
                return redirect(url_for("case_work.case_detail", case_id=target_matter_id))
            return redirect(url_for("case_work.edit", case_id=obj.id))

    # Determine template
    template = _get_special_template(obj.division, obj.case_type)
    return render_template(
        template,
        form=form,
        case=obj,
        form_action=url_for("case_work.edit", case_id=obj.id),
        # Pass context variables for edit template
        is_domestic_patent=is_domestic_patent,
        is_domestic_design=is_domestic_design,
        is_domestic_trademark=is_domestic_trademark,
        is_incoming_patent=is_incoming_patent,
        is_incoming_design=is_incoming_design,
        is_incoming_trademark=is_incoming_trademark,
        is_outgoing_patent=is_outgoing_patent,
        is_outgoing_design=is_outgoing_design,
        is_outgoing_trademark=is_outgoing_trademark,
        is_litigation=is_litigation,
        dom_patent=dom_patent,
        dom_design=dom_design,
        dom_trademark=dom_trademark,
        inc_patent=inc_patent,
        inc_design=inc_design,
        inc_trademark=inc_trademark,
        out_patent=out_patent,
        out_design=out_design,
        out_trademark=out_trademark,
        litigation=litigation,
        dom_patent_fields=DOMESTIC_PATENT_FIELDS,
        dom_design_fields=DOMESTIC_DESIGN_FIELDS,
        dom_trademark_fields=DOMESTIC_TRADEMARK_FIELDS,
        inc_patent_fields=INCOMING_PATENT_FIELDS,
        inc_design_fields=INCOMING_DESIGN_FIELDS,
        inc_trademark_fields=INCOMING_TRADEMARK_FIELDS,
        out_patent_fields=OUTGOING_PATENT_FIELDS,
        out_design_fields=OUTGOING_DESIGN_FIELDS,
        out_trademark_fields=OUTGOING_TRADEMARK_FIELDS,
        litigation_fields=LITIGATION_FIELDS,
        staff_picker=staff_picker,
        staff_assignment=staff_assignment,
    )


@bp.route("/<int:case_id>/delete", methods=["POST"])
@matter_action("delete_case")
@login_required
def delete(case_id):
    if not can_manage_case_globally(current_user):
        abort(403, "You do not have permission to edit this matter.")
    idempotency_key = (
        request.headers.get("Idempotency-Key") or request.form.get("idempotency_key") or ""
    ).strip()

    # 1. Delete New Case (if exists)
    obj = Case.query.get(case_id)

    # 2. Delete Matter (New System) and Cascade
    matter_id = None
    if obj and (getattr(obj, "ref_no", None) or "").strip():
        matter_id = resolve_matter_id_for_case_ref(obj.ref_no)
    if not matter_id:
        candidate = str(case_id)
        try:
            if Matter.query.get(candidate):
                matter_id = candidate
        except Exception:
            matter_id = None

    op, created = reserve_operation(
        "case.delete",
        request_id=idempotency_key or None,
        actor_id=getattr(current_user, "id", None),
        risk_level="HIGH",
        targets_json={"case_id": str(case_id), "matter_id": matter_id, "context": "delete"},
        summary_json={"actor_type": "user"},
    )
    if op and not created and op.status == "applied":
        flash(" Process .", "warning")
        return redirect(url_for("case_work.case_list"))

    all_asset_ids = set()
    try:
        # reserve_operation() may already have started the ambient transaction.
        # Use a nested transaction here to avoid "transaction already begun" errors.
        with db.session.begin_nested():
            if obj:
                db.session.execute(
                    text(
                        """
                        DELETE FROM reminders
                        WHERE deadline_id IN (
                            SELECT id FROM deadlines WHERE case_id = :case_id
                        )
                        """
                    ),
                    {"case_id": int(obj.id)},
                )
                db.session.delete(obj)

            if not matter_id:
                if op and op.id:
                    mark_operation_applied(
                        op,
                        summary_updates={
                            "case_id": str(case_id),
                            "request_id": idempotency_key or getattr(g, "request_id", None),
                            "actor_type": "user",
                        },
                    )
            else:
                # Collect FileAsset IDs to potentially GC (Garbage Collect)
                # From MatterFileAsset
                mfa_assets = (
                    db.session.execute(
                        text("SELECT file_asset_id FROM matter_file_asset WHERE matter_id = :mid"),
                        {"mid": matter_id},
                    )
                    .scalars()
                    .all()
                )

                # From CommunicationFileAsset (via Communication)
                cfa_assets = (
                    db.session.execute(
                        text(
                            """
                SELECT cfa.file_asset_id
                FROM communication_file_asset cfa
                JOIN communication c ON c.comm_id = cfa.comm_id
                WHERE c.matter_id = :mid
            """
                        ),
                        {"mid": matter_id},
                    )
                    .scalars()
                    .all()
                )

                # From OfficeActionFileAsset (via OfficeAction)
                ofa_assets = (
                    db.session.execute(
                        text(
                            """
                SELECT ofa.file_asset_id
                FROM office_action_file_asset ofa
                JOIN office_action oa ON oa.oa_id = ofa.oa_id
                WHERE oa.matter_id = :mid
            """
                        ),
                        {"mid": matter_id},
                    )
                    .scalars()
                    .all()
                )

                # From MatterMemoFileAsset (via MatterMemo)
                mmfa_assets = (
                    db.session.execute(
                        text(
                            """
                SELECT mmfa.file_asset_id
                FROM matter_memo_file_asset mmfa
                JOIN matter_memo mm ON mm.id = mmfa.memo_id
                WHERE mm.matter_id = :mid
            """
                        ),
                        {"mid": matter_id},
                    )
                    .scalars()
                    .all()
                )

                all_asset_ids = (
                    set(mfa_assets) | set(cfa_assets) | set(ofa_assets) | set(mmfa_assets)
                )

                # --- DELETE DEPENDENTS ---

                # 1. Links to FileAssets
                db.session.execute(
                    text("DELETE FROM matter_file_asset WHERE matter_id = :mid"), {"mid": matter_id}
                )

                db.session.execute(
                    text(
                        """
            DELETE FROM communication_file_asset
            WHERE comm_id IN (SELECT comm_id FROM communication WHERE matter_id = :mid)
        """
                    ),
                    {"mid": matter_id},
                )

                db.session.execute(
                    text(
                        """
            DELETE FROM office_action_file_asset
            WHERE oa_id IN (SELECT oa_id FROM office_action WHERE matter_id = :mid)
        """
                    ),
                    {"mid": matter_id},
                )

                db.session.execute(
                    text(
                        """
            DELETE FROM matter_memo_file_asset
            WHERE memo_id IN (SELECT id FROM matter_memo WHERE matter_id = :mid)
        """
                    ),
                    {"mid": matter_id},
                )

                # 2. Functional Records
                db.session.execute(
                    text("DELETE FROM communication WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM office_action WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM docket_item WHERE matter_id = :mid"), {"mid": matter_id}
                )
                # Future-proof: remove any direct FK children of workflows discovered from DB metadata.
                delete_workflow_fk_children_for_matter(matter_id)
                # workflows      FK  .
                db.session.execute(
                    text(
                        """
            DELETE FROM workflow_checklist_item
            WHERE workflow_id IN (SELECT id FROM workflows WHERE case_id = :mid)
        """
                    ),
                    {"mid": matter_id},
                )
                db.session.execute(
                    text(
                        """
            DELETE FROM workflow_reminder_sent
            WHERE workflow_id IN (SELECT id FROM workflows WHERE case_id = :mid)
        """
                    ),
                    {"mid": matter_id},
                )
                db.session.execute(
                    text("DELETE FROM workflows WHERE case_id = :mid"), {"mid": matter_id}
                )

                # 3. Matter Details
                db.session.execute(
                    text("DELETE FROM matter_identifier WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM matter_event WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM matter_custom_field WHERE matter_id = :mid"),
                    {"mid": matter_id},
                )
                db.session.execute(
                    text("DELETE FROM matter_party_role WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM matter_staff_assignment WHERE matter_id = :mid"),
                    {"mid": matter_id},
                )
                db.session.execute(
                    text("DELETE FROM matter_memo WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM matter_progress WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM matter_family WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM case_flat_index WHERE matter_id = :mid"), {"mid": matter_id}
                )
                db.session.execute(
                    text("DELETE FROM external_invoice_case_link WHERE matter_id = :mid"),
                    {"mid": matter_id},
                )
                db.session.execute(
                    text("DELETE FROM external_invoice_case_map WHERE matter_id = :mid"),
                    {"mid": matter_id},
                )
                # Future-proof: remove any additional direct FK children of matter.
                delete_matter_fk_children(
                    matter_id,
                    exclude_tables={
                        "workflows",
                        "external_invoice_case_link",
                        "external_invoice_case_map",
                    },
                )

                # 4. The Matter itself
                db.session.execute(
                    text("DELETE FROM matter WHERE matter_id = :mid"), {"mid": matter_id}
                )

                if op and op.id:
                    mark_operation_applied(
                        op,
                        summary_updates={
                            "case_id": str(case_id),
                            "matter_id": str(matter_id),
                            "request_id": idempotency_key or getattr(g, "request_id", None),
                            "actor_type": "user",
                        },
                    )

        db.session.commit()

        if not matter_id:
            flash("Matter Delete.", "success")
            return redirect(url_for("case_work.case_list"))

        # Best-effort: purge orphaned FileAssets (disk + DB) after the transaction is committed.
        if all_asset_ids:
            try:
                from app.services.storage.file_asset_service import get_file_asset_service

                file_service = get_file_asset_service()
                for fid in {str(x) for x in all_asset_ids if x}:
                    try:
                        file_service.purge_if_orphan(fid, min_age_days=0, dry_run=False)
                    except Exception as e:
                        current_app.logger.warning(
                            "GC: failed to purge file asset %s for matter %s: %s",
                            fid,
                            matter_id,
                            e,
                        )
            except Exception as e:
                current_app.logger.warning(
                    "GC: failed to initialize file service for matter %s: %s",
                    matter_id,
                    e,
                )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Failed to delete matter {matter_id}: {e}")
        flash(f"Delete In Progress Error : {e}", "danger")
        if matter_id:
            return redirect(url_for("case_work.case_detail", case_id=matter_id))
        return redirect(url_for("case_work.case_list"))

    flash("Matter   Data  Delete.", "success")
    return redirect(url_for("case_work.case_list"))
