from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models.ip_records import AnnuityItem, Matter
from app.services.annuity.annuity_policy import (
    PREPAID_YEARS_AT_REGISTRATION_DOMESTIC,
    is_domestic_case,
)
from app.services.annuity.annuity_service import (
    ANNUITY_RIGHT_TYPES,
    _get_term_years,
    _infer_right_type,
    revive_soft_deleted_annuity_item,
)
from app.services.workflow.task_sync import sync_annuity_workflows_for_matter


def _clean_text(v: Any) -> str | None:
    s = str(v or "").strip()
    return s or None


def _to_int(v: Any) -> int | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        n = int(s)
        return n if n > 0 else None
    except Exception:
        return None


def _to_float(v: Any) -> float | None:
    s = str(v or "").strip()
    if s == "":
        return None
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def _to_bool(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _norm_status(paid_date: str | None, raw_status: str | None, assumed_paid: bool) -> str | None:
    # Storage policy: paid/giveup/pending; overdue is derived.
    if paid_date:
        return "paid"
    if assumed_paid:
        return "paid"
    raw = str(raw_status or "").strip()
    if not raw:
        return None
    v = raw.lower()
    if v in {"paid", "payed", "done", "complete", "completed"}:
        return "paid"
    if v in {"giveup", "give_up", "give-up", "abandoned", "waived", "forfeit"}:
        return "giveup"
    if "Abandoned" in raw or "Withdrawn" in raw:
        return "giveup"
    return "pending"


def _field_equal(old: Any, new: Any) -> bool:
    # Normalize empty/None for comparisons (DB often stores "" vs NULL).
    if isinstance(old, str):
        old_n = old.strip() or None
    else:
        old_n = old
    if isinstance(new, str):
        new_n = new.strip() or None
    else:
        new_n = new
    return old_n == new_n


def _apply(obj: Any, attr: str, value: Any, *, overwrite_blanks: bool) -> bool:
    if value is None and not overwrite_blanks:
        return False
    old = getattr(obj, attr, None)
    if _field_equal(old, value):
        return False
    setattr(obj, attr, value)
    return True


def _active_annuity_filter():
    return or_(AnnuityItem.is_deleted.is_(False), AnnuityItem.is_deleted.is_(None))


def _default_csv_path() -> Path:
    # root_path: /app/app -> repo root is parent (/app)
    return Path(current_app.root_path).parent / "annuity_date_schedule.csv"


def sync_annuities_from_schedule_csv_for_matter(
    matter_id: str,
    *,
    csv_path: str | Path | None = None,
    overwrite_blanks: bool = False,
    sync_workflows: bool = True,
    commit: bool = True,
) -> dict[str, int]:
    """
    Sync one matter's annuity rows from annuity_date_schedule.csv.

    Returns:
      dict with keys: created, updated, unchanged, skipped, matched_rows
    Raises:
      ValueError / FileNotFoundError on invalid input.
    """
    mid = str(matter_id or "").strip()
    if not mid:
        raise ValueError("matter_id  exists.")

    matter = Matter.query.get(mid)
    if not matter:
        raise ValueError("Matter not found.")

    right_type = _infer_right_type(matter)
    if not right_type or right_type not in ANNUITY_RIGHT_TYPES:
        raise ValueError("Renewal target Matter .")

    term_years = int(_get_term_years(right_type) or 0)
    if term_years <= 0:
        raise ValueError(" Type Period(term) Confirm  none.")
    prepaid_years = PREPAID_YEARS_AT_REGISTRATION_DOMESTIC if is_domestic_case(matter) else 0

    path = Path(csv_path) if csv_path else _default_csv_path()
    if not path.exists():
        raise FileNotFoundError(f"CSV File   none: {path}")

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if not isinstance(r, dict):
                continue
            if str(r.get("matter_id") or "").strip() != mid:
                continue
            rows.append(r)

    if not rows:
        raise ValueError("CSV  Matter Renewal Data none.")

    existing_items = AnnuityItem.query.filter_by(matter_id=mid).all()
    by_cycle: dict[int, AnnuityItem] = {}
    for it in existing_items or []:
        try:
            c = int(getattr(it, "cycle_no", None) or 0)
        except Exception:
            c = 0
        if c > 0:
            by_cycle[c] = it

    created = 0
    updated = 0
    unchanged = 0
    skipped = 0

    for r in rows:
        cyc = _to_int(r.get("cycle_no"))
        if not cyc:
            skipped += 1
            continue
        if cyc > term_years:
            skipped += 1
            continue

        it = by_cycle.get(cyc)
        is_new = it is None
        if is_new:
            it = AnnuityItem(matter_id=mid, cycle_no=cyc)
            db.session.add(it)
            by_cycle[cyc] = it
            created += 1

        due_date = _clean_text(r.get("due_date"))
        extended_due_date = _clean_text(r.get("extended_due_date"))
        renewal_open_date = _clean_text(r.get("renewal_open_date"))
        renewal_notice_due = _clean_text(r.get("renewal_notice_due"))
        internal_due_date = _clean_text(r.get("internal_due_date"))
        paid_date = _clean_text(r.get("paid_date"))
        paid_amount = _to_float(r.get("paid_amount"))
        raw_status = _clean_text(r.get("annuity_status"))
        official_fee = _to_float(r.get("official_fee"))
        vat_amount = _to_float(r.get("vat_amount"))
        service_fee = _to_float(r.get("service_fee"))
        discount_rate = _to_float(r.get("discount_rate"))
        owner_staff_party_id = _clean_text(r.get("owner_staff_party_id"))
        memo = _clean_text(r.get("memo"))
        raw_id = _clean_text(r.get("raw_id"))
        assumed_paid = _to_bool(r.get("assumed_paid"))
        assumed_reason = _clean_text(r.get("assumed_paid_reason"))

        status = _norm_status(paid_date, raw_status, assumed_paid)
        if prepaid_years > 0 and cyc <= prepaid_years and status != "giveup":
            status = "paid"
        if status == "paid" and not paid_date and assumed_reason:
            memo = (memo or "").rstrip()
            tag = f"[assumed_paid] {assumed_reason}"
            if tag not in memo:
                memo = (memo + ("\n" if memo else "") + tag).strip()

        changed_fields = 1 if revive_soft_deleted_annuity_item(it) else 0
        for attr, val in [
            ("due_date", due_date),
            ("extended_due_date", extended_due_date),
            ("renewal_open_date", renewal_open_date),
            ("renewal_notice_due", renewal_notice_due),
            ("internal_due_date", internal_due_date),
            ("paid_date", paid_date),
            ("paid_amount", paid_amount),
            ("annuity_status", status if status else "pending"),
            ("official_fee", official_fee),
            ("vat_amount", vat_amount),
            ("service_fee", service_fee),
            ("discount_rate", discount_rate),
            ("owner_staff_party_id", owner_staff_party_id),
            ("memo", memo),
            ("raw_id", raw_id),
        ]:
            if _apply(it, attr, val, overwrite_blanks=overwrite_blanks):
                changed_fields += 1

        if not is_new:
            if changed_fields > 0:
                updated += 1
            else:
                unchanged += 1

    if sync_workflows:
        sync_annuity_workflows_for_matter(mid)

    if commit:
        db.session.commit()

    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "matched_rows": len(rows),
    }
