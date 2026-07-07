from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_MONEY_QUANT = Decimal("0.01")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        cleaned = str(value).replace(",", "").strip()
        return Decimal(cleaned or "0")
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _clamp_money(value: Decimal) -> Decimal:
    if abs(value) < Decimal("0.005"):
        return Decimal("0")
    return value


def _money_to_float(value: Decimal) -> float:
    return float(_clamp_money(_quantize_money(value)))
