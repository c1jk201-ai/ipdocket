from __future__ import annotations

import re
import unicodedata
from email.header import decode_header

_MIME_ENCODED_WORD_RE = re.compile(
    r"=\?[^?]+\?[bBqQ]\?[^?]+\?=(?:[ \t\r\n]+=\?[^?]+\?[bBqQ]\?[^?]+\?=)*"
)


def _safe_decode_bytes(data: bytes, preferred: str | None = None) -> str:
    candidates: list[str] = []
    if preferred:
        candidates.append(str(preferred))
    candidates.extend(["utf-8", "latin-1"])

    seen: set[str] = set()
    for enc in candidates:
        enc = (enc or "").strip()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _maybe_recover_legacy_header(text: str) -> str:
    if not text:
        return ""
    return text


def contains_mime_encoded_words(value: str | None) -> bool:
    text = str(value or "")
    if "=?" not in text or "?=" not in text:
        return False
    return _MIME_ENCODED_WORD_RE.search(text) is not None


def decode_mime_encoded_words(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not contains_mime_encoded_words(text):
        return unicodedata.normalize("NFC", text).replace("\x00", "")

    def _decode_fragment(match: re.Match[str]) -> str:
        fragment = match.group(0)
        try:
            parts: list[str] = []
            for content, encoding in decode_header(fragment):
                if isinstance(content, bytes):
                    if encoding:
                        try:
                            decoded = content.decode(encoding)
                        except (LookupError, UnicodeDecodeError):
                            decoded = _safe_decode_bytes(content, encoding)
                    else:
                        decoded = _safe_decode_bytes(content)
                else:
                    decoded = str(content)
                parts.append(_maybe_recover_legacy_header(decoded))
            return "".join(parts)
        except Exception:
            return fragment

    decoded = _MIME_ENCODED_WORD_RE.sub(_decode_fragment, text)
    return unicodedata.normalize("NFC", decoded).replace("\x00", "")


def normalize_uploaded_filename(filename: str | None, *, default: str = "file.bin") -> str:
    normalized = decode_mime_encoded_words(filename).strip()
    return normalized or default
