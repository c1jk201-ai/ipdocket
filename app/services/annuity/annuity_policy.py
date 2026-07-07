from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./\s]+(\d{1,2})[-./\s]+(\d{1,2})(?!\d)")
PREPAID_YEARS_AT_REGISTRATION_DOMESTIC = 3


def parse_date(value: Any) -> Optional[date]:
    """Best-effort date parse. Returns None if not parseable."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(yyyy, mm, dd)
    except Exception:
        return None


def normalize_status(raw: Optional[str]) -> str:
    """Storage recommendation: keep only pending/paid/giveup. overdue is derived."""
    text = str(raw or "").strip()
    if not text:
        return "pending"

    v = text.lower()

    # giveup (localized + English synonyms)
    if v in ("giveup", "give_up", "give-up", "abandoned", "waived", "forfeit"):
        return "giveup"
    if "Abandoned" in text or "Withdrawn" in text:
        return "giveup"

    # paid (localized + English synonyms)
    if v in ("paid", "payed", "done", "complete", "completed"):
        return "paid"
    if "Done" in text or " Done" in text:
        return "pending"
    if "Receipt" in text:
        return "paid"
    if "Payment" in text and "Done" in text:
        return "paid"

    if v in ("overdue",):
        return "pending"
    if v in ("pending", "open", "unpaid"):
        return "pending"
    if "overdue" in text or "In Progress" in text:
        return "pending"

    # unknown -> pending (fail-safe)
    return "pending"


def effective_due_date_str(annuity: Any) -> Optional[str]:
    """
    Return the effective due date (YYYY-MM-DD) for sorting/visibility.

    Policy (renewal module aligned):
    - Legal due is `due_date` (preferred), falling back to `extended_due_date` only when `due_date` is missing.
    - `internal_due_date` is used only when it is earlier than the legal due.
    """
    internal = parse_date(getattr(annuity, "internal_due_date", None))
    legal = parse_date(getattr(annuity, "due_date", None)) or parse_date(
        getattr(annuity, "extended_due_date", None)
    )
    if internal and legal:
        return min(internal, legal).strftime("%Y-%m-%d")
    if internal:
        return internal.strftime("%Y-%m-%d")
    if legal:
        return legal.strftime("%Y-%m-%d")
    return None


def is_domestic_case(matter: Any) -> bool:
    """Best-effort domestic check used for US maintenance-fee policies."""
    if matter is None:
        return False
    rg = (getattr(matter, "right_group", None) or "").strip().upper()
    if rg == "DOM":
        return True
    if rg in ("OUT", "INC"):
        return False
    our_ref = (getattr(matter, "our_ref", None) or "").strip().upper()
    if our_ref.endswith("US"):
        return True
    if "Domestic" in ((getattr(matter, "right_name", None) or "").strip()):
        return True
    return False


def is_registration_prepaid_cycle(
    annuity: Any,
    matter: Any | None,
    *,
    prepaid_years: int = PREPAID_YEARS_AT_REGISTRATION_DOMESTIC,
) -> bool:
    """
    Return True when an initial domestic maintenance cycle is treated as covered at registration.
    """
    if not matter or not is_domestic_case(matter):
        return False
    try:
        cycle_no = int(getattr(annuity, "cycle_no", None) or 0)
    except Exception:
        return False
    return 0 < cycle_no <= int(prepaid_years or 0)


def compute_status(annuity: Any, today: Optional[date] = None) -> str:
    """Return one of: pending/overdue/paid/giveup."""
    today = today or date.today()
    st = normalize_status(getattr(annuity, "annuity_status", None))
    if st in ("paid", "giveup"):
        return st
    if parse_date(getattr(annuity, "paid_date", None)):
        return "paid"
    eff = parse_date(effective_due_date_str(annuity))
    if eff and eff < today:
        return "overdue"
    return "pending"
