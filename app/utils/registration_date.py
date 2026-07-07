from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Iterator

from app.utils.docket_dates import parse_date

# ---------------------------------------------------------------------------
# Registration date key sets
# - Used by annuity auto-generation, matter_facts normalization, and listeners.
# - Keep these centralized to avoid drift across modules.
# ---------------------------------------------------------------------------

# Event keys (matter_event.event_key) that may represent a registration date.
# Note: matter_event uses localized human labels in many flows, so include them.
REGISTRATION_EVENT_KEYS: tuple[str, ...] = (
    # Common registration-date labels
    "Registration date",
    "Registration date",
    "SettingsRegistration date",
    "SettingsRegistration date",
    # English-ish variants (imports/legacy)
    "REGISTRATION_DATE",
    "REG_DATE",
    "REGISTRATION DATE",
    "REG DATE",
    "registration_date",
    "reg_date",
    # Form-specific labels that are sometimes the only populated date
    "DefaultDesignRegistration date",
    "EP Registration date",
    "Registration date",
)

# Custom-field keys (matter_custom_field.data keys) that may represent a registration date.
REGISTRATION_CUSTOM_KEYS: tuple[str, ...] = (
    # Standard keys
    "registration_date",
    "registrationDate",
    "reg_date",
    "regDate",
    # Variants seen in field registries/migrations
    "basic_registration_date",
    "original_registration_date",
    "ep_registration_date",
    "application_reg_date",
    # Label-key fallbacks (some imports store by label)
    "Registration date",
    "Registration date",
    "SettingsRegistration date",
    "SettingsRegistration date",
    "DefaultDesignRegistration date",
    "Registration date",
    "EP Registration date",
    # Uppercase variants (defensive)
    "REGISTRATION_DATE",
    "REG_DATE",
)

# Optional fallback: registration-fee paid date (NOT a true registration date).
# Only use for annuity auto-gen when explicitly enabled via config.
REG_FEE_PAID_EVENT_KEYS: tuple[str, ...] = (
    "RegistrationPayment",
    "Registration Payment",
    "REGISTRATION_FEE_PAID",
    "REG_FEE_PAID_DATE",
    "REGISTRATION_FEE_PAID_DATE",
    "registration_fee_paid_date",
    "reg_fee_paid_date",
)

REG_FEE_PAID_CUSTOM_KEYS: tuple[str, ...] = (
    "reg_fee_paid_date",
    "registration_fee_paid_date",
    "registrationFeePaidDate",
    "RegistrationPayment",
    "Registration Payment",
    "REG_FEE_PAID_DATE",
    "REGISTRATION_FEE_PAID_DATE",
)


_KEY_NORM_RE = re.compile(r"[^0-9A-Z-]+")


def normalize_key(key: object | None) -> str:
    """Normalize keys for robust matching (case/whitespace/punct-insensitive)."""
    if key is None:
        return ""
    try:
        s = str(key).strip()
    except Exception:
        return ""
    if not s:
        return ""
    return _KEY_NORM_RE.sub("", s.upper())


def iter_json_items(data: Any, *, max_nodes: int = 5000) -> Iterator[tuple[str, Any]]:
    """
    Iterate (key, value) pairs recursively over JSON-like structures.
    Avoids deep recursion and caps the amount of work for safety.
    """
    if data is None:
        return
    stack = [data]
    seen = 0
    while stack:
        cur = stack.pop()
        seen += 1
        if seen > max_nodes:
            return
        if isinstance(cur, dict):
            for k, v in cur.items():
                yield (str(k), v)
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def data_has_any_key(
    data: Any,
    keys: Iterable[str],
    *,
    key_substring: str | None = None,
) -> bool:
    """Return True if any key exists (supports nested dict/list)."""
    key_norms = {normalize_key(k) for k in keys if k}
    if not key_norms and not key_substring:
        return False
    for k, _v in iter_json_items(data):
        nk = normalize_key(k)
        if not nk:
            continue
        if nk in key_norms:
            return True
        if key_substring and key_substring in nk:
            return True
    return False


def find_first_date(
    data: Any,
    keys: Iterable[str],
    *,
    key_substring: str | None = None,
) -> date | None:
    """Return the first parseable date value found under matching keys."""
    key_norms = {normalize_key(k) for k in keys if k}
    if not key_norms and not key_substring:
        return None
    for k, v in iter_json_items(data):
        nk = normalize_key(k)
        if not nk:
            continue
        if nk in key_norms or (key_substring and key_substring in nk):
            dt = parse_date(v)
            if dt:
                return dt
    return None
