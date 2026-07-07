"""Email upload parsing helpers extracted from case upload routes."""

from __future__ import annotations

import hashlib
import tempfile
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from flask import current_app


def parse_refs_for_file(
    filename: str,
    *,
    where: str,
    subject: str,
    parse_refs_from_text: Callable[[str], set[str]],
) -> set[str]:
    refs = set()
    if where in ("subject", "filename_both"):
        refs |= parse_refs_from_text(subject)
    if where in ("filename", "filename_both"):
        refs |= parse_refs_from_text(filename or "")
    if not refs and where == "subject":
        refs |= parse_refs_from_text(filename or "")
    return refs


def parse_msg_meta(data: bytes, *, source_name: str = "") -> dict:
    try:
        import extract_msg

        msg = None
        tmp_path: Path | None = None
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".msg")
            temp_file.write(data)
            temp_file.flush()
            temp_file.close()
            tmp_path = Path(temp_file.name)

            msg = extract_msg.Message(str(tmp_path), delayAttachments=True)

            try:
                subj = (msg.subject or "").strip()
            except Exception:
                subj = ""
            try:
                dt = msg.date
            except Exception:
                dt = None
            try:
                from_addr = (getattr(msg, "sender", "") or "").strip()
            except Exception:
                from_addr = ""
            try:
                message_id = (getattr(msg, "message_id", "") or "").strip()
            except Exception:
                message_id = ""

            if not dt:
                date_str = ""
            else:
                date_str = dt.date().isoformat()
            return {
                "subject": subj,
                "date": date_str,
                "from": from_addr,
                "message_id": message_id,
            }
        finally:
            try:
                if msg:
                    msg.close()
            except Exception as exc:
                current_app.logger.debug(
                    "Failed to close MSG parser resource for %s: %s",
                    source_name or "upload",
                    exc,
                )
            try:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()
            except Exception as exc:
                current_app.logger.debug(
                    "Failed to delete temp MSG file for %s: %s",
                    source_name or "upload",
                    exc,
                )
    except Exception:
        return {}


def parse_eml_meta(data: bytes) -> dict:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
    except Exception:
        return {}

    subj = (msg.get("subject") or "").strip()
    date_raw = (msg.get("date") or "").strip()
    message_id = (msg.get("message-id") or msg.get("message_id") or "").strip()
    from_addr = (msg.get("from") or "").strip()
    if not date_raw:
        return {
            "subject": subj,
            "date": "",
            "from": from_addr,
            "message_id": message_id,
        }
    try:
        dt = parsedate_to_datetime(date_raw)
        if dt is None:
            return {
                "subject": subj,
                "date": "",
                "from": from_addr,
                "message_id": message_id,
            }
        return {
            "subject": subj,
            "date": dt.date().isoformat(),
            "from": from_addr,
            "message_id": message_id,
        }
    except Exception:
        return {
            "subject": subj,
            "date": "",
            "from": from_addr,
            "message_id": message_id,
        }


def ensure_message_id(meta: dict) -> None:
    if meta.get("message_id"):
        return
    parts = [
        (meta.get("from") or "").strip(),
        (meta.get("date") or "").strip(),
        (meta.get("subject") or "").strip(),
    ]
    if all(parts):
        raw = "|".join(parts)
        meta["message_id"] = "synthetic:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
