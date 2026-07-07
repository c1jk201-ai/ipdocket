import uuid
from datetime import date, datetime
from typing import Optional

from flask import current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter, MatterEvent, MatterIdentifier
from app.services.case.case_audit_service import record_case_audit
from app.utils.permissions import require_matter_access

_PRIORITY_EVENT_KEY = "PRIORITY_DATE"
_LEGACY_PRIORITY_EVENT_KEYS = ("", "Text")
_PRIORITY_ID_TYPE = "Priority"
_PRIORITY_ID_TYPES = (_PRIORITY_ID_TYPE, "priority_no", "Text")


def _normalize_identifier(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isalnum()).upper()


def _split_priority_numbers(raw: str) -> list[str]:
    s = str(raw or "").strip()
    if not s:
        return []

    s = (
        s.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("，", ",")
        .replace(";", ",")
        .replace("|", ",")
        .replace("\n", ",")
    )
    out: list[str] = []
    seen: set[str] = set()
    for token in s.split(","):
        v = (token or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _parse_date_ymd(raw: Optional[str]) -> Optional[date]:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@bp.route("/<case_id>/priority/add", methods=["POST"])
@login_required
def add_priority(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    raw_priority_no = (request.form.get("priority_no") or "").strip()
    claim_date_raw = (request.form.get("claim_date") or "").strip()
    country = (request.form.get("country") or "").strip().upper()

    priority_numbers = _split_priority_numbers(raw_priority_no)
    claim_date = _parse_date_ymd(claim_date_raw) if claim_date_raw else None

    if claim_date_raw and claim_date is None:
        flash("Priority YYYY-MM-DD to Input .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-priority"))

    if not priority_numbers and claim_date is None:
        flash("Priority   Priority Input .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-priority"))

    inserted_numbers: list[str] = []
    old_event_value = None
    new_event_value = None
    try:
        existing_values = [
            (row.id_value or "").strip()
            for row in MatterIdentifier.query.filter(MatterIdentifier.matter_id == str(case_id))
            .filter(MatterIdentifier.id_type.in_(_PRIORITY_ID_TYPES))
            .all()
        ]
        existing_norms = {_normalize_identifier(v) for v in (existing_values or []) if v}

        for no in priority_numbers:
            norm = _normalize_identifier(no)
            if not norm or norm in existing_norms:
                continue
            existing_norms.add(norm)
            db.session.add(
                MatterIdentifier(
                    mid_id=uuid.uuid4().hex,
                    matter_id=str(case_id),
                    id_type=_PRIORITY_ID_TYPE,
                    id_value=no,
                    country=country or None,
                    raw_text=no,
                    source_column="manual:priority",
                )
            )
            inserted_numbers.append(no)

        if claim_date is not None:
            existing_event = (
                MatterEvent.query.filter(MatterEvent.matter_id == str(case_id))
                .filter(MatterEvent.event_key.in_((_PRIORITY_EVENT_KEY, *_LEGACY_PRIORITY_EVENT_KEYS)))
                .order_by(MatterEvent.mevent_id.asc())
                .first()
            )

            if existing_event:
                old_event_value = (existing_event.event_at or "").strip()
                old_event_date = _parse_date_ymd(old_event_value)
                keep_date = (
                    claim_date if old_event_date is None else min(old_event_date, claim_date)
                )
                new_event_value = keep_date.isoformat()
                existing_event.event_key = _PRIORITY_EVENT_KEY
                existing_event.event_at = new_event_value
                existing_event.source_column = "manual:priority"
                db.session.add(existing_event)
            else:
                new_event_value = claim_date.isoformat()
                db.session.add(
                    MatterEvent(
                        mevent_id=uuid.uuid4().hex,
                        matter_id=str(case_id),
                        event_key=_PRIORITY_EVENT_KEY,
                        event_at=new_event_value,
                        source_column="manual:priority",
                    )
                )

        if inserted_numbers or claim_date is not None:
            record_case_audit(
                case_id=str(case_id),
                action="USER",
                field_name="priority.add",
                actor_user_id=getattr(current_user, "id", None),
                old_value={"priority_date": old_event_value},
                new_value={
                    "added_priority_numbers": inserted_numbers,
                    "priority_date": new_event_value,
                    "country": country or None,
                },
            )
            db.session.commit()
            if inserted_numbers and claim_date is not None:
                flash("Priority   Save.", "success")
            elif inserted_numbers:
                flash("Priority  Save.", "success")
            else:
                flash(" Save.", "success")
        else:
            db.session.rollback()
            flash("  Priority  Save exists.", "info")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Priority add failed (case_id=%s)", case_id)
        flash("Priority Save In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-priority"))
