from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable

from flask import current_app, flash

from app.services.case.helpers_staff import _BASIC_CANONICAL_STAFF_KEYS
from app.utils.error_logging import report_swallowed_exception

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
_FORM_ROUTING_KEYS = {
    "category",
    "in_out_type",
}
_FORM_OPTIONAL_PROFILE_KEYS = {
    "application_country",
}


def should_skip_custom_field_filter_key(key: str, allowed_keys: set[str]) -> bool:
    if key in _FORM_IGNORE_KEYS or key in _FORM_CORE_KEYS or key in _BASIC_CANONICAL_STAFF_KEYS:
        return True
    if key in _FORM_ROUTING_KEYS:
        return True
    if key in _FORM_OPTIONAL_PROFILE_KEYS and key not in allowed_keys:
        return True
    return False


def validate_application_number(value: str) -> bool:
    if not value:
        return True
    return bool(re.match(r"^\d{2}-\d{4}-\d{7}$", value.strip()))


def allowed_keys_from_fields(fields, extra_allowed: Iterable[str] | None = None) -> set[str]:
    keys: set[str] = set()
    for row in fields:
        for cell in row:
            try:
                key = cell[1]
            except (TypeError, IndexError):
                continue
            if key and key not in ("__blank__",):
                keys.add(key)
    if extra_allowed:
        keys |= set(extra_allowed)
    return keys


def is_yes(value: str | None) -> bool:
    return (value or "").strip().lower() in {"y", "yes", "true", "1"}


def normalize_our_ref_input(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return re.sub(r"[\s\-_\/]+", "", raw).upper()


def normalize_date_input(value: str | None, label: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        flash(f"{label}   .", "warning")
        return None


def parse_int(value: Any, default: Any = None) -> Any:
    try:
        if value in (None, ""):
            return default
        return int(str(value).strip())
    except Exception:
        return default


def get_case_date(case: Any, field_name: str) -> Any:
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


def _field_label(field: Any, key: str) -> str:
    return str(getattr(field, "label", None) or key)


def _is_bool_token(value: str) -> bool:
    return value.strip().lower() in {
        "y",
        "yes",
        "true",
        "1",
        "on",
        "t",
        "n",
        "no",
        "false",
        "0",
        "off",
        "f",
    }


def _normalize_number_value(raw: str, field: Any, key: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    label = _field_label(field, key)
    serializer = str(getattr(field, "serializer", "") or "").strip()
    if serializer == "int":
        if not re.match(r"^[+-]?\d+$", value):
            raise ValueError(f"Invalid number for {label}: {value}")
        return str(int(value))
    if not re.match(r"^[+-]?\d+(\.\d+)?$", value):
        raise ValueError(f"Invalid number for {label}: {value}")
    return value


def _validate_bool_value(raw: str, field: Any, key: str) -> None:
    value = (raw or "").strip()
    if not value:
        return
    if not _is_bool_token(value):
        label = _field_label(field, key)
        raise ValueError(f"Invalid yes/no value for {label}: {value}")


def validate_custom_field_updates(
    *,
    matter_id: str,
    namespace: str,
    form_data: dict,
    allowed_keys: list[str],
    strict_dates: bool = True,
) -> dict:
    updates: dict[str, str] = {}
    invalid_fields: list[tuple[str, str]] = []
    try:
        from app.services.case_fields.registry import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
    except Exception:
        registry = None

    for key in allowed_keys:
        if key in _BASIC_CANONICAL_STAFF_KEYS or key not in form_data:
            continue
        raw = (form_data.get(key) or "").strip()
        field = registry.get(key) if registry else None
        if _is_date_field(key):
            normalized = _normalize_date_strict(raw)
            if normalized is None:
                label = key
                if field and field.label:
                    label = field.label
                invalid_fields.append((key, label))
                try:
                    from app.services.automation.parse_failure import record_parse_failure

                    record_parse_failure(
                        kind="date",
                        raw_value=raw,
                        error="invalid_format",
                        source="case.custom_update",
                        field_name=key,
                        entity_type="matter",
                        entity_id=matter_id,
                        extra={"namespace": namespace},
                    )
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="case.form_support.validate_custom_field_updates.record_parse_failure",
                        log_key="case.form_support.validate_custom_field_updates.record_parse_failure",
                        log_window_seconds=300,
                    )
                continue
            updates[key] = normalized
            continue
        if field and field.input_type == "select" and field.options and raw:
            allowed_values = {
                str(option.get("value") or "").strip()
                for option in field.options
                if isinstance(option, dict)
            }
            if raw not in allowed_values:
                label = field.label or key
                raise ValueError(f"Invalid option for {label}: {raw}")
        if field and (field.input_type == "select_yn" or field.serializer == "bool"):
            _validate_bool_value(raw, field, key)
        if field and (field.input_type == "number" or field.serializer == "int"):
            raw = _normalize_number_value(raw, field, key)
        updates[key] = raw

    if invalid_fields and strict_dates:
        labels = ", ".join(dict.fromkeys(label for _, label in invalid_fields))
        raise ValueError(f"Invalid date format: {labels}")
    return updates


def log_custom_field_filtering(
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
