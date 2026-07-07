from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import false, func

from app.extensions import db
from app.models.matter import Matter
from app.models.system_config import SystemConfig
from app.services.matter.matter_auto_status import (
    AutoStatus,
    date_only_str,
    derive_auto_status,
)
from app.utils.error_logging import report_swallowed_exception

logger = logging.getLogger(__name__)

_AUDIT_CURSOR_KEY = "MATTER_STATUS_CACHE_AUDIT_CURSOR"
_RECONCILE_PAGE_SIZE = 2000


@dataclass(frozen=True)
class MatterStatusCacheSyncResult:
    changed: bool = False
    fields_changed: tuple[str, ...] = ()
    auto_status: AutoStatus = AutoStatus()


def _normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _empty_or_value(value: str, *, empty_as_none: bool) -> str | None:
    if value or not empty_as_none:
        return value
    return None


def _matter_status_cache_scope_query(*, start_after_matter_id: str | None = None):
    q = Matter.query.with_entities(Matter.matter_id)
    if hasattr(Matter, "is_deleted"):
        q = q.filter(func.coalesce(Matter.is_deleted, false()) == false())
    if start_after_matter_id:
        q = q.filter(Matter.matter_id > str(start_after_matter_id).strip())
    return q.order_by(Matter.matter_id.asc())


def _fetch_matter_status_cache_window_ids(
    *,
    limit: int,
    start_after_matter_id: str | None = None,
) -> list[str]:
    q = _matter_status_cache_scope_query(start_after_matter_id=start_after_matter_id)
    q = q.limit(max(1, int(limit or 1)))
    return [str(mid or "").strip() for (mid,) in q.all() if str(mid or "").strip()]


def _iter_matter_status_cache_ids(
    *,
    start_after_matter_id: str | None = None,
    limit: int | None = None,
    page_size: int = _RECONCILE_PAGE_SIZE,
) -> Iterator[str]:
    remaining = None
    if limit is not None:
        try:
            remaining = max(0, int(limit))
        except Exception:
            remaining = None
        if remaining == 0:
            return

    cursor = str(start_after_matter_id or "").strip() or None
    fetch_size = max(1, int(page_size or _RECONCILE_PAGE_SIZE))

    while remaining is None or remaining > 0:
        batch_limit = fetch_size if remaining is None else min(fetch_size, remaining)
        matter_ids = _fetch_matter_status_cache_window_ids(
            limit=batch_limit,
            start_after_matter_id=cursor,
        )
        if not matter_ids:
            break

        for matter_id in matter_ids:
            yield matter_id

        cursor = matter_ids[-1]
        if remaining is not None:
            remaining -= len(matter_ids)


def _reconcile_matter_status_cache_for_ids(
    *,
    matter_ids: Iterable[str],
    commit: bool,
    commit_interval: int,
    empty_as_none: bool,
) -> dict[str, Any]:
    processed = 0
    updated = 0
    errors = 0
    changed_fields: dict[str, int] = {
        "status_red": 0,
        "status_red_related_date": 0,
        "status_red_related_on": 0,
        "status_blue": 0,
    }
    samples: list[dict[str, str]] = []
    commit_every = max(1, int(commit_interval or 100))
    cursor_checkpoint = ""
    saw_error = False

    for matter_id in matter_ids:
        processed += 1
        savepoint = None
        try:
            savepoint = db.session.begin_nested()
            matter = db.session.get(Matter, matter_id)
            if matter is not None:
                result = apply_auto_status_cache_to_matter(
                    matter=matter,
                    empty_as_none=empty_as_none,
                )
                if result.changed:
                    db.session.add(matter)
                    updated += 1
                    for field in result.fields_changed:
                        if field in changed_fields:
                            changed_fields[field] += 1
                    if len(samples) < 20:
                        samples.append(
                            {
                                "matter_id": matter_id,
                                "our_ref": str(getattr(matter, "our_ref", "") or "").strip(),
                                "status_blue": str(
                                    getattr(matter, "status_blue", "") or ""
                                ).strip(),
                            }
                        )
            savepoint.commit()
            if not saw_error:
                cursor_checkpoint = matter_id
        except Exception as exc:
            errors += 1
            saw_error = True
            report_swallowed_exception(
                exc,
                context=f"matter_status_cache.reconcile_matter_status_cache_batch.matter_id={matter_id}",
                log_key="matter_status_cache.reconcile_matter_status_cache_batch",
                log_window_seconds=300,
            )
            try:
                if savepoint is not None:
                    savepoint.rollback()
            except Exception:
                logger.debug(
                    "Failed to rollback matter status cache reconcile savepoint",
                    exc_info=True,
                )

        if commit and processed % commit_every == 0:
            db.session.commit()

    if commit:
        db.session.commit()

    return {
        "processed": processed,
        "updated": updated,
        "errors": errors,
        "changed_fields": changed_fields,
        "samples": samples,
        "cursor_checkpoint": cursor_checkpoint,
    }


def apply_auto_status_cache_to_matter(
    *,
    matter: Matter,
    memo: str | None = None,
    current_red: str | None = None,
    current_red_date: str | None = None,
    current_blue: str | None = None,
    validate_current_red: bool = True,
    empty_as_none: bool = False,
) -> MatterStatusCacheSyncResult:
    """Recalculate and persist a matter's auto-status cache on the ORM object."""
    if not matter or not getattr(matter, "matter_id", None):
        return MatterStatusCacheSyncResult()

    matter_id = str(matter.matter_id)
    memo_txt = (memo if memo is not None else getattr(matter, "memo", None) or "").strip()
    calc_red = (
        current_red if current_red is not None else getattr(matter, "status_red", None) or ""
    ).strip()
    calc_red_dt = date_only_str(
        current_red_date
        if current_red_date is not None
        else getattr(matter, "status_red_related_date", None)
    )
    calc_blue = (
        current_blue if current_blue is not None else getattr(matter, "status_blue", None) or ""
    ).strip()

    # Retained for older call sites; derive_auto_status() owns deadline validation.
    _ = validate_current_red

    auto = derive_auto_status(
        matter_id=matter_id,
        current_red=calc_red,
        current_red_date=calc_red_dt,
        current_blue=calc_blue,
        memo=memo_txt,
    )

    fields_changed: list[str] = []
    new_red = (auto.status_red or "").strip()
    new_red_dt = date_only_str(auto.status_red_related_date)
    new_blue = (auto.status_blue or "").strip()
    new_red_on = date.fromisoformat(new_red_dt) if new_red_dt else None

    if _normalize_space(getattr(matter, "status_red", None)) != _normalize_space(new_red):
        matter.status_red = _empty_or_value(new_red, empty_as_none=empty_as_none)
        fields_changed.append("status_red")
    if date_only_str(getattr(matter, "status_red_related_date", None)) != new_red_dt:
        matter.status_red_related_date = _empty_or_value(new_red_dt, empty_as_none=empty_as_none)
        fields_changed.append("status_red_related_date")
    if (
        hasattr(matter, "status_red_related_on")
        and getattr(matter, "status_red_related_on", None) != new_red_on
    ):
        matter.status_red_related_on = new_red_on
        fields_changed.append("status_red_related_on")
    if _normalize_space(getattr(matter, "status_blue", None)) != _normalize_space(new_blue):
        matter.status_blue = _empty_or_value(new_blue, empty_as_none=empty_as_none)
        fields_changed.append("status_blue")

    return MatterStatusCacheSyncResult(
        changed=bool(fields_changed),
        fields_changed=tuple(fields_changed),
        auto_status=auto,
    )


def reconcile_matter_status_cache_batch(
    *,
    limit: int | None = None,
    commit: bool = True,
    commit_interval: int = 100,
    empty_as_none: bool = False,
    start_after_matter_id: str | None = None,
    page_size: int = _RECONCILE_PAGE_SIZE,
) -> dict[str, Any]:
    """Rebuild persisted matter status cache for non-deleted matters."""
    return _reconcile_matter_status_cache_for_ids(
        matter_ids=_iter_matter_status_cache_ids(
            start_after_matter_id=start_after_matter_id,
            limit=limit,
            page_size=page_size,
        ),
        commit=commit,
        commit_interval=commit_interval,
        empty_as_none=empty_as_none,
    )


def audit_matter_status_cache_window(
    *,
    limit: int = 5000,
    commit: bool = True,
    commit_interval: int = 100,
    empty_as_none: bool = False,
    cursor_key: str = _AUDIT_CURSOR_KEY,
) -> dict[str, Any]:
    """
    Bounded audit pass for persisted matter status cache.

    Why:
    - At 1M matters, a daily full-table reconcile is too expensive.
    - This function advances a persistent cursor through matter_id space and
      audits only a fixed-size window each run.
    """
    batch_limit = max(1, int(limit or 1))
    cursor = str(SystemConfig.get_config(cursor_key, "") or "").strip()

    matter_ids = _fetch_matter_status_cache_window_ids(
        limit=batch_limit,
        start_after_matter_id=cursor,
    )
    wrapped = False

    if not matter_ids and cursor:
        wrapped = True
        matter_ids = _fetch_matter_status_cache_window_ids(limit=batch_limit)

    result = _reconcile_matter_status_cache_for_ids(
        matter_ids=matter_ids,
        commit=commit,
        commit_interval=commit_interval,
        empty_as_none=empty_as_none,
    )

    next_cursor = (
        str(result.get("cursor_checkpoint") or "").strip()
        if int(result.get("errors", 0) or 0) > 0
        else (matter_ids[-1] if matter_ids else "")
    )
    if commit:
        try:
            SystemConfig.set_config(cursor_key, next_cursor)
            db.session.commit()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter_status_cache.audit_matter_status_cache_window.cursor",
                log_key="matter_status_cache.audit_matter_status_cache_window.cursor",
                log_window_seconds=300,
            )
            try:
                db.session.rollback()
            except Exception:
                logger.debug("Failed to rollback matter status cache audit cursor", exc_info=True)

    result["cursor_key"] = cursor_key
    result["cursor_before"] = cursor
    result["cursor_after"] = next_cursor
    result["wrapped"] = wrapped
    return result
