from __future__ import annotations

from datetime import datetime

from app.utils.coercion import (
    coerce_float,
    coerce_int,
    load_json,
    parse_iso_datetime,
    zoneinfo_or_default,
)


def test_coerce_int_handles_blank_and_invalid_values() -> None:
    assert coerce_int(" 42 ", None) == 42
    assert coerce_int("", 7) == 7
    assert coerce_int("not-a-number", 7) == 7


def test_coerce_float_accepts_grouped_number_strings() -> None:
    assert coerce_float("1,234.5", None, remove_commas=True) == 1234.5
    assert coerce_float("oops", 0.0) == 0.0


def test_load_json_returns_default_for_invalid_payload() -> None:
    assert load_json('{"ok": true}', None) == {"ok": True}
    assert load_json("{broken", {}) == {}


def test_parse_iso_datetime_supports_z_suffix() -> None:
    parsed = parse_iso_datetime("2026-03-15T09:30:00Z", None)

    assert isinstance(parsed, datetime)
    assert parsed.isoformat() == "2026-03-15T09:30:00+00:00"


def test_zoneinfo_or_default_falls_back_for_unknown_name() -> None:
    tz = zoneinfo_or_default("Not/A_Real_Timezone", default="America/New_York")

    assert getattr(tz, "key", None) == "America/New_York"
