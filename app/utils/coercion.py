from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

T = TypeVar("T")


def coerce_int(value: Any, default: T = None, *, strip: bool = True) -> int | T:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip() if strip else value
        if not text:
            return default
        try:
            return int(text)
        except (TypeError, ValueError, OverflowError):
            return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def coerce_float(
    value: Any,
    default: T = None,
    *,
    strip: bool = True,
    remove_commas: bool = False,
) -> float | T:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip() if strip else value
        if remove_commas:
            text = text.replace(",", "")
        if not text:
            return default
        try:
            return float(text)
        except (TypeError, ValueError, OverflowError):
            return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def coerce_decimal(
    value: Any,
    default: T = None,
    *,
    strip: bool = True,
    remove_commas: bool = False,
) -> Decimal | T:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip() if strip else value
        if remove_commas:
            text = text.replace(",", "")
        if not text:
            return default
        try:
            return Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def load_json(value: Any, default: T = None) -> Any | T:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def parse_iso_datetime(
    value: Any,
    default: T = None,
    *,
    strip: bool = True,
    z_suffix: bool = True,
) -> datetime | T:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip() if strip else value
    if not text:
        return default
    if z_suffix:
        text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return default


def parse_iso_date(value: Any, default: T = None, *, strip: bool = True) -> date | T:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip() if strip else value
    if not text:
        return default
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return default


def zoneinfo_or_default(name: str | None, *, default: str) -> ZoneInfo:
    candidate = (name or "").strip() or default
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return ZoneInfo(default)
