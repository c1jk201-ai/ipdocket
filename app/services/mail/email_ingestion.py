from __future__ import annotations

from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

from app.utils.html_sanitizer import sanitize_email_html
from app.utils.mime_headers import decode_mime_encoded_words, normalize_uploaded_filename


def _decode_header_bytes(value: bytes | None) -> str:
    if not value:
        return ""
    text = ""
    try:
        ascii_value = value.decode("ascii")
        decoded_parts = []
        for part, charset in decode_header(ascii_value):
            if isinstance(part, bytes):
                for enc in (charset, "utf-8", "latin-1"):
                    if not enc:
                        continue
                    try:
                        decoded_parts.append(part.decode(enc))
                        break
                    except Exception:
                        continue
            else:
                decoded_parts.append(str(part))
        text = "".join(decoded_parts)
    except Exception:
        text = ""
    if text and "\ufffd" not in text:
        return decode_mime_encoded_words(text).strip()

    for enc in ("utf-8", "latin-1"):
        try:
            return value.decode(enc).strip()
        except Exception:
            continue
    return value.decode("utf-8", errors="replace").strip()


def _raw_headers(raw: bytes) -> dict[str, bytes]:
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    unfolded: list[bytes] = []
    for line in head.replace(b"\r\n", b"\n").split(b"\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and unfolded:
            unfolded[-1] += b" " + line.strip()
        else:
            unfolded.append(line.rstrip())

    headers: dict[str, bytes] = {}
    for line in unfolded:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        headers[key.decode("ascii", errors="ignore").strip().lower()] = value.strip()
    return headers


def _safe_part_text(part: Any) -> str:
    try:
        content = part.get_content()
        return str(content or "")
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        for enc in (charset, "utf-8", "latin-1"):
            try:
                return payload.decode(enc)
            except Exception:
                continue
        return payload.decode("utf-8", errors="replace")


def _parse_eml(raw: bytes) -> tuple[dict[str, Any], str, str | None, list[dict[str, Any]]]:
    headers = _raw_headers(raw or b"")
    msg = BytesParser(policy=policy.default).parsebytes(raw or b"")

    subject = _decode_header_bytes(headers.get("subject")) or decode_mime_encoded_words(
        msg.get("subject") or ""
    )
    from_addr = _decode_header_bytes(headers.get("from")) or decode_mime_encoded_words(
        msg.get("from") or ""
    )
    to_addr = _decode_header_bytes(headers.get("to")) or decode_mime_encoded_words(
        msg.get("to") or ""
    )
    cc_addr = _decode_header_bytes(headers.get("cc")) or decode_mime_encoded_words(
        msg.get("cc") or ""
    )
    date_raw = _decode_header_bytes(headers.get("date")) or str(msg.get("date") or "").strip()
    try:
        date_parsed = parsedate_to_datetime(date_raw) if date_raw else None
    except Exception:
        date_parsed = None

    body_text_parts: list[str] = []
    body_html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get("Content-Disposition") or "").lower()
        is_attachment = bool(filename) or "attachment" in disposition

        if is_attachment:
            decoded_name = normalize_uploaded_filename(
                decode_mime_encoded_words(filename or ""),
                default="attachment",
            )
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": decoded_name,
                    "content_type": content_type,
                    "size": len(payload),
                    "data": payload,
                }
            )
            continue

        if content_type == "text/plain":
            text = _safe_part_text(part)
            if text:
                body_text_parts.append(text)
        elif content_type == "text/html":
            html = sanitize_email_html(_safe_part_text(part))
            if html:
                body_html_parts.append(html)

    meta = {
        "subject": subject.strip(),
        "from": from_addr.strip(),
        "to": to_addr.strip(),
        "cc": cc_addr.strip(),
        "date": date_raw,
        "date_parsed": date_parsed,
        "message_id": str(msg.get("message-id") or msg.get("message_id") or "").strip(),
    }
    body = "\n".join(part for part in body_text_parts if part)
    html = "\n".join(part for part in body_html_parts if part) or None
    return meta, body, html, attachments
