"""Static FX-rate helpers for optional invoice currency conversion.

The public build is USD-first and does not call jurisdiction-specific exchange
rate providers. These helpers return stable sample rates relative to USD so the
invoice UI can render optional conversion controls without a network dependency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List
from zoneinfo import ZoneInfo

APP_TZ = ZoneInfo("America/New_York")


def _sample_rates(now_local: str) -> Dict[str, dict]:
    """Return static sample rates relative to USD."""
    return {
        "USD": {
            "base": "USD",
            "unit": 1,
            "base_rate": 1.00,
            "sending": 1.00,
            "receiving": 1.00,
            "source": "sample",
            "fetched_at": now_local,
        },
        "EUR": {
            "base": "USD",
            "unit": 1,
            "base_rate": 1.08,
            "sending": 1.08,
            "receiving": 1.08,
            "source": "sample",
            "fetched_at": now_local,
        },
        "GBP": {
            "base": "USD",
            "unit": 1,
            "base_rate": 1.27,
            "sending": 1.27,
            "receiving": 1.27,
            "source": "sample",
            "fetched_at": now_local,
        },
        "JPY": {
            "base": "USD",
            "unit": 1,
            "base_rate": 0.0067,
            "sending": 0.0067,
            "receiving": 0.0067,
            "source": "sample",
            "fetched_at": now_local,
        },
    }


def fetch_sample_rates_all() -> Dict[str, dict]:
    """Return all built-in sample FX rates.

    The function name is kept for compatibility with existing route imports.
    """
    return _sample_rates(datetime.now(APP_TZ).isoformat(timespec="seconds"))


def fetch_sample_rates(codes: List[str]) -> Dict[str, dict]:
    """Return built-in sample rates filtered to requested currency codes."""
    samples = _sample_rates(datetime.now(APP_TZ).isoformat(timespec="seconds"))
    out: Dict[str, dict] = {}
    for code in codes:
        currency = (code or "").upper().strip()
        if currency in samples:
            out[currency] = samples[currency]
    return out
