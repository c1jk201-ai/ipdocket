from __future__ import annotations

from typing import Any

GroupSpec = tuple[int, int | None, str]


_DEFAULT_GROUPS: dict[tuple[str, str], tuple[GroupSpec, ...]] = {
    ("DOM", "PATENT"): (
        (0, 8, "Default/Responsible"),
        (8, 10, "Invention/Title"),
        (10, 12, "Filing/Publication"),
        (12, 15, "Billing//"),
        (15, 21, "Priority/"),
        (21, 28, "Examination/Publication"),
        (28, 32, "Registration/"),
        (32, 40, "//"),
        (40, None, "Other/Closed"),
    ),
    ("DOM", "DESIGN"): (
        (0, 8, "Default/Responsible"),
        (8, 13, "Filing/Publication/Notice"),
        (13, 16, "Billing//"),
        (16, 19, "Applicant/Title"),
        (19, 24, "Priority/"),
        (24, 28, "Registration/"),
        (28, 34, "//"),
        (34, 36, "Foreign/Abandoned"),
        (36, None, "Other/Closed"),
    ),
    ("DOM", "TRADEMARK"): (
        (0, 8, "Default/Responsible"),
        (8, 15, "Filing/Publication/Notice"),
        (15, 19, "Applicant/Trademark"),
        (19, 23, "Priority/Parent application"),
        (23, 28, "Registration/"),
        (28, 36, "//"),
        (36, None, "Abandoned/Other/Closed"),
    ),
    ("INC", "PATENT"): (
        (0, 6, "Default/Responsible"),
        (6, 8, "PCT/EP"),
        (8, 16, "Filing/Publication"),
        (16, 19, "Applicant/Title"),
        (19, 24, "Priority/"),
        (24, 27, "Billing//"),
        (27, 31, "Registration/"),
        (31, 39, "//"),
        (39, 43, "Examination/Other"),
        (43, None, "Done/"),
    ),
    ("INC", "DESIGN"): (
        (0, 8, "Default/Responsible"),
        (8, 16, "EP/Filing/Publication"),
        (16, 20, "/Applicant/Title"),
        (20, 25, "Priority/"),
        (25, 30, "Billing/Registration"),
        (30, 36, "//"),
        (36, 38, "Abandoned/Other"),
        (38, None, "Done/"),
    ),
    ("INC", "TRADEMARK"): (
        (0, 8, "Default/Responsible"),
        (8, 15, "Filing/Publication"),
        (15, 19, "Applicant/Trademark"),
        (19, 23, "Priority/Parent application"),
        (23, 25, "/CTM"),
        (25, 30, "Registration/"),
        (30, 35, "//"),
        (35, None, "Abandoned/"),
    ),
    ("OUT", "PATENT"): (
        (0, 8, "Default/Responsible"),
        (8, 10, "PCT/EP"),
        (10, 14, "Filing/Publication/Examination"),
        (14, 18, "Applicant/Title"),
        (18, 23, "Priority/"),
        (23, 26, "Billing//"),
        (26, 32, "Registration/"),
        (32, 38, "//"),
        (38, None, "Other/Done/"),
    ),
    ("OUT", "DESIGN"): (
        (0, 9, "Default/Responsible"),
        (9, 14, "Image/Filing"),
        (14, 18, "Applicant/Title"),
        (18, 23, "Priority/"),
        (23, 27, "Registration/"),
        (27, 33, "//"),
        (33, None, "Abandoned/Other/Done"),
    ),
    ("OUT", "TRADEMARK"): (
        (0, 9, "Default/Responsible"),
        (9, 15, "Image/Filing"),
        (15, 19, "Applicant/Trademark"),
        (19, 24, "Priority/Parent application"),
        (24, 29, "Registration/"),
        (29, 36, "//"),
        (36, None, "Abandoned/Other/Done/"),
    ),
    ("OUT", "PCT"): (
        (0, 6, "Default/Responsible"),
        (6, 10, "Filing/Publication/Preliminary examination"),
        (10, 13, "Applicant/Title"),
        (13, 17, "Priority//"),
        (17, 20, "Domestic/"),
        (20, None, "Abandoned/Other/Closed"),
    ),
    ("ETC", "LITIGATION"): (
        (0, 6, "Default/Client"),
        (6, 10, "Responsible/"),
        (10, 17, "Case/Court"),
        (17, None, "Result/Other"),
    ),
    ("ETC", "MISC"): (
        (0, None, "Other"),
    ),
}


def _clean_token(value: Any) -> str:
    return str(value or "").strip().upper()


def _profile_key(division: Any, case_type: Any) -> tuple[str, str]:
    div = _clean_token(division)
    typ = _clean_token(case_type)
    if typ == "UTILITY":
        typ = "PATENT"
    if typ == "PCT":
        div = "OUT"
    if typ in {"LITIGATION", "MISC"}:
        div = "ETC"
    return div, typ


def default_group_specs(division: Any, case_type: Any) -> tuple[GroupSpec, ...]:
    return _DEFAULT_GROUPS.get(_profile_key(division, case_type), ())


def default_group_names() -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for specs in _DEFAULT_GROUPS.values():
        for _start, _end, label in specs:
            if label not in seen:
                seen.add(label)
                names.append(label)
    return names


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _coerce_col(value: Any) -> int:
    try:
        col = int(value)
    except Exception:
        col = 1
    return 2 if col == 2 else 1


def _json_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _mapping_target(mapping_key: Any) -> tuple[str, str]:
    parts = [part.strip().upper() for part in str(mapping_key or "").split(":") if part.strip()]
    if len(parts) >= 3 and parts[0] == "IP":
        return parts[1], parts[2]
    if len(parts) == 2 and parts[0] == "IP":
        return "ETC", parts[1]
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1 and parts[0] in {"LITIGATION", "MISC", "PCT"}:
        return "ETC", parts[0]
    return "", ""


def _spec_for_pair_index(specs: tuple[GroupSpec, ...], pair_index: int) -> tuple[str, float] | None:
    for spec_index, (start, end, label) in enumerate(specs):
        if pair_index >= start and (end is None or pair_index < end):
            return label, float((spec_index + 1) * 10)
    return None


def _default_order_for_label(specs: tuple[GroupSpec, ...], label: str) -> float | None:
    if not label:
        return None
    for spec_index, (_start, _end, spec_label) in enumerate(specs):
        if spec_label == label:
            return float((spec_index + 1) * 10)
    return None


def apply_default_field_groups(
    fields: list[dict[str, Any]],
    division: Any,
    case_type: Any,
) -> list[dict[str, Any]]:
    rows = [dict(field) for field in fields if isinstance(field, dict)]
    if not rows:
        return []

    specs = default_group_specs(division, case_type)
    indexed_rows: list[tuple[int, float, int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        order = _coerce_float(row.get("order"))
        if order is None:
            order = float(index + 1)
            row["order"] = order
        indexed_rows.append((index, order, _coerce_col(row.get("col")), row))

    pair_index_by_order: dict[float, int] = {}
    for _index, order, _col, _row in sorted(indexed_rows, key=lambda item: (item[1], item[2], item[0])):
        if order not in pair_index_by_order:
            pair_index_by_order[order] = len(pair_index_by_order)

    for _index, order, _col, row in indexed_rows:
        group = str(row.get("group") or row.get("section") or "").strip()
        group_order = _coerce_float(row.get("group_order", row.get("section_order")))
        spec = _spec_for_pair_index(specs, pair_index_by_order.get(order, 0)) if specs else None
        if spec:
            default_group, default_order = spec
            if not group:
                group = default_group
            if group_order is None:
                group_order = _default_order_for_label(specs, group) or default_order
        row["group"] = group
        normalized_group_order = _json_number(group_order)
        if normalized_group_order is None:
            row.pop("group_order", None)
        else:
            row["group_order"] = normalized_group_order

    return rows


def apply_default_field_groups_for_mapping_key(
    fields: list[dict[str, Any]],
    mapping_key: Any,
) -> list[dict[str, Any]]:
    division, case_type = _mapping_target(mapping_key)
    return apply_default_field_groups(fields, division, case_type)
