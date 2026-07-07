"""Case kind inference helpers.

Why this module exists:
    This logic used to live under `app.blueprints.case.*`, but importing a module inside the
    `app.blueprints.case` package triggers `app/blueprints/case/__init__.py`, which eagerly imports
    route modules. Services (e.g. `canonical_field_service`) importing those helpers could then
    create circular imports.

This module is intentionally blueprint-free and safe to import from services/scripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.ip_records import (
    Matter,
    MatterCustomField,
    MatterIdentifier,
    RawImportField,
    VMatterOverview,
)


def _has_litigation_keyword(value: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    if "" in raw or "Litigation" in raw:
        return True
    if re.search(r"\s*", raw) or re.search(r"\s*", raw):
        return True
    if "litigation" in raw.lower():
        return True
    return False


def _infer_case_kind_from_right_name(right_name: str) -> tuple[str, str]:
    raw = (right_name or "").strip()
    if not raw:
        return ("", "")
    lower = raw.lower()
    if _has_litigation_keyword(raw):
        return ("", "LITIGATION")

    division = ""
    if "incoming" in lower or "" in raw or "inbound" in lower or "" in raw:
        division = "INC"
    elif "outgoing" in lower or "Foreign" in raw or "foreign" in lower:
        division = "OUT"
    elif "Domestic" in raw or "domestic" in lower:
        division = "DOM"

    matter_type = ""
    if "Utility model" in raw or "Utility model" in raw or "Utility modelRegistration" in raw:
        matter_type = "UTILITY"
    elif "utility" in lower or "utilitymodel" in lower:
        matter_type = "UTILITY"
    elif "PCT" in raw.upper() or "Filing" in raw:
        matter_type = "PCT"
    elif "Patent" in raw or "patent" in lower:
        matter_type = "PATENT"
    elif "Design" in raw or "design" in lower:
        matter_type = "DESIGN"
    elif (
        any(token in raw for token in ("Trademark", "Servicetable", "table", "Tasktable"))
        or "trademark" in lower
    ):
        matter_type = "TRADEMARK"

    return (division, matter_type)


def _infer_case_kind_from_app_no(app_no: str) -> tuple[str, str]:
    raw = (app_no or "").strip().replace(" ", "")
    if not raw:
        return ("", "")
    match = re.search(r"(?i)(?:US)?(\d{2})-", raw) or re.match(r"(?i)(?:US)?(\d{2})", raw)
    if not match:
        return ("", "")
    prefix = match.group(1)
    if prefix == "10":
        return ("DOM", "PATENT")
    if prefix == "20":
        return ("DOM", "UTILITY")
    if prefix == "30":
        return ("DOM", "DESIGN")
    if prefix == "40":
        return ("DOM", "TRADEMARK")
    return ("", "")


def _lookup_app_no(matter_id: str) -> str:
    row = MatterIdentifier.query.filter(
        MatterIdentifier.matter_id == str(matter_id),
        MatterIdentifier.id_type.in_(("Application No.", "APP_NO", "application_no", "app_no")),
    ).first()
    if row:
        value = str(row.id_value or "").strip()
        if value:
            return value
    row = MatterIdentifier.query.filter_by(matter_id=str(matter_id), id_type="Parent application No.").first()
    if row:
        value = str(row.id_value or "").strip()
        if value:
            return value
    return ""


def _lookup_raw_right_label(raw_id: str | None) -> str:
    rid = (raw_id or "").strip()
    if not rid:
        return ""
    row = (
        RawImportField.query.filter_by(raw_id=rid, sheet_name="Matter", source_column="")
        .order_by(RawImportField.created_at.desc())
        .first()
    )
    if row and (row.value_text or "").strip():
        return (row.value_text or "").strip()
    row = (
        RawImportField.query.filter_by(raw_id=rid, sheet_name="Matter", source_column="")
        .order_by(RawImportField.created_at.desc())
        .first()
    )
    if row and (row.value_text or "").strip():
        return (row.value_text or "").strip()
    return ""


def _normalize_case_division(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in ("DOM", "INC", "OUT", "ETC"):
        return upper
    if upper in ("INCOMING", "INBOUND"):
        return "INC"
    if upper in ("OUTGOING", "OUTBOUND", "FOREIGN"):
        return "OUT"
    if upper in ("DOMESTIC",):
        return "DOM"
    if upper in ("MISC", "OTHER"):
        return "ETC"
    if raw in ("Domestic", ""):
        return "DOM"
    if raw in ("", "Matter", ""):
        return "INC"
    if raw in ("Foreign", "Foreign", "", ""):
        return "OUT"
    if raw in ("Other", "OtherMatter"):
        return "ETC"
    return ""


def _normalize_case_type(value: str | None) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    compact = re.sub(r"\s+", "", upper)
    compact_raw = re.sub(r"\s+", "", raw)
    if upper in (
        "PATENT",
        "DESIGN",
        "TRADEMARK",
        "LITIGATION",
        "UTILITY",
        "MISC",
        "PCT",
        "MADRID",
        "HAGUE",
        "COPYRIGHT",
    ):
        return upper
    if upper in ("OTHER", "ETC"):
        return "MISC"
    if compact in ("UTILITYMODEL", "UM"):
        return "UTILITY"
    if "PCT" in compact or "Filing" in raw:
        return "PCT"
    if raw in ("Patent", "Patent"):
        return "PATENT"
    if raw in ("Utility model", "Utility model", "Utility modelRegistration"):
        return "UTILITY"
    if raw == "Design":
        return "DESIGN"
    if raw in ("Trademark", "Trademark", "Servicetable", "table", "Tasktable", "Display table"):
        return "TRADEMARK"
    if raw in ("", "Litigation", "/Litigation"):
        return "LITIGATION"
    if raw in ("", " ", "", " ", ""):
        return "LITIGATION"
    if compact_raw in ("//Litigation",):
        return "LITIGATION"
    if raw in ("Other", "Other", "OtherMatter", "OtherItem"):
        return "MISC"
    if raw in ("",):
        return "COPYRIGHT"
    return ""


PATENT_LIKE_TYPES = {"PATENT", "UTILITY"}
SPECIAL_ETC_TYPES = frozenset({"PCT", "MADRID", "HAGUE", "COPYRIGHT", "LITIGATION", "MISC"})
USPTO_MANAGED_SPECIAL_TYPES = frozenset({"PCT", "MADRID", "HAGUE"})
USPTO_TRIAL_KEYWORDS = (
    "Patent",
    "",
    "",
    "Billing",
    "Void",
    "Confirm",
    "Cancel",
    "",
    "",
)
NON_USPTO_TRIAL_KEYWORDS = (
    "Patent",
    "",
    "",
    "",
    "",
    "Litigation",
    "lawsuit",
    "court",
)


def resolve_profile_case_kind(
    division: str | None,
    case_type: str | None,
) -> tuple[str, str]:
    div = _normalize_case_division(division)
    typ = _normalize_case_type(case_type)
    if typ == "MADRID":
        return ("OUT", "TRADEMARK")
    if typ == "HAGUE":
        return ("OUT", "DESIGN")
    if typ == "COPYRIGHT":
        return ("", "MISC")
    if typ == "LITIGATION":
        return ("", "LITIGATION")
    if typ == "MISC":
        return ("", "MISC")
    if div == "ETC" and typ == "PCT":
        return ("OUT", "PCT")
    return (div, typ)


def public_case_type_label(case_type: str | None) -> str:
    typ = (case_type or "").strip().upper()
    if typ == "PATENT":
        return "Patent"
    if typ == "UTILITY":
        return "Utility"
    if typ == "DESIGN":
        return "Design"
    if typ == "TRADEMARK":
        return "Trademark"
    if typ == "PCT":
        return "PCT"
    if typ == "MADRID":
        return "Madrid"
    if typ == "HAGUE":
        return "Hague"
    if typ == "COPYRIGHT":
        return "Copyright"
    if typ == "LITIGATION":
        return "Proceedings / Litigation"
    if typ == "MISC":
        return "Other"
    return (case_type or "").strip().upper()


def resolve_public_case_kind(
    division: str | None,
    case_type: str | None,
    *,
    is_madrid: bool = False,
    is_hague: bool = False,
    is_copyright: bool = False,
) -> tuple[str, str]:
    div = _normalize_case_division(division)
    typ = _normalize_case_type(case_type)
    if div == "ETC" and typ in SPECIAL_ETC_TYPES:
        if typ == "MISC":
            return ("ETC", "COPYRIGHT" if is_copyright else "MISC")
        return ("ETC", typ)
    if typ in {"MADRID", "HAGUE", "COPYRIGHT", "LITIGATION"}:
        return ("ETC", typ)
    if typ == "PCT":
        return ("ETC", "PCT")
    if typ == "LITIGATION":
        return ("ETC", "LITIGATION")
    if typ == "MISC":
        return ("ETC", "COPYRIGHT" if is_copyright else "MISC")
    if div == "OUT" and typ == "TRADEMARK" and is_madrid:
        return ("ETC", "MADRID")
    if div == "OUT" and typ == "DESIGN" and is_hague:
        return ("ETC", "HAGUE")
    return (div, typ)


def is_uspto_managed_case_kind(
    division: str | None,
    case_type: str | None,
    *,
    is_madrid: bool = False,
    is_hague: bool = False,
    is_copyright: bool = False,
) -> bool:
    public_division, public_type = resolve_public_case_kind(
        division,
        case_type,
        is_madrid=is_madrid,
        is_hague=is_hague,
        is_copyright=is_copyright,
    )
    return public_division in {"DOM", "INC"} or public_type in USPTO_MANAGED_SPECIAL_TYPES


def format_public_case_kind_label(
    division: str | None,
    case_type: str | None,
    *,
    is_madrid: bool = False,
    is_hague: bool = False,
    is_copyright: bool = False,
) -> str:
    public_div, public_type = resolve_public_case_kind(
        division,
        case_type,
        is_madrid=is_madrid,
        is_hague=is_hague,
        is_copyright=is_copyright,
    )
    type_label = public_case_type_label(public_type)
    if public_div == "DOM":
        return f"US - {type_label}"
    if public_div == "INC":
        return f"Inbound US - {type_label}"
    if public_div == "OUT":
        return f"Foreign - {type_label}"
    if public_div == "ETC":
        return f"Other Matter - {type_label}"
    raw_div = (division or "").strip().upper()
    raw_type = (case_type or "").strip().upper()
    return f"{raw_div} {raw_type}".strip()


def _contains_case_keyword(value: str | None, *keywords: str) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def _custom_field_payloads(matter_id: str | None, namespaces: tuple[str, ...]) -> list[dict]:
    mid = (matter_id or "").strip()
    if not mid:
        return []
    rows = (
        MatterCustomField.query.filter(MatterCustomField.matter_id == mid)
        .filter(MatterCustomField.namespace.in_(namespaces))
        .all()
    )
    out: list[dict] = []
    for row in rows or []:
        if isinstance(row.data, dict):
            out.append(row.data)
    return out


def _matter_has_madrid_markers(matter: Matter, overview: VMatterOverview | None = None) -> bool:
    payloads = _custom_field_payloads(
        getattr(matter, "matter_id", None),
        ("outgoing_trademark", "out_trademark"),
    )
    for data in payloads:
        if _contains_case_keyword(data.get("app_route"), "madrid", "\ub9c8\ub4dc\ub9ac\ub4dc"):
            return True
        if any(
            str(data.get(key) or "").strip()
            for key in ("madrid_application_no", "madrid_application_date")
        ):
            return True
    for value in (getattr(matter, "right_name", None), getattr(overview, "right_name", None)):
        if _contains_case_keyword(value, "madrid", "\ub9c8\ub4dc\ub9ac\ub4dc"):
            return True
    return False


def _matter_has_hague_markers(matter: Matter, overview: VMatterOverview | None = None) -> bool:
    payloads = _custom_field_payloads(
        getattr(matter, "matter_id", None),
        ("outgoing_design", "out_design"),
    )
    for data in payloads:
        if _contains_case_keyword(data.get("app_route"), "hague", "\ud5e4\uc774\uadf8"):
            return True
        if any(
            str(data.get(key) or "").strip()
            for key in ("hague_application_no", "hague_application_date")
        ):
            return True
    for value in (getattr(matter, "right_name", None), getattr(overview, "right_name", None)):
        if _contains_case_keyword(value, "hague", "\ud5e4\uc774\uadf8"):
            return True
    return False


def _matter_is_copyright_misc(matter: Matter, overview: VMatterOverview | None = None) -> bool:
    payloads = _custom_field_payloads(getattr(matter, "matter_id", None), ("misc",))
    for data in payloads:
        if _contains_case_keyword(data.get("right_type"), "copyright", "\uc800\uc791\uad8c"):
            return True
        if _contains_case_keyword(data.get("case_kind"), "copyright", "\uc800\uc791\uad8c"):
            return True
    for value in (getattr(matter, "right_name", None), getattr(overview, "right_name", None)):
        if _contains_case_keyword(value, "copyright", "\uc800\uc791\uad8c"):
            return True
    return False


def resolve_public_case_kind_for_matter(
    matter: Matter | None,
    overview: VMatterOverview | None = None,
) -> tuple[str, str]:
    if not matter:
        return ("", "")
    raw_div = _normalize_case_division(
        getattr(matter, "right_group", None)
    ) or _normalize_case_division(getattr(overview, "right_group", None) if overview else "")
    raw_typ = _normalize_case_type(getattr(matter, "matter_type", None)) or _normalize_case_type(
        getattr(overview, "matter_type", None) if overview else ""
    )
    has_copyright_markers = raw_typ in {"MISC", "COPYRIGHT"} and _matter_is_copyright_misc(
        matter, overview
    )
    if raw_div == "ETC" and raw_typ in SPECIAL_ETC_TYPES:
        if raw_typ == "MISC":
            return ("ETC", "COPYRIGHT" if has_copyright_markers else "MISC")
        return ("ETC", raw_typ)
    div, typ = _infer_case_kind(matter, overview)
    is_madrid = div == "OUT" and typ == "TRADEMARK" and _matter_has_madrid_markers(matter, overview)
    is_hague = div == "OUT" and typ == "DESIGN" and _matter_has_hague_markers(matter, overview)
    is_copyright = typ == "MISC" and has_copyright_markers
    return resolve_public_case_kind(
        div,
        typ,
        is_madrid=is_madrid,
        is_hague=is_hague,
        is_copyright=is_copyright,
    )


def is_uspto_managed_matter(
    matter: Matter | None,
    overview: VMatterOverview | None = None,
) -> bool:
    public_division, public_type = resolve_public_case_kind_for_matter(matter, overview)
    return public_division in {"DOM", "INC"} or public_type in USPTO_MANAGED_SPECIAL_TYPES


def _matter_is_uspto_trial(matter: Matter, overview: VMatterOverview | None = None) -> bool:
    raw_type = _normalize_case_type(getattr(matter, "matter_type", None)) or _normalize_case_type(
        getattr(overview, "matter_type", None) if overview else ""
    )
    public_division, public_type = resolve_public_case_kind_for_matter(matter, overview)
    if raw_type != "LITIGATION" and public_type != "LITIGATION":
        return False

    values: list[str] = []
    for value in (
        getattr(matter, "right_name", None),
        getattr(overview, "right_name", None) if overview else None,
        getattr(matter, "right_group", None),
        public_division,
    ):
        if (value or "").strip():
            values.append(str(value).strip())

    for data in _custom_field_payloads(getattr(matter, "matter_id", None), ("litigation",)):
        for key in (
            "proposal_title",
            "case_name",
            "title",
            "court",
            "court_other",
            "department",
            "stand_reason",
        ):
            value = str(data.get(key) or "").strip()
            if value:
                values.append(value)

    merged = "\n".join(values)
    if not merged:
        return False

    if "Patent" in merged:
        return True
    if any(keyword in merged for keyword in NON_USPTO_TRIAL_KEYWORDS):
        return False
    return any(keyword in merged for keyword in USPTO_TRIAL_KEYWORDS)


def is_uspto_response_upload_matter(
    matter: Matter | None,
    overview: VMatterOverview | None = None,
) -> bool:
    """Return whether USPTO document/PDF response uploads may be processed for a matter.

    This is intentionally a little wider than "USPTO-managed matter": domestic
    USPTO trial matters are stored as ETC/LITIGATION, but still receive USPTO documents
    such as Billing.
    """

    if matter is None:
        return False
    if is_uspto_managed_matter(matter, overview):
        return True
    return _matter_is_uspto_trial(matter, overview)


@dataclass(frozen=True)
class CaseKind:
    division: str
    case_type: str

    @classmethod
    def from_values(cls, division: str | None, case_type: str | None) -> "CaseKind":
        div, typ = resolve_profile_case_kind(division, case_type)
        if typ in ("LITIGATION", "MISC"):
            div = ""
        return cls(div, typ)

    @property
    def is_pct(self) -> bool:
        return self.case_type == "PCT"

    @property
    def is_litigation(self) -> bool:
        return self.case_type == "LITIGATION"

    @property
    def is_misc(self) -> bool:
        return self.case_type == "MISC"

    @property
    def is_patent_like(self) -> bool:
        return self.case_type in PATENT_LIKE_TYPES

    @property
    def is_dom(self) -> bool:
        return self.division == "DOM"

    @property
    def is_inc(self) -> bool:
        return self.division == "INC"

    @property
    def is_out(self) -> bool:
        return self.division == "OUT"

    @property
    def is_dom_pat(self) -> bool:
        return self.is_dom and self.is_patent_like

    @property
    def is_dom_design(self) -> bool:
        return self.is_dom and self.case_type == "DESIGN"

    @property
    def is_dom_tm(self) -> bool:
        return self.is_dom and self.case_type == "TRADEMARK"

    @property
    def is_inc_pat(self) -> bool:
        return self.is_inc and self.is_patent_like

    @property
    def is_inc_design(self) -> bool:
        return self.is_inc and self.case_type == "DESIGN"

    @property
    def is_inc_tm(self) -> bool:
        return self.is_inc and self.case_type == "TRADEMARK"

    @property
    def is_out_pat(self) -> bool:
        return self.is_out and self.is_patent_like

    @property
    def is_out_design(self) -> bool:
        return self.is_out and self.case_type == "DESIGN"

    @property
    def is_out_tm(self) -> bool:
        return self.is_out and self.case_type == "TRADEMARK"


def _infer_case_kind(matter: Matter, overview: VMatterOverview | None = None) -> tuple[str, str]:
    """
    Priority:
      1) explicit DB values
      2) application number prefix
      3) legacy raw import label
    """
    raw_typ = _normalize_case_type(matter.matter_type) or _normalize_case_type(
        overview.matter_type if overview else ""
    )
    raw_div = _normalize_case_division(matter.right_group) or _normalize_case_division(
        overview.right_group if overview else ""
    )
    div, typ = resolve_profile_case_kind(raw_div, raw_typ)

    if typ in ("LITIGATION", "MISC"):
        return ("", typ)

    if not typ or not div:
        app_div, app_typ = _infer_case_kind_from_app_no(_lookup_app_no(matter.matter_id))
        if not typ and app_typ:
            typ = app_typ
        if not div and app_div:
            div = app_div

    if not typ or not div:
        right_label_seed = _lookup_raw_right_label(matter.raw_id)
        if right_label_seed:
            label_div, label_typ = _infer_case_kind_from_right_name(right_label_seed)
            if not typ and label_typ:
                if label_typ == "LITIGATION":
                    return ("", "LITIGATION")
                typ = label_typ
            if not div and label_div:
                div = label_div

    if typ and not div:
        if typ == "PCT":
            div = "OUT"
        elif typ == "MISC":
            div = ""
        else:
            div = "DOM"
    return (div, typ)


def _apply_case_kind_to_matter(matter: Matter, division: str, matter_type: str) -> bool:
    div = _normalize_case_division(division)
    typ = _normalize_case_type(matter_type)
    if typ == "PCT":
        div = "ETC"
    elif typ in {"MADRID", "HAGUE", "COPYRIGHT", "LITIGATION", "MISC"}:
        div = "ETC"

    changed = False
    if typ in SPECIAL_ETC_TYPES:
        if (matter.matter_type or "").strip() != typ:
            matter.matter_type = typ
            changed = True
        if (matter.right_group or "").strip() != div:
            matter.right_group = div
            changed = True
        return changed
    if div and (matter.right_group or "").strip() != div:
        matter.right_group = div
        changed = True
    if typ and (matter.matter_type or "").strip() != typ:
        matter.matter_type = typ
        changed = True
    return changed
