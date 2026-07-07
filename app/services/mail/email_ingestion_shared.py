from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Callable

from app.extensions import db
from app.models.ip_records import EmailMessage
from app.utils.policy_sql import policy_text as text


def _coerce_received_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        return None


def _infer_division_from_our_ref(our_ref: str | None) -> str:
    s = str(our_ref or "").strip().upper()
    if len(s) >= 4 and s[:2].isdigit():
        code = s[2:4]
        if len(code) == 2:
            div = code[1:2]
            if div == "D":
                return "DOM"
            if div == "I":
                return "INC"
            if div == "O":
                return "OUT"
    return ""


def upsert_email_message(
    *,
    provider_message_id: str | None,
    message_id_raw: str | None,
    raw_hash: str | None,
    meta: dict,
    raw_eml_path: str | None,
    status: str,
    body_text: str | None,
    body_html: str | None,
    mailbox_tag: str | None = None,
    find_existing_email_id: Callable[..., str | None] | None = None,
) -> tuple[str, bool]:
    scoped_id = str(provider_message_id or "").strip() or None
    if scoped_id and find_existing_email_id:
        existing = find_existing_email_id(
            provider_message_id=scoped_id,
            message_id_raw=message_id_raw,
            raw_hash=raw_hash,
        )
        if existing:
            return existing, False

    if scoped_id:
        new_id = uuid.uuid4().hex
        ignored_at = (
            datetime.utcnow() if (status or "").strip().upper() == "INBOX_IGNORED" else None
        )
        db.session.execute(
            text(
                """
                INSERT INTO email_message (
                    id, provider_message_id, thread_id, "from", "to", "cc",
                    subject, received_at, body_text, body_html, raw_eml_path,
                    mailbox_tag, processing_status, ignored_at
                )
                VALUES (
                    :id, :mid, NULL, :from_addr, :to_addr, :cc_addr,
                    :subject, :received_at, :body_text, :body_html, :raw_eml_path,
                    :mailbox_tag, :status, :ignored_at
                )
                ON CONFLICT (provider_message_id) DO NOTHING
                """
            ),
            {
                "id": new_id,
                "mid": scoped_id,
                "from_addr": (meta.get("from") or "").strip() or None,
                "to_addr": (meta.get("to") or "").strip() or None,
                "cc_addr": (meta.get("cc") or "").strip() or None,
                "subject": (meta.get("subject") or "").strip() or None,
                "received_at": _coerce_received_at(meta.get("date")),
                "body_text": body_text,
                "body_html": body_html,
                "raw_eml_path": raw_eml_path,
                "mailbox_tag": mailbox_tag,
                "status": status,
                "ignored_at": ignored_at,
            },
        )
        existing = db.session.execute(
            text("SELECT id FROM email_message WHERE provider_message_id = :mid LIMIT 1"),
            {"mid": scoped_id},
        ).scalar()
        if not existing:
            return new_id, True
        existing_id = str(existing)
        return existing_id, existing_id == new_id

    msg = EmailMessage(
        provider_message_id=None,
        thread_id=None,
        from_addr=(meta.get("from") or "").strip() or None,
        to_text=(meta.get("to") or "").strip() or None,
        cc_text=(meta.get("cc") or "").strip() or None,
        subject=(meta.get("subject") or "").strip() or None,
        received_at=_coerce_received_at(meta.get("date")),
        body_text=body_text,
        body_html=body_html,
        raw_eml_path=raw_eml_path,
        processing_status=status,
        mailbox_tag=mailbox_tag,
        ignored_at=datetime.utcnow() if (status or "").strip().upper() == "INBOX_IGNORED" else None,
    )
    db.session.add(msg)
    db.session.flush()
    return msg.id, True
