from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.case.case_kind import CaseKind
from app.services.ops.operation_log import mark_operation_applied, reserve_operation
from app.utils.permissions import can_access_matter, is_invoice_manager

from .general_shared import *


def _is_our_ref_unique_violation(exc: IntegrityError) -> bool:
    msg = str(getattr(exc, "orig", exc) or "").lower()
    if not msg:
        return False
    if "duplicate" not in msg and "unique" not in msg:
        return False
    return any(
        token in msg
        for token in (
            "ix_matter_our_ref",
            "ux_matter_our_ref",
            "matter_our_ref",
            "key (our_ref)",
            "(our_ref)=",
        )
    )


_FAMILY_CREATE_MODE_ALIASES = {
    "priority": "priority",
    "Priority": "priority",
    "Priority": "priority",
    "PriorityFiling": "priority",
    "divisional": "divisional",
    "division": "divisional",
    "": "divisional",
    "Divisional application": "divisional",
    "paris": "paris",
    "": "paris",
    "": "paris",
    "ForeignFiling": "paris",
    "pct": "pct",
    "pctFiling": "pct",
    "national_phase": "national_phase",
    "nationalphase": "national_phase",
    "pctnationalphase": "national_phase",
    "pctnp": "national_phase",
    "Domestic": "national_phase",
    "Domestic": "national_phase",
    "DomesticFiling": "national_phase",
    "DomesticFiling": "national_phase",
    "madrid": "madrid",
    "madridFiling": "madrid",
    "": "madrid",
    "Filing": "madrid",
    "hague": "hague",
    "hagueFiling": "hague",
    "": "hague",
    "Filing": "hague",
}
_FAMILY_IP_BASE_TYPES = {"PATENT", "UTILITY", "DESIGN", "TRADEMARK"}
_ETC_CREATE_TYPES = {"PCT", "MADRID", "HAGUE", "COPYRIGHT", "LITIGATION", "MISC"}


def _normalize_family_create_mode(value: str | None) -> str:
    text_value = (value or "").strip()
    if not text_value:
        return ""
    raw = text_value.lower().replace(" ", "")
    return _FAMILY_CREATE_MODE_ALIASES.get(raw, "")


def _load_editable_family_source(family_link_target_id: str) -> tuple[Matter | None, str]:
    target_id = (family_link_target_id or "").strip()
    if not target_id:
        return None, ""

    try:
        source_matter = Matter.query.get(target_id)
    except Exception:
        current_app.logger.exception("failed to load family source matter (%s)", target_id)
        return None, target_id

    # Preserve unknown ids for existing form compatibility. If the id resolves
    # to a matter, do not expose source data unless the matter is active and the
    # user can edit it.
    if not source_matter:
        return None, target_id
    if bool(getattr(source_matter, "is_deleted", False)):
        return None, ""
    if not can_access_matter(current_user, str(source_matter.matter_id), action="edit_case"):
        return None, ""
    return source_matter, target_id


@dataclass(frozen=True)
class _CreateKindSpec:
    public_division: str
    public_type: str
    internal_division: str
    internal_type: str
    forced_app_route: str = ""


def _build_template_case_flags(
    *,
    division: str | None,
    case_type: str | None,
    display_division: str | None,
    display_case_type: str | None,
) -> dict[str, object]:
    profile_division = division
    profile_type = case_type
    try:
        from app.services.case.case_menu_config import case_menu_profile_values

        menu_profile = case_menu_profile_values(
            display_division or division,
            display_case_type or case_type,
        )
        if menu_profile:
            profile_division = menu_profile[0] or profile_division
            profile_type = menu_profile[1] or profile_type
    except Exception:
        current_app.logger.debug("case menu profile lookup failed", exc_info=True)

    kind = CaseKind.from_values(profile_division, profile_type)
    public_div = _normalize_public_create_division(display_division or division)
    public_typ = _normalize_public_create_type(display_case_type or case_type)
    return {
        "profile_division": kind.division,
        "profile_case_type": kind.case_type,
        "is_dom_pat": kind.is_dom_pat,
        "is_dom_design": kind.is_dom_design,
        "is_dom_tm": kind.is_dom_tm,
        "is_inc_pat": kind.is_inc_pat,
        "is_inc_design": kind.is_inc_design,
        "is_inc_tm": kind.is_inc_tm,
        "is_out_pat": kind.is_out_pat,
        "is_out_design": kind.is_out_design,
        "is_out_tm": kind.is_out_tm,
        "is_pct": kind.is_pct,
        "is_litigation": kind.is_litigation,
        "is_misc": kind.is_misc,
        "is_madrid": public_div == "ETC" and public_typ == "MADRID",
        "is_hague": public_div == "ETC" and public_typ == "HAGUE",
        "is_copyright": public_div == "ETC" and public_typ == "COPYRIGHT",
    }


def _normalize_public_create_division(value: str | None) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    if upper in {"DOM", "INC", "OUT", "ETC"}:
        return upper
    return _normalize_case_division(raw)


def _normalize_public_create_type(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    compact = raw.lower().replace(" ", "")
    if compact in {"madrid", "madridfiling", "filing"}:
        return "MADRID"
    if compact in {"hague", "haguefiling"}:
        return "HAGUE"
    if compact == "copyright":
        return "COPYRIGHT"
    normalized = _normalize_case_type(raw)
    if normalized:
        return normalized
    return raw.upper()


def _resolve_create_kind(
    *,
    division: str | None,
    case_type: str | None,
    app_route: str | None = None,
) -> _CreateKindSpec | None:
    public_division = _normalize_public_create_division(division)
    public_type = _normalize_public_create_type(case_type)
    forced_app_route = _normalize_special_app_route(app_route)

    if public_division == "OUT" and public_type == "PCT":
        return _CreateKindSpec("ETC", "PCT", "OUT", "PCT")
    if public_division == "OUT" and public_type == "TRADEMARK" and forced_app_route == "":
        return _CreateKindSpec("ETC", "MADRID", "OUT", "TRADEMARK", "")
    if public_division == "OUT" and public_type == "DESIGN" and forced_app_route == "HAGUE":
        return _CreateKindSpec("ETC", "HAGUE", "OUT", "DESIGN", "HAGUE")

    try:
        from app.services.case.case_menu_config import find_case_menu_item

        menu_item = find_case_menu_item(division, case_type)
    except Exception:
        menu_item = None
    if menu_item:
        return _CreateKindSpec(
            public_division=str(menu_item.get("division") or "").strip(),
            public_type=str(menu_item.get("type") or "").strip(),
            internal_division=str(
                menu_item.get("profile_division") or menu_item.get("division") or ""
            ).strip(),
            internal_type=str(
                menu_item.get("profile_type") or menu_item.get("type") or ""
            ).strip(),
            forced_app_route=str(menu_item.get("forced_app_route") or "").strip(),
        )

    if public_division in {"DOM", "INC", "OUT"} and public_type in _FAMILY_IP_BASE_TYPES:
        return _CreateKindSpec(
            public_division=public_division,
            public_type=public_type,
            internal_division=public_division,
            internal_type=public_type,
        )

    if not public_division and public_type in {"PCT", "LITIGATION", "MISC", "COPYRIGHT"}:
        public_division = "ETC"

    if public_division != "ETC":
        return None

    if public_type == "PCT":
        return _CreateKindSpec("ETC", "PCT", "OUT", "PCT")
    if public_type == "MADRID":
        return _CreateKindSpec("ETC", "MADRID", "OUT", "TRADEMARK", "")
    if public_type == "HAGUE":
        return _CreateKindSpec("ETC", "HAGUE", "OUT", "DESIGN", "HAGUE")
    if public_type == "COPYRIGHT":
        return _CreateKindSpec("ETC", "COPYRIGHT", "", "MISC")
    if public_type == "LITIGATION":
        return _CreateKindSpec("ETC", "LITIGATION", "", "LITIGATION")
    if public_type == "MISC":
        return _CreateKindSpec("ETC", "MISC", "", "MISC")
    return None


def _build_create_route_params(
    spec: _CreateKindSpec,
    *,
    popup: str = "",
    invoice_id: str = "",
    client_id: str = "",
    family_link_target_id: str = "",
    family_create_mode: str = "",
) -> dict[str, str]:
    params: dict[str, str] = {
        "type": spec.public_type,
    }
    if spec.public_division:
        params["division"] = spec.public_division
    if spec.public_type == "COPYRIGHT":
        params["right_type"] = ""
        params["case_kind"] = ""
    if popup:
        params["popup"] = popup
    if invoice_id:
        params["invoice_id"] = invoice_id
    if client_id:
        params["client_id"] = client_id
    if family_link_target_id:
        params["family_link_target_id"] = family_link_target_id
    if family_create_mode:
        params["family_create_mode"] = family_create_mode
    return params


def _normalize_family_source_type(value: str | None) -> str:
    normalized = _normalize_case_type((value or "").strip())
    if normalized in _FAMILY_IP_BASE_TYPES or normalized == "PCT":
        return normalized
    return ""


def _infer_family_source_type_from_ref(value: str | None) -> str:
    ref = (value or "").strip().upper()
    if not ref:
        return ""
    if ref.endswith("PCT"):
        return "PCT"
    if len(ref) < 4 or not ref[:2].isdigit():
        return ""
    type_code = ref[2]
    if type_code == "P":
        return "PATENT"
    if type_code == "U":
        return "UTILITY"
    if type_code == "D":
        return "DESIGN"
    if type_code == "T":
        return "TRADEMARK"
    return ""


def _family_source_type_for(source_matter: Matter | None) -> str:
    if not source_matter:
        return ""

    normalized = _normalize_family_source_type(getattr(source_matter, "matter_type", ""))
    if normalized:
        return normalized

    # Fallback for legacy/dirty rows where matter_type is empty or non-canonical.
    normalized_ref = _normalize_family_source_type(
        _infer_family_source_type_from_ref(getattr(source_matter, "our_ref", ""))
    )
    if normalized_ref:
        return normalized_ref

    _div, inferred_type = _infer_case_kind_from_right_name(getattr(source_matter, "right_name", ""))
    normalized_inferred = _normalize_family_source_type(inferred_type)
    if normalized_inferred:
        return normalized_inferred

    return ""


def _normalize_family_source_division(value: str | None) -> str:
    normalized = _normalize_case_division((value or "").strip())
    if normalized in {"DOM", "INC", "OUT"}:
        return normalized
    return "DOM"


def _to_date_only(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) >= 10:
        raw = raw[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return ""


def _first_non_empty(*values) -> str:
    for value in values:
        text_value = str(value or "").strip()
        if text_value:
            return text_value
    return ""


def _get_custom_data_map(*, matter_id: str, namespace: str) -> dict:
    ns = (namespace or "").strip()
    if not ns:
        return {}
    row = MatterCustomField.query.filter_by(matter_id=matter_id, namespace=ns).first()
    if not row or not isinstance(row.data, dict):
        return {}
    return dict(row.data or {})


def _first_identifier_value(*, matter_id: str, id_types: tuple[str, ...]) -> str:
    for id_type in id_types:
        row = (
            MatterIdentifier.query.filter_by(matter_id=matter_id, id_type=id_type)
            .order_by(MatterIdentifier.mid_id.asc())
            .first()
        )
        if row and (row.id_value or "").strip():
            return (row.id_value or "").strip()
    return ""


def _priority_filing_type_for(case_type: str) -> str:
    typ = (case_type or "").strip().upper()
    if typ in {"PATENT", "UTILITY"}:
        return "Priority Filing"
    return ""


def _divisional_filing_type_for(case_type: str) -> str:
    typ = (case_type or "").strip().upper()
    if typ in {"PATENT", "UTILITY"}:
        return "Divisional application"
    if typ == "DESIGN":
        return "DesignExaminationRegistrationDivisional application"
    if typ == "TRADEMARK":
        return "TrademarkRegistrationDivisional application"
    return ""


def _normalize_special_app_route(value: str | None) -> str:
    text_value = (value or "").strip()
    if not text_value:
        return ""
    lowered = text_value.lower()
    if "madrid" in lowered or "\ub9c8\ub4dc\ub9ac\ub4dc" in text_value:
        return ""
    if "hague" in lowered or "\ud5e4\uc774\uadf8" in text_value:
        return "HAGUE"
    return text_value


def _forced_app_route_for_create(
    *, division: str, matter_type: str, raw_app_route: str | None
) -> str:
    app_route = _normalize_special_app_route(raw_app_route)
    public_division = _normalize_public_create_division(division)
    public_type = _normalize_public_create_type(matter_type)
    if public_division == "ETC" and public_type == "MADRID":
        return ""
    if public_division == "ETC" and public_type == "HAGUE":
        return "HAGUE"
    div = _normalize_case_division(division)
    typ = _normalize_case_type(matter_type)
    if div == "OUT" and typ == "TRADEMARK" and app_route == "":
        return ""
    if div == "OUT" and typ == "DESIGN" and app_route == "HAGUE":
        return "HAGUE"
    return ""


def _international_family_option_for(source_type: str) -> dict[str, str] | None:
    typ = _normalize_case_type(source_type)
    if typ in {"PATENT", "UTILITY"}:
        return {
            "mode": "pct",
            "label": "PCT Filing",
            "description": "ETC/PCT + Priority  ",
            "icon": "bi-airplane",
            "division": "OUT",
            "type": "PCT",
            "public_division": "ETC",
            "public_type": "PCT",
        }
    if typ == "TRADEMARK":
        return {
            "mode": "madrid",
            "label": " Filing",
            "description": "ETC/ + Priority  ",
            "icon": "bi-airplane",
            "division": "OUT",
            "type": "TRADEMARK",
            "public_division": "ETC",
            "public_type": "MADRID",
            "app_route": "",
        }
    if typ == "DESIGN":
        return {
            "mode": "hague",
            "label": " Filing",
            "description": "ETC/ + Priority  ",
            "icon": "bi-airplane",
            "division": "OUT",
            "type": "DESIGN",
            "public_division": "ETC",
            "public_type": "HAGUE",
            "app_route": "HAGUE",
        }
    return None


def _pct_national_phase_option() -> dict[str, str]:
    return {
        "mode": "national_phase",
        "label": "PCT Domestic ",
        "description": "OUT/PATENT + PCT/Priority  ",
        "icon": "bi-box-arrow-in-right",
        "division": "OUT",
        "type": "PATENT",
    }


def _family_mode_specs_for(source_matter: Matter | None) -> dict[str, dict[str, str]]:
    if not source_matter:
        return {}

    src_div = _normalize_family_source_division(getattr(source_matter, "right_group", ""))
    src_typ = _family_source_type_for(source_matter)
    if src_typ == "PCT":
        return {"national_phase": _pct_national_phase_option()}
    if src_typ not in _FAMILY_IP_BASE_TYPES:
        return {}

    options = [
        {
            "mode": "priority",
            "label": "Priority Filing",
            "description": "Priority / Auto ",
            "icon": "bi-flag",
            "division": src_div,
            "type": src_typ,
        },
        {
            "mode": "divisional",
            "label": "Divisional application",
            "description": "Parent application / Auto ",
            "icon": "bi-diagram-2",
            "division": src_div,
            "type": src_typ,
        },
        {
            "mode": "paris",
            "label": " ForeignFiling",
            "description": "Foreign(OUT) + items Route ",
            "icon": "bi-globe2",
            "division": "OUT",
            "type": src_typ,
        },
    ]
    intl_option = _international_family_option_for(src_typ)
    if intl_option:
        options.append(intl_option)

    # Safety net: trademark/design family flows must never expose PCT shortcuts.
    if src_typ in {"TRADEMARK", "DESIGN"}:
        options = [
            item
            for item in options
            if item.get("mode") != "pct" and _normalize_case_type(item.get("type")) != "PCT"
        ]

    return {str(item.get("mode") or ""): item for item in options if item.get("mode")}


def _infer_family_create_mode_for_target(
    *,
    source_matter: Matter | None,
    target_division: str,
    target_type: str,
) -> str:
    if not source_matter:
        return ""

    src_typ = _family_source_type_for(source_matter)
    target_public_div = _normalize_public_create_division(target_division)
    target_public_typ = _normalize_public_create_type(target_type)

    if (
        src_typ in {"PATENT", "UTILITY"}
        and target_public_div == "ETC"
        and target_public_typ == "PCT"
    ):
        return "pct"
    if src_typ == "TRADEMARK" and target_public_div == "ETC" and target_public_typ == "MADRID":
        return "madrid"
    if src_typ == "DESIGN" and target_public_div == "ETC" and target_public_typ == "HAGUE":
        return "hague"
    if (
        src_typ == "PCT"
        and target_public_div in {"OUT", "INC"}
        and target_public_typ
        in {
            "PATENT",
            "UTILITY",
        }
    ):
        return "national_phase"
    return ""


def _effective_family_create_mode(
    *,
    requested_mode: str | None,
    source_matter: Matter | None,
    target_division: str,
    target_type: str,
) -> str:
    mode = _normalize_family_create_mode(requested_mode)
    if not mode:
        mode = _infer_family_create_mode_for_target(
            source_matter=source_matter,
            target_division=target_division,
            target_type=target_type,
        )
    if not mode:
        return ""

    mode_specs = _family_mode_specs_for(source_matter)
    spec = mode_specs.get(mode)
    if not spec:
        return ""

    expected_division = _normalize_case_division(spec.get("division"))
    expected_type = _normalize_case_type(spec.get("type"))
    expected_public_division = _normalize_public_create_division(
        spec.get("public_division") or spec.get("division")
    )
    expected_public_type = _normalize_public_create_type(
        spec.get("public_type") or spec.get("type")
    )
    target_div = _normalize_case_division(target_division)
    target_typ = _normalize_case_type(target_type)
    target_public_div = _normalize_public_create_division(target_division)
    target_public_typ = _normalize_public_create_type(target_type)

    if (
        target_public_div != expected_public_division or target_public_typ != expected_public_type
    ) and (target_div != expected_division or target_typ != expected_type):
        return ""
    return mode


def _build_family_create_options(
    *,
    source_matter: Matter | None,
    family_link_target_id: str,
    popup: str,
    invoice_id: str,
    client_id: str,
) -> list[dict]:
    target_id = (family_link_target_id or "").strip()
    if not target_id or not source_matter:
        return []

    options = list(_family_mode_specs_for(source_matter).values())

    out: list[dict] = []
    for item in options:
        spec = _CreateKindSpec(
            public_division=(item.get("public_division") or item.get("division") or "").strip(),
            public_type=(item.get("public_type") or item.get("type") or "").strip(),
            internal_division=(item.get("division") or "").strip(),
            internal_type=(item.get("type") or "").strip(),
            forced_app_route=(item.get("app_route") or "").strip(),
        )
        params = _build_create_route_params(
            spec,
            popup=popup,
            invoice_id=invoice_id,
            client_id=client_id,
            family_link_target_id=target_id,
            family_create_mode=str(item.get("mode") or "").strip(),
        )
        out.append({**item, "url": url_for("case_work.create_matter", **params)})
    return out


def _build_family_inherit_prefill(
    *,
    family_link_target_id: str,
    division: str,
    matter_type: str,
    family_create_mode: str,
) -> tuple[dict, dict]:
    target_id = (family_link_target_id or "").strip()
    if not target_id:
        return {}, {}

    source = Matter.query.get(target_id)
    if not source:
        return {}, {}
    mode = _effective_family_create_mode(
        requested_mode=family_create_mode,
        source_matter=source,
        target_division=division,
        target_type=matter_type,
    )

    source_div = _normalize_case_division((source.right_group or "").strip()) or ""
    source_typ = _normalize_case_type((source.matter_type or "").strip()) or ""
    if source_typ in ("LITIGATION", "MISC"):
        source_div = ""

    source_ns = ""
    try:
        source_ns = (CaseParameterService.get_namespace(source_div, source_typ) or "").strip()
    except Exception:
        source_ns = ""

    source_registry = _get_custom_data_map(matter_id=target_id, namespace=source_ns)
    source_basic = _get_custom_data_map(matter_id=target_id, namespace="basic")

    def _pick(*keys: str) -> str:
        for key in keys:
            v = _first_non_empty(source_registry.get(key), source_basic.get(key))
            if v:
                return v
        return ""

    source_app_no = _first_non_empty(
        _pick("application_no"),
        _first_identifier_value(
            matter_id=target_id,
            id_types=("Application No.", "APP_NO", "application_no", "app_no"),
        ),
    )
    source_app_date = _to_date_only(_pick("application_date"))
    source_priority_no = _first_non_empty(
        _pick("priority_no"),
        _first_identifier_value(
            matter_id=target_id,
            id_types=("Priority", "priority_no"),
        ),
    )
    source_priority_date = _to_date_only(_pick("priority_date"))
    source_parent_no = _first_non_empty(
        _pick("parent_application_no"),
        _first_identifier_value(
            matter_id=target_id,
            id_types=("Parent application No.", "parent_application_no"),
        ),
    )
    source_parent_date = _to_date_only(_pick("parent_application_date"))
    source_country = _first_non_empty(_pick("application_country"))
    source_has_priority = bool(source_priority_no or source_priority_date)
    source_pct_no = source_app_no
    source_pct_date = source_app_date

    inherit_prefill = {
        "family_link_target_id": target_id,
        "right_name": _first_non_empty(source.right_name),
        "client_id": _pick("client_id"),
        "client_name": _pick("client_name"),
        "applicant_name": _pick("applicant_name"),
        "applicant_same_as_client": _pick("applicant_same_as_client"),
        "manager": _pick("manager"),
        "manager_id": _pick("manager_id"),
        "attorney": _pick("attorney"),
        "attorney_id": _pick("attorney_id"),
        "handler": _pick("handler"),
        "handler_id": _pick("handler_id"),
        "application_applicant_name": _pick("application_applicant_name"),
        "application_agent": _pick("application_agent"),
        "application_applicant_customer_no": _pick("application_applicant_customer_no"),
    }
    inherit_prefill = {
        key: value
        for key, value in inherit_prefill.items()
        if key == "family_link_target_id" or (value or "").strip()
    }

    target_allowed = set(CaseParameterService.get_allowed_keys(division, matter_type))
    forced_prefill: dict[str, str] = {}
    priority_no = _first_non_empty(source_priority_no, source_app_no)
    priority_date = _first_non_empty(source_priority_date, source_app_date)
    parent_no = _first_non_empty(source_parent_no, source_app_no)
    parent_date = _first_non_empty(source_parent_date, source_app_date)

    def _set_forced(key: str, value: str) -> None:
        if not (value or "").strip():
            return
        if target_allowed and key not in target_allowed:
            return
        forced_prefill[key] = value.strip()

    if mode in {"priority", "divisional", "paris", "pct", "national_phase", "madrid", "hague"}:
        _set_forced("filing_deadline_type", "LEGAL")

    if mode == "priority":
        _set_forced("priority_claimed", "Y")
        _set_forced("priority_no", priority_no)
        _set_forced("priority_date", priority_date)
        _set_forced("filing_type", _priority_filing_type_for(matter_type))
    elif mode == "divisional":
        _set_forced("filing_type", _divisional_filing_type_for(matter_type))
        _set_forced("parent_application_no", parent_no)
        _set_forced("parent_application_date", parent_date)
    elif mode == "paris":
        _set_forced("app_route", "items")
        _set_forced("priority_claimed", "Y")
        _set_forced("priority_no", priority_no)
        _set_forced("priority_date", priority_date)
        if source_country and source_country.upper() != "US":
            _set_forced("application_country", source_country.upper())
    elif mode in {"pct", "madrid", "hague"}:
        _set_forced("priority_claimed", "Y")
        _set_forced("priority_no", priority_no)
        _set_forced("priority_date", priority_date)
        if mode == "madrid":
            _set_forced("app_route", "")
        elif mode == "hague":
            _set_forced("app_route", "HAGUE")
    elif mode == "national_phase":
        _set_forced("pct_application_no", source_pct_no)
        _set_forced("pct_application_date", source_pct_date)
        if source_has_priority:
            _set_forced("priority_claimed", "Y")
            _set_forced("priority_no", source_priority_no)
            _set_forced("priority_date", source_priority_date)
        _set_forced("app_route", "PCT-NP" if _normalize_case_division(division) == "OUT" else "PCT")

    return inherit_prefill, forced_prefill


def _merge_family_prefill(*, prefill: dict, inherit_prefill: dict, forced_prefill: dict) -> dict:
    merged = dict(prefill or {})
    for key, value in (inherit_prefill or {}).items():
        if not (value or "").strip():
            continue
        if not str(merged.get(key) or "").strip():
            merged[key] = value
    for key, value in (forced_prefill or {}).items():
        if not (value or "").strip():
            continue
        if not str(merged.get(key) or "").strip():
            merged[key] = value
    return merged


def _resolve_invoice_client(invoice_id: str | None) -> Client | None:
    raw_invoice_id = (invoice_id or "").strip()
    if not raw_invoice_id:
        return None
    try:
        invoice_id_int = int(raw_invoice_id)
    except Exception:
        return None

    try:
        from app.services.billing.invoice_services import InvoiceService

        invoice = InvoiceService.get_by_id(invoice_id_int) or {}
    except Exception:
        current_app.logger.exception(
            "Failed to resolve invoice client for invoice_id=%s", raw_invoice_id
        )
        return None

    try:
        client_id = int(invoice.get("client_id") or 0)
    except Exception:
        client_id = 0
    if client_id <= 0:
        return None
    return Client.query.get(client_id)


@bp.route("/matter/create/select", methods=["GET"])
@login_required
def select_matter_create():
    hx_redirect = _hx_hard_redirect_response(
        "case_work.select_matter_create", **request.args.to_dict()
    )
    if hx_redirect is not None:
        return hx_redirect

    popup = (request.args.get("popup") or "").strip()
    invoice_id = (request.args.get("invoice_id") or "").strip()
    client_id = (request.args.get("client_id") or "").strip()
    if not client_id and invoice_id:
        invoice_client = _resolve_invoice_client(invoice_id)
        if invoice_client:
            client_id = str(invoice_client.id)
    family_link_target_id = (request.args.get("family_link_target_id") or "").strip()
    family_create_mode = _normalize_family_create_mode(request.args.get("family_create_mode"))

    source_matter, family_link_target_id = _load_editable_family_source(family_link_target_id)

    family_create_options = _build_family_create_options(
        source_matter=source_matter,
        family_link_target_id=family_link_target_id,
        popup=popup,
        invoice_id=invoice_id,
        client_id=client_id,
    )
    allowed_modes = {str(opt.get("mode") or "") for opt in family_create_options}
    if family_create_mode and family_create_mode not in allowed_modes:
        family_create_mode = ""

    return render_template(
        "case/matter_create_select.html",
        family_source_matter=source_matter,
        family_create_options=family_create_options,
        family_create_mode=family_create_mode,
        popup_param=popup,
        invoice_id_param=invoice_id,
        client_id_param=client_id,
        family_link_target_id_param=family_link_target_id,
    )


@bp.route("/matter/intake", methods=["GET", "POST"])
@login_required
def intake_matter():
    if request.method == "GET":
        hx_redirect = _hx_hard_redirect_response(
            "case_work.select_matter_create", **request.args.to_dict()
        )
        if hx_redirect is not None:
            return hx_redirect
        return redirect(url_for("case_work.select_matter_create", **request.args.to_dict()))

    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        MatterCreateCommand,
        MatterCreateResult,
        SessionIdempotencyStore,
    )

    sess_store = SessionIdempotencyStore(session)
    hx_redirect = _hx_hard_redirect_response("case_work.intake_matter", **request.args.to_dict())
    if hx_redirect is not None:
        return hx_redirect

    def _default_division() -> str:
        raw = (request.args.get("division") or "").strip()
        return _normalize_case_division(raw) or "DOM"

    def _default_type() -> str:
        raw = (request.args.get("type") or request.args.get("case_type") or "").strip()
        return _normalize_case_type(raw) or "PATENT"

    def _render_form(*, form_data: dict, missing_fields: list | None = None):
        division = _normalize_case_division(form_data.get("division")) or _default_division()
        case_type = (
            _normalize_case_type(form_data.get("case_type") or form_data.get("type"))
            or _default_type()
        )
        if not _is_valid_create_kind(division, case_type):
            division, case_type = "DOM", "PATENT"

        staff_picker = _build_staff_picker_context()
        staff_assignment = _build_staff_assignment_context()

        field_meta = {
            "our_ref": {"label": "Our Ref."},
            "application_no": {"label": "Application no."},
            "application_country": {"label": "Country"},
            "client_name": {"label": "Client"},
            "applicant_name": {"label": "Applicant"},
            "retained_at": {"label": "Intake date"},
            "manager": {"label": "Docketing owner"},
            "attorney": {"label": "Responsible attorney"},
        }

        return render_template(
            "case/matter_intake.html",
            form_data=form_data,
            division=division,
            case_type=case_type,
            staff_picker=staff_picker,
            staff_assignment=staff_assignment,
            idempotency_key=form_data.get("idempotency_key") or uuid.uuid4().hex,
            missing_fields=missing_fields or [],
            field_meta=field_meta,
        )

    if request.method == "GET":
        prefill = _extract_prefill_params(request.args)
        division = _default_division()
        case_type = _default_type()
        if not _is_valid_create_kind(division, case_type):
            division, case_type = "DOM", "PATENT"
        prefill.setdefault("division", division)
        prefill.setdefault("case_type", case_type)
        prefill.setdefault("type", case_type)
        prefill.setdefault("right_type", case_type)
        if "application_country" not in prefill:
            prefill["application_country"] = "US" if division != "OUT" else ""
        prefill.setdefault("idempotency_key", uuid.uuid4().hex)
        return _render_form(form_data=prefill)

    form_data = dict(request.form)
    idempotency_key = (form_data.get("idempotency_key") or "").strip() or uuid.uuid4().hex

    division = _normalize_case_division(form_data.get("division"))
    case_type = _normalize_case_type(form_data.get("case_type") or form_data.get("type"))
    if not _is_valid_create_kind(division, case_type):
        flash("Invalid matter flow or IP type. Please select again.", "warning")
        form_data["idempotency_key"] = idempotency_key
        return _render_form(form_data=form_data)

    cmd = MatterCreateCommand(
        division=division,
        case_type=case_type,
        form_data=form_data,
        files=request.files,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
    )

    class _RollbackIntakeMatter(Exception):
        def __init__(self, result: MatterCreateResult):
            super().__init__("rollback intake_matter")
            self.result = result

    op, created = reserve_operation(
        "case.create",
        request_id=idempotency_key,
        actor_id=current_user.id,
        summary_json={"actor_type": "user"},
        targets_json={"context": "intake_matter"},
    )
    if not created and op and isinstance(op.summary_json, dict):
        existing_id = op.summary_json.get("matter_id") or op.summary_json.get("existing_id")
        if existing_id:
            flash("This request has already been processed.", "warning")
            return redirect(url_for("case_work.case_detail", case_id=existing_id))

    res: MatterCreateResult | None = None
    try:
        with db.session.begin_nested():
            res = MatterCreateApplyUseCase().execute(cmd, sess_store)
            if not res.success:
                raise _RollbackIntakeMatter(res)
            mark_operation_applied(
                op,
                summary_updates={
                    "matter_id": res.matter_id,
                    "existing_id": res.existing_id,
                    "request_id": idempotency_key,
                },
            )
        db.session.commit()
    except _RollbackIntakeMatter as exc:
        db.session.rollback()
        res = exc.result
    except IntegrityError as exc:
        db.session.rollback()
        if _is_our_ref_unique_violation(exc):
            flash("This Our Ref. already exists.", "danger")
            res = MatterCreateResult(success=False, error="This Our Ref. already exists.")
        else:
            current_app.logger.exception("intake_matter POST integrity error")
            flash("A data integrity error occurred while saving.", "danger")
            res = MatterCreateResult(success=False, error="integrity_error")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("intake_matter POST failed")
        flash("An error occurred while saving.", "danger")
        res = MatterCreateResult(success=False, error="internal_error")

    if res and res.success:
        target_matter_id = res.existing_id or res.matter_id
        if res.existing_id:
            flash("This request has already been processed.", "warning")
            return redirect(url_for("case_work.case_detail", case_id=target_matter_id))

        flash("Matter created.", "success")
        return redirect(url_for("case_work.case_detail", case_id=target_matter_id))

    if not res:
        res = MatterCreateResult(success=False, error="internal_error")
    if res.validation_errors:
        missing_labels = [m.get("label", m.get("key", "")) for m in res.validation_errors]
        flash(f"Required fields are missing: {', '.join(missing_labels)}", "warning")
    elif res.error:
        flash(res.error, "warning" if "warning" in (res.error or "").lower() else "danger")

    form_data["idempotency_key"] = idempotency_key
    return _render_form(form_data=form_data, missing_fields=res.validation_errors or [])


@bp.route("/create/<string:case_type>", methods=["GET", "POST"])
@login_required
def legacy_create_case(case_type: str):
    """Backward-compatible alias for older create URLs."""
    case_type = (case_type or "").strip().lower()
    return redirect(url_for("case_work.create_matter", case_type=case_type), code=302)


@bp.route("/matter/create", methods=["GET", "POST"])
@login_required
def create_matter():
    from app.services.matter.matter_use_cases import (
        MatterCreateApplyUseCase,
        MatterCreatePrepareUseCase,
        SessionIdempotencyStore,
    )

    sess_store = SessionIdempotencyStore(session)
    current_app.logger.debug("created sess_store: %s", sess_store)
    hx_redirect = _hx_hard_redirect_response("case_work.create_matter", **request.args.to_dict())
    if hx_redirect is not None:
        return hx_redirect

    is_popup = request.args.get("popup") == "1"
    invoice_id = (request.args.get("invoice_id") or "").strip()
    if invoice_id and not is_invoice_manager(current_user):
        current_app.logger.warning(
            "Ignoring invoice_id for non-invoice-manager user_id=%s",
            getattr(current_user, "id", None),
        )
        invoice_id = ""
    client_id = (request.args.get("client_id") or "").strip()
    resolved_invoice_client = None
    if not client_id and invoice_id:
        resolved_invoice_client = _resolve_invoice_client(invoice_id)
        if resolved_invoice_client:
            client_id = str(resolved_invoice_client.id)
    family_link_target_id = (request.args.get("family_link_target_id") or "").strip()
    family_create_mode = _normalize_family_create_mode(request.args.get("family_create_mode"))
    family_source_matter, family_link_target_id = _load_editable_family_source(
        family_link_target_id
    )
    select_redirect_params = {}
    if is_popup:
        select_redirect_params["popup"] = "1"
    if invoice_id:
        select_redirect_params["invoice_id"] = invoice_id
    if client_id:
        select_redirect_params["client_id"] = client_id
    if family_link_target_id:
        select_redirect_params["family_link_target_id"] = family_link_target_id
    if family_create_mode:
        select_redirect_params["family_create_mode"] = family_create_mode

    def _maybe_link_invoice(matter_id: str | None) -> None:
        if not invoice_id or not matter_id:
            return
        invoice_id_int = None
        try:
            invoice_id_int = int(invoice_id)
        except Exception:
            invoice_id_int = None
        if invoice_id_int is not None:
            try:
                linked = fetch_linked_invoices_for_case(
                    matter_id=matter_id, our_ref=None, limit=200
                )
                if any(int(inv.get("id") or 0) == invoice_id_int for inv in linked):
                    return
            except Exception:
                current_app.logger.exception("invoice link check failed")
        try:
            InvoiceMatterLinkUseCase.link(
                matter_id=matter_id,
                external_invoice_ref=invoice_id,
                actor_id=getattr(current_user, "id", None),
            )
        except InvoiceBridgeError as exc:
            current_app.logger.exception("invoice link failed")
            flash(f"Invoice Link Failed: {exc}", "warning")
        except Exception:
            current_app.logger.exception("invoice link failed")
            flash("Invoice Link In Progress Error .", "warning")

    raw_args_division = (request.args.get("division") or "").strip()
    raw_args_type = (request.args.get("type") or "").strip()
    raw_args_app_route = (request.args.get("app_route") or "").strip()
    args_division = _normalize_case_division(raw_args_division)
    args_type = _normalize_case_type(raw_args_type)
    create_kind = _resolve_create_kind(
        division=raw_args_division,
        case_type=raw_args_type,
        app_route=raw_args_app_route,
    )
    forced_app_route = ""
    effective_family_create_mode = ""

    if request.method == "GET":
        legacy_special_requested = (
            (args_division == "OUT" and args_type == "PCT")
            or (
                args_division == "OUT"
                and args_type == "TRADEMARK"
                and _normalize_special_app_route(raw_args_app_route) == ""
            )
            or (
                args_division == "OUT"
                and args_type == "DESIGN"
                and _normalize_special_app_route(raw_args_app_route) == "HAGUE"
            )
        )
        if legacy_special_requested and create_kind:
            return redirect(
                url_for(
                    "case_work.create_matter",
                    **_build_create_route_params(
                        create_kind,
                        popup=select_redirect_params.get("popup", ""),
                        invoice_id=invoice_id,
                        client_id=client_id,
                        family_link_target_id=family_link_target_id,
                        family_create_mode=family_create_mode,
                    ),
                )
            )
        if raw_args_type and not create_kind and not args_type:
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))
        if raw_args_division and not create_kind and not args_division:
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))

        division = create_kind.public_division if create_kind else args_division
        matter_type = create_kind.public_type if create_kind else args_type
        display_division = create_kind.public_division if create_kind else division
        display_case_type = create_kind.public_type if create_kind else matter_type

        if not _is_valid_create_kind(division, matter_type):
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))
        effective_family_create_mode = _effective_family_create_mode(
            requested_mode=family_create_mode,
            source_matter=family_source_matter,
            target_division=division,
            target_type=matter_type,
        )

        try:
            raw_args = dict(request.args)
            if (
                create_kind
                and create_kind.public_type == "COPYRIGHT"
                and matter_type == "COPYRIGHT"
            ):
                raw_args.setdefault("right_type", "")
                raw_args.setdefault("case_kind", "")
            cmd = MatterCreatePrepareCommand(
                division=division, case_type=matter_type, raw_args=raw_args
            )
            res = MatterCreatePrepareUseCase().execute(cmd, sess_store)

            # Prefill applicant details from the selected CRM or invoice client.
            c = resolved_invoice_client
            if c is None and client_id and str(client_id).isdigit():
                c = Client.query.get(int(client_id))
            if c:
                prefill = dict(res.prefill or {})
                if not str(prefill.get("client_id") or "").strip():
                    prefill["client_id"] = str(c.id)
                if not str(prefill.get("client_name") or "").strip():
                    prefill["client_name"] = (c.name or "").strip()
                if division != "INC" and not str(prefill.get("applicant_name") or "").strip():
                    prefill["applicant_name"] = (c.name or "").strip()
                    prefill.setdefault("applicant_same_as_client", "1")
                res.prefill = prefill

            if family_link_target_id:
                inherit_prefill, forced_prefill = _build_family_inherit_prefill(
                    family_link_target_id=family_link_target_id,
                    division=division,
                    matter_type=matter_type,
                    family_create_mode=effective_family_create_mode,
                )
                res.prefill = _merge_family_prefill(
                    prefill=dict(res.prefill or {}),
                    inherit_prefill=inherit_prefill,
                    forced_prefill=forced_prefill,
                )
                prefill_with_family_target = dict(res.prefill or {})
                prefill_with_family_target["family_link_target_id"] = family_link_target_id
                res.prefill = prefill_with_family_target
            prefill_for_mode = dict(res.prefill or {})
            if effective_family_create_mode:
                prefill_for_mode["family_create_mode"] = effective_family_create_mode
            else:
                prefill_for_mode.pop("family_create_mode", None)
            res.prefill = prefill_for_mode

            forced_app_route = _forced_app_route_for_create(
                division=division,
                matter_type=matter_type,
                raw_app_route=_first_non_empty(
                    request.args.get("app_route"),
                    create_kind.forced_app_route if create_kind else "",
                    (
                        (res.prefill or {}).get("_forced_app_route")
                        if isinstance(res.prefill, dict)
                        else ""
                    ),
                    (res.prefill or {}).get("app_route") if isinstance(res.prefill, dict) else "",
                ),
            )
            if forced_app_route:
                prefill = dict(res.prefill or {})
                prefill["app_route"] = forced_app_route
                prefill["_forced_app_route"] = forced_app_route
                res.prefill = prefill
        except Exception:
            current_app.logger.exception("create_matter GET failed")
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))

        field_layout = res.field_layout
        template_flags = _build_template_case_flags(
            division=res.division,
            case_type=res.case_type,
            display_division=display_division,
            display_case_type=display_case_type,
        )

        form_action_params = {}
        if create_kind:
            form_action_params = _build_create_route_params(
                create_kind,
                popup="1" if is_popup else "",
                invoice_id=invoice_id,
                client_id=client_id,
                family_link_target_id=family_link_target_id,
                family_create_mode=effective_family_create_mode,
            )
        else:
            if matter_type:
                form_action_params["type"] = matter_type
            if division:
                form_action_params["division"] = division
            if is_popup:
                form_action_params["popup"] = "1"
            if invoice_id:
                form_action_params["invoice_id"] = invoice_id
        form_action = url_for("case_work.create_matter", **form_action_params)

        ctx = res.context or {}
        staff_picker = ctx.get("staff_picker") or _build_staff_picker_context()
        staff_assignment = ctx.get("staff_assignment") or _build_staff_assignment_context()

        current_app.logger.debug(
            f"DEBUG: create_matter GET rendering template. idempotency_key={res.idempotency_key!r} division={division} type={matter_type}"
        )

        return render_template(
            "case/matter_create.html",
            division=division,
            case_type=matter_type,
            display_division=display_division,
            display_case_type=display_case_type,
            dom_patent_fields=field_layout if template_flags["is_dom_pat"] else [],
            dom_design_fields=field_layout if template_flags["is_dom_design"] else [],
            dom_trademark_fields=field_layout if template_flags["is_dom_tm"] else [],
            inc_patent_fields=field_layout if template_flags["is_inc_pat"] else [],
            inc_design_fields=field_layout if template_flags["is_inc_design"] else [],
            inc_trademark_fields=field_layout if template_flags["is_inc_tm"] else [],
            out_patent_fields=field_layout if template_flags["is_out_pat"] else [],
            out_design_fields=field_layout if template_flags["is_out_design"] else [],
            out_trademark_fields=field_layout if template_flags["is_out_tm"] else [],
            pct_fields=field_layout if template_flags["is_pct"] else [],
            litigation_fields=field_layout if template_flags["is_litigation"] else [],
            misc_fields=field_layout if template_flags["is_misc"] else [],
            staff_picker=staff_picker,
            staff_assignment=staff_assignment,
            form_data=res.prefill or {},
            prefill=res.prefill or None,
            field_meta=res.field_meta,
            missing_fields=[],
            form_action=form_action,
            idempotency_key=res.idempotency_key,
            forced_app_route=forced_app_route,
            effective_family_create_mode=effective_family_create_mode,
            **template_flags,
        )

    else:  # POST

        class _RollbackCreateMatter(Exception):
            def __init__(self, result: MatterCreateResult):
                super().__init__("rollback create_matter")
                self.result = result

        form_data = dict(request.form)
        current_app.logger.debug(f"DEBUG: create_matter POST form_data: {form_data}")
        client_id_from_form = (form_data.get("client_id") or "").strip()
        if client_id_from_form and "client_id" not in select_redirect_params:
            select_redirect_params["client_id"] = client_id_from_form
        idempotency_key = (form_data.get("idempotency_key") or "").strip()
        division, matter_type = sess_store.load_context(idempotency_key)

        if not idempotency_key:
            flash("  . Retry .", "warning")
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))
        if not matter_type or (not division and matter_type not in ("LITIGATION", "MISC")):
            flash(" . Retry .", "warning")
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))
        if not _is_valid_create_kind(division, matter_type):
            flash("  Create . Retry .", "warning")
            return redirect(url_for("case_work.select_matter_create", **select_redirect_params))

        raw_post_family_link_target_id = (form_data.get("family_link_target_id") or "").strip()
        post_family_source_matter, post_family_link_target_id = _load_editable_family_source(
            raw_post_family_link_target_id
        )
        if raw_post_family_link_target_id and not post_family_link_target_id:
            form_data.pop("family_link_target_id", None)
        elif post_family_link_target_id:
            form_data["family_link_target_id"] = post_family_link_target_id
        effective_post_family_mode = _effective_family_create_mode(
            requested_mode=form_data.get("family_create_mode"),
            source_matter=post_family_source_matter,
            target_division=division,
            target_type=matter_type,
        )
        if effective_post_family_mode:
            form_data["family_create_mode"] = effective_post_family_mode
        else:
            form_data.pop("family_create_mode", None)

        forced_app_route = _forced_app_route_for_create(
            division=division,
            matter_type=matter_type,
            raw_app_route=_first_non_empty(
                form_data.get("_forced_app_route"),
                form_data.get("app_route"),
                create_kind.forced_app_route if create_kind else "",
            ),
        )
        if forced_app_route:
            form_data["_forced_app_route"] = forced_app_route
            form_data["app_route"] = forced_app_route
        if create_kind and create_kind.public_type == "COPYRIGHT":
            form_data.setdefault("right_type", "")
            form_data.setdefault("case_kind", "")

        # Basic validation of context/args
        raw_form_division = (form_data.get("division") or "").strip()
        raw_form_type = (form_data.get("case_type") or form_data.get("type") or "").strip()
        form_division = _normalize_case_division(raw_form_division)
        form_type = _normalize_case_type(raw_form_type)

        # Note: Extensive validation of args vs form vs context was in original.
        # UseCase handles most, but route should check 400s if needed.
        # For brevity, we trust the UseCase to validate logic, or we let the UseCase raise exceptions.
        # But we need basic info to render error page if UseCase fails.

        cmd = MatterCreateCommand(
            division=division,  # Context division
            case_type=matter_type,  # Context type
            form_data=form_data,
            files=request.files,
            actor_user_id=current_user.id,
            idempotency_key=idempotency_key,
        )

        op, created = reserve_operation(
            "case.create",
            request_id=idempotency_key,
            actor_id=current_user.id,
            summary_json={"actor_type": "user"},
            targets_json={"context": "create_matter"},
        )
        if not created and op and isinstance(op.summary_json, dict):
            existing_id = op.summary_json.get("matter_id") or op.summary_json.get("existing_id")
            if existing_id:
                flash("This request has already been processed.", "warning")
                if is_popup and invoice_id and existing_id:
                    return render_template(
                        "case/popup_done.html",
                        title="Matter Created",
                        back_url=url_for("case_work.case_detail", case_id=existing_id),
                    )
                return redirect(url_for("case_work.case_detail", case_id=existing_id))

        res: MatterCreateResult | None = None
        try:
            with db.session.begin_nested():
                res = MatterCreateApplyUseCase().execute(cmd, sess_store)
                if not res.success:
                    raise _RollbackCreateMatter(res)
                mark_operation_applied(
                    op,
                    summary_updates={
                        "matter_id": res.matter_id,
                        "existing_id": res.existing_id,
                        "request_id": idempotency_key,
                    },
                )
            db.session.commit()
        except _RollbackCreateMatter as exc:
            db.session.rollback()
            res = exc.result
        except IntegrityError as exc:
            db.session.rollback()
            if _is_our_ref_unique_violation(exc):
                flash("This Our Ref. already exists.", "danger")
                res = MatterCreateResult(success=False, error="This Our Ref. already exists.")
            else:
                current_app.logger.exception("create_matter POST integrity error")
                flash("A data integrity error occurred while saving.", "danger")
                res = MatterCreateResult(success=False, error="integrity_error")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("create_matter POST failed")
            flash("An error occurred while saving.", "danger")
            res = MatterCreateResult(success=False, error="internal_error")

        if res and res.success:
            target_matter_id = res.existing_id or res.matter_id
            _maybe_link_invoice(target_matter_id)  # after commit (best-effort)
            if res.existing_id:
                flash("This request has already been processed.", "warning")
                # Redirect logic for existing
                if res.redirect_to_list:
                    return redirect(url_for("case_work.case_list"))
                if is_popup and invoice_id and target_matter_id:
                    return render_template(
                        "case/popup_done.html",
                        title="Matter Created",
                        back_url=url_for("case_work.case_detail", case_id=target_matter_id),
                    )
                return redirect(url_for("case_work.case_detail", case_id=res.existing_id))

            flash("Matter created.", "success")
            if is_popup and invoice_id and target_matter_id:
                return render_template(
                    "case/popup_done.html",
                    title="Matter Created",
                    back_url=url_for("case_work.case_detail", case_id=target_matter_id),
                )
            return redirect(url_for("case_work.case_detail", case_id=res.matter_id))

        # Failure / Validation Error
        if not res:
            res = MatterCreateResult(success=False, error="internal_error")

        if res.validation_errors:
            missing_labels = [m.get("label", m.get("key", "")) for m in res.validation_errors]
            flash(f"Required fields are missing: {', '.join(missing_labels)}", "warning")
        elif res.error:
            flash(res.error, "warning" if "warning" in (res.error or "").lower() else "danger")

        # Render with errors (need to fetch layout again or reuse?)
        # We need to render the page again.
        # Ideally we redirect to GET but we lose form state.
        # So we re-render.
        # We need the layout again.

        # Re-fetch layout (UseCase logic reused or simplified direct call)
        field_layout, field_meta = CaseParameterService.get_field_layout_with_meta(
            division, matter_type
        )
        staff_picker = _build_staff_picker_context()
        staff_assignment = (
            _build_staff_assignment_context()
            if _is_valid_create_kind(division, matter_type)
            else {}
        )

        display_division = create_kind.public_division if create_kind else division
        display_case_type = create_kind.public_type if create_kind else matter_type
        template_flags = _build_template_case_flags(
            division=division,
            case_type=matter_type,
            display_division=display_division,
            display_case_type=display_case_type,
        )

        form_action_params = {}
        if create_kind:
            form_action_params = _build_create_route_params(
                create_kind,
                popup="1" if is_popup else "",
                invoice_id=invoice_id,
                client_id=client_id,
                family_link_target_id=(form_data.get("family_link_target_id") or "").strip(),
                family_create_mode=(
                    _normalize_family_create_mode(form_data.get("family_create_mode")) or ""
                ),
            )
        else:
            if matter_type:
                form_action_params["type"] = matter_type
            if division:
                form_action_params["division"] = division
            if is_popup:
                form_action_params["popup"] = "1"
            if invoice_id:
                form_action_params["invoice_id"] = invoice_id
        form_action = url_for("case_work.create_matter", **form_action_params)

        forced_app_route = _forced_app_route_for_create(
            division=division,
            matter_type=matter_type,
            raw_app_route=_first_non_empty(
                form_data.get("_forced_app_route"),
                form_data.get("app_route"),
            ),
        )
        if forced_app_route:
            form_data["_forced_app_route"] = forced_app_route
            form_data["app_route"] = forced_app_route

        return render_template(
            "case/matter_create.html",
            division=division,
            case_type=matter_type,
            display_division=display_division,
            display_case_type=display_case_type,
            dom_patent_fields=field_layout if template_flags["is_dom_pat"] else [],
            dom_design_fields=field_layout if template_flags["is_dom_design"] else [],
            dom_trademark_fields=field_layout if template_flags["is_dom_tm"] else [],
            inc_patent_fields=field_layout if template_flags["is_inc_pat"] else [],
            inc_design_fields=field_layout if template_flags["is_inc_design"] else [],
            inc_trademark_fields=field_layout if template_flags["is_inc_tm"] else [],
            out_patent_fields=field_layout if template_flags["is_out_pat"] else [],
            out_design_fields=field_layout if template_flags["is_out_design"] else [],
            out_trademark_fields=field_layout if template_flags["is_out_tm"] else [],
            pct_fields=field_layout if template_flags["is_pct"] else [],
            litigation_fields=field_layout if template_flags["is_litigation"] else [],
            misc_fields=field_layout if template_flags["is_misc"] else [],
            staff_picker=staff_picker,
            staff_assignment=staff_assignment,
            form_data=form_data,
            prefill=None,
            field_meta=field_meta,
            missing_fields=res.validation_errors or [],
            form_action=form_action,
            idempotency_key=idempotency_key,
            forced_app_route=forced_app_route,
            effective_family_create_mode=_normalize_family_create_mode(
                form_data.get("family_create_mode")
            ),
            **template_flags,
        )

    return _render_create(form_data)
