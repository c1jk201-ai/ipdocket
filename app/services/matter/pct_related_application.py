from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.extensions import db
from app.models.ip_records import Matter, MatterCustomField
from app.services.case.case_kind import resolve_public_case_kind_for_matter
from app.services.matter.status_normalization import date_only_str

def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + int(months)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return date(year, month, min(value.day, month_lengths[month - 1]))


_FIELD_LABELS = {
    "app_route": "Filing",
    "filing_deadline": "Filing deadline",
    "filing_deadline_type": "FilingDeadline Type",
    "priority_claimed": "Priority ",
    "priority_date": "Priority date",
    "priority_no": "Priority",
    "national_phase_19m_deadline": "Domestic Deadline 1  Notice",
    "national_phase_deadline": "Domestic Due date",
}

_RELATED_APPLICATION_TARGETS = {
    "pct": {
        "public_type": "PCT",
        "namespace": "pct",
        "target_label": "PCT",
        "storage_label": "PCT Registry",
    },
    "madrid": {
        "public_type": "MADRID",
        "namespace": "outgoing_trademark",
        "target_label": "Madrid",
        "storage_label": " Registry",
        "route_value": "Madrid",
        "deadline_months": 6,
    },
    "hague": {
        "public_type": "HAGUE",
        "namespace": "outgoing_design",
        "target_label": "Hague",
        "storage_label": " Registry",
        "route_value": "HAGUE",
        "deadline_months": 6,
    },
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date_only_str(str(value or ""))


def _parse_date(value: Any) -> date | None:
    text = _date_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _is_pct_matter(matter: Matter | None) -> bool:
    if not matter:
        return False
    matter_type = _clean(getattr(matter, "matter_type", "")).upper()
    our_ref = _clean(getattr(matter, "our_ref", "")).upper()
    return matter_type == "PCT" or our_ref.endswith("PCT")


def _load_custom_data(matter_id: str, namespace: str) -> dict:
    row = MatterCustomField.query.filter_by(matter_id=matter_id, namespace=namespace).first()
    return dict(row.data or {}) if row and isinstance(row.data, dict) else {}


def _field_value(data: dict, key: str) -> str:
    if key.endswith(("_date", "_deadline")):
        return _date_text(data.get(key))
    return _clean(data.get(key))


def _national_phase_months(*, pct_data: dict, matter: Matter) -> int:
    try:
        from app.services.deadlines.mgmt_deadlines import _resolve_pct_jurisdiction_codes

        filing_country, designated_country = _resolve_pct_jurisdiction_codes(
            custom_data=pct_data,
            right_group=getattr(matter, "right_group", None),
            matter_type=getattr(matter, "matter_type", None),
        )
        country = _clean(designated_country or filing_country).upper()
        return 30 if country == "US" else 30
    except Exception:
        return 30


def _row_rank(row: dict) -> tuple[int, str, str]:
    label = _clean(row.get("relation_label"))
    if "Priority" in label:
        relation_rank = 0
    elif "Parent application" in label:
        relation_rank = 1
    elif "Family" in label:
        relation_rank = 2
    else:
        relation_rank = 3
    return (relation_rank, _date_text(row.get("application_date")), _clean(row.get("our_ref")))


def _pick_related_application_row(related_family_rows: list[dict] | None) -> dict | None:
    candidates = [
        row
        for row in (related_family_rows or [])
        if isinstance(row, dict) and _parse_date(row.get("application_date"))
    ]
    if not candidates:
        return None
    return sorted(candidates, key=_row_rank)[0]


def _target_config_for_matter(matter: Matter | None) -> tuple[str, dict] | tuple[str, None]:
    if not matter:
        return "", None

    public_division, public_type = resolve_public_case_kind_for_matter(matter)
    public_type = _clean(public_type).upper()
    if public_type == "PCT" or _is_pct_matter(matter):
        return "pct", _RELATED_APPLICATION_TARGETS["pct"]
    if public_division == "ETC" and public_type == "MADRID":
        return "madrid", _RELATED_APPLICATION_TARGETS["madrid"]
    if public_division == "ETC" and public_type == "HAGUE":
        return "hague", _RELATED_APPLICATION_TARGETS["hague"]
    return "", None


def _candidate_fields_for_target(
    *,
    target_key: str,
    config: dict,
    data: dict,
    related_app_date: date,
    related_app_no: str,
    matter: Matter,
) -> tuple[list[tuple[str, str]], str, str, int | None]:
    current_priority_date = _parse_date(data.get("priority_date"))
    basis_date = current_priority_date or related_app_date
    basis_label = "Current Registry " if current_priority_date else "Related applications Filing date"

    if target_key == "pct":
        months = _national_phase_months(pct_data=data, matter=matter)
        national_deadline = add_months(basis_date, months).isoformat()
        one_year_notice_deadline = add_months(basis_date, 18).isoformat()
        candidates: list[tuple[str, str]] = [
            ("priority_date", related_app_date.isoformat()),
            ("national_phase_19m_deadline", one_year_notice_deadline),
            ("national_phase_deadline", national_deadline),
        ]
        if related_app_no:
            candidates.insert(1, ("priority_no", related_app_no))
        return candidates, basis_date.isoformat(), basis_label, months

    deadline_months = int(config.get("deadline_months") or 6)
    filing_deadline = add_months(basis_date, deadline_months).isoformat()
    candidates = [
        ("app_route", _clean(config.get("route_value"))),
        ("priority_claimed", "Y"),
        ("priority_date", related_app_date.isoformat()),
        ("filing_deadline_type", "LEGAL"),
        ("filing_deadline", filing_deadline),
    ]
    if related_app_no:
        candidates.insert(3, ("priority_no", related_app_no))
    return candidates, basis_date.isoformat(), basis_label, deadline_months


def build_related_application_suggestion(
    *,
    matter: Matter | None,
    related_family_rows: list[dict] | None,
    custom_data: dict | None = None,
) -> dict | None:
    """Return a conservative field-fill suggestion from related application data."""
    target_key, config = _target_config_for_matter(matter)
    if not config:
        return None

    row = _pick_related_application_row(related_family_rows)
    if not row:
        return None

    namespace = _clean(config.get("namespace"))
    data = dict(
        custom_data
        if custom_data is not None
        else _load_custom_data(str(matter.matter_id), namespace)
    )
    related_app_date = _parse_date(row.get("application_date"))
    if not related_app_date:
        return None

    related_app_no = _clean(row.get("application_no"))
    candidates, basis_date, basis_label, deadline_months = _candidate_fields_for_target(
        target_key=target_key,
        config=config,
        data=data,
        related_app_date=related_app_date,
        related_app_no=related_app_no,
        matter=matter,
    )

    fields = []
    for key, value in candidates:
        current_value = _field_value(data, key)
        if current_value:
            continue
        fields.append(
            {
                "key": key,
                "label": _FIELD_LABELS.get(key, key),
                "value": value,
                "current": current_value,
            }
        )

    if not fields:
        return None

    source = {
        "matter_id": _clean(row.get("matter_id")),
        "our_ref": _clean(row.get("our_ref")),
        "relation_label": _clean(row.get("relation_label")) or "Related applications",
        "application_date": related_app_date.isoformat(),
        "application_no": related_app_no,
        "title": _clean(row.get("title")),
    }
    suggestion = {
        "target": target_key,
        "target_label": _clean(config.get("target_label")),
        "storage_label": _clean(config.get("storage_label")),
        "namespace": namespace,
        "source": source,
        "basis_date": basis_date,
        "basis_label": basis_label,
        "deadline_months": deadline_months,
        "fields": fields,
    }
    if target_key == "pct":
        suggestion["national_phase_months"] = deadline_months
    return suggestion


def build_pct_related_application_suggestion(
    *,
    matter: Matter | None,
    related_family_rows: list[dict] | None,
    pct_data: dict | None = None,
) -> dict | None:
    """Return a conservative PCT-field fill suggestion from related application data."""
    suggestion = build_related_application_suggestion(
        matter=matter,
        related_family_rows=related_family_rows,
        custom_data=pct_data,
    )
    if not suggestion or suggestion.get("target") != "pct":
        return None
    return suggestion


def apply_related_application_suggestion(
    *,
    matter: Matter,
    related_family_rows: list[dict] | None,
) -> dict:
    matter_id = _clean(getattr(matter, "matter_id", ""))
    if not matter_id:
        return {"changed": False, "reason": "matter_required", "changes": []}

    target_key, config = _target_config_for_matter(matter)
    if not config:
        return {"changed": False, "reason": "unsupported_target", "changes": []}

    namespace = _clean(config.get("namespace"))
    row = MatterCustomField.query.filter_by(matter_id=matter_id, namespace=namespace).first()
    data = dict(row.data or {}) if row and isinstance(row.data, dict) else {}
    before_data = dict(data)
    suggestion = build_related_application_suggestion(
        matter=matter,
        related_family_rows=related_family_rows,
        custom_data=data,
    )
    if not suggestion:
        result = {
            "changed": False,
            "reason": "no_suggestion",
            "changes": [],
            "data": data,
            "before_data": before_data,
            "namespace": namespace,
            "target": target_key,
            "target_label": _clean(config.get("target_label")),
            "storage_label": _clean(config.get("storage_label")),
        }
        if target_key == "pct":
            result["pct_data"] = data
        return result

    changes = []
    for field in suggestion.get("fields") or []:
        key = _clean(field.get("key"))
        value = _clean(field.get("value"))
        if not key or not value or _field_value(data, key):
            continue
        data[key] = value
        changes.append(field)

    if not changes:
        result = {
            "changed": False,
            "reason": "no_missing_fields",
            "changes": [],
            "data": data,
            "before_data": before_data,
            "namespace": namespace,
            "target": target_key,
            "target_label": _clean(config.get("target_label")),
            "storage_label": _clean(config.get("storage_label")),
        }
        if target_key == "pct":
            result["pct_data"] = data
        return result

    if not row:
        row = MatterCustomField(matter_id=matter_id, namespace=namespace, data={})
        db.session.add(row)
    row.data = data
    result = {
        "changed": True,
        "reason": "",
        "changes": changes,
        "data": data,
        "before_data": before_data,
        "namespace": namespace,
        "target": target_key,
        "target_label": _clean(config.get("target_label")),
        "storage_label": _clean(config.get("storage_label")),
        "suggestion": suggestion,
    }
    if target_key == "pct":
        result["pct_data"] = data
    return result


def apply_pct_related_application_suggestion(
    *,
    matter: Matter,
    related_family_rows: list[dict] | None,
) -> dict:
    result = apply_related_application_suggestion(
        matter=matter,
        related_family_rows=related_family_rows,
    )
    if result.get("target") != "pct":
        return {"changed": False, "reason": "unsupported_target", "changes": []}
    return result
