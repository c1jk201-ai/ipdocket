from __future__ import annotations

import re


def _to_positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value or 0)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _compact(value: object | None) -> str:
    try:
        raw = str(value or "").strip().upper()
    except Exception:
        return ""
    return re.sub(r"[^A-Z0-9]", "", raw)


def normalize_renewal_right_type(*values: object | None) -> str | None:
    """Return PATENT/UTILITY/DESIGN/TRADEMARK from common matter type aliases."""
    for value in values:
        compact = _compact(value)
        if not compact:
            continue

        if "TRADEMARK" in compact or compact in {"TM", "MARK", "T"}:
            return "TRADEMARK"
        if "DESIGN" in compact or compact in {"DES", "D"}:
            return "DESIGN"
        if "UTILITY" in compact or compact in {"UM", "UTILITYMODEL", "U"}:
            return "UTILITY"
        if "PATENT" in compact or compact in {"PAT", "PT", "P"}:
            return "PATENT"

        if len(compact) >= 4 and compact[:2].isdigit():
            code = compact[2:4]
            if code.startswith("T"):
                return "TRADEMARK"
            if code.startswith("D"):
                return "DESIGN"
            if code.startswith("U"):
                return "UTILITY"
            if code.startswith("P"):
                return "PATENT"

    return None


def normalize_renewal_jurisdiction(*values: object | None) -> str | None:
    """Return USPTO when a matter/profile token clearly belongs to U.S. practice."""
    for value in values:
        compact = _compact(value)
        if not compact:
            continue
        if compact in {"USPTO", "US", "USA", "UNITEDSTATES", "DOM", "INC"}:
            return "USPTO"
        if compact.startswith("US") and any(
            token in compact for token in ("TRADEMARK", "PATENT", "DESIGN", "UTILITY")
        ):
            return "USPTO"
        if len(compact) >= 4 and compact[:2].isdigit() and compact.endswith("US"):
            return "USPTO"
    return None


def _trademark_cycle_label(cycle: int | None) -> str:
    if not cycle:
        return " Updated"
    if cycle % 10 == 5:
        renewal_round = (cycle // 10) + 1
        if renewal_round <= 1:
            return "2 Registration"
        return f"{renewal_round} 2 Registration"
    if cycle % 10 == 0:
        return f"{cycle // 10} Updated"
    return "Updated"


def _uspto_trademark_cycle_label(cycle: int | None) -> str:
    if cycle == 6:
        return "Section 8 Declaration"
    if cycle and cycle % 10 == 0:
        return "Section 8/9 Renewal"
    return "USPTO Trademark Maintenance"


def renewal_cycle_label(
    cycle_no: object | None,
    *,
    right_type: str | None = None,
    jurisdiction: str | None = None,
) -> str:
    cycle = _to_positive_int(cycle_no)
    normalized_type = normalize_renewal_right_type(right_type)
    normalized_jurisdiction = normalize_renewal_jurisdiction(jurisdiction, right_type)

    if normalized_type == "TRADEMARK":
        if normalized_jurisdiction == "USPTO":
            return _uspto_trademark_cycle_label(cycle)
        return _trademark_cycle_label(cycle)

    return f"{cycle}" if cycle else " Renewal"


def renewal_workflow_name(
    cycle_no: object | None,
    *,
    right_type: str | None = None,
    jurisdiction: str | None = None,
) -> str:
    normalized_type = normalize_renewal_right_type(right_type)
    normalized_jurisdiction = normalize_renewal_jurisdiction(jurisdiction, right_type)
    label = renewal_cycle_label(
        cycle_no,
        right_type=normalized_type,
        jurisdiction=normalized_jurisdiction,
    )

    if normalized_type == "TRADEMARK":
        return f"Trademark {label}"

    return f"Renewal {label}" if label != " Renewal" else "Renewal"
