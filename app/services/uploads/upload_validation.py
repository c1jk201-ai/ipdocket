from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Tuple

ALLOWED_APPLICATION_EXTS = {".pdf"}
ALLOWED_RESPONSE_EXTS = {".pdf"}
ALLOWED_EMAIL_EXTS = {".msg", ".eml", ".pdf"}


def _ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _read_head_bytes(file_obj, size: int = 16) -> bytes:
    stream = getattr(file_obj, "stream", None) or getattr(file_obj, "file", None) or file_obj
    if stream is None:
        return b""
    try:
        pos = stream.tell()
    except Exception:
        return b""
    try:
        data = stream.read(size) or b""
    except Exception:
        return b""
    try:
        stream.seek(pos)
    except Exception:
        return b""
    if isinstance(data, str):
        return data.encode("utf-8", errors="ignore")
    return data


def _read_path_head(path: str | Path, size: int = 1024) -> bytes:
    try:
        with Path(path).open("rb") as f:
            return f.read(size) or b""
    except Exception:
        return b""


def _looks_like_pdf(head: bytes) -> bool:
    if not head:
        return False
    idx = head.find(b"%PDF-")
    if idx == -1:
        return False
    if idx == 0:
        return True
    prefix = head[:idx]
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    if prefix.startswith(b"\xfe\xff") or prefix.startswith(b"\xff\xfe"):
        prefix = prefix[2:]
    return all(ch in b"\x00\t\n\r\f\v " for ch in prefix)


def _looks_like_zip(head: bytes) -> bool:
    return head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))


def _looks_like_msg(head: bytes) -> bool:
    return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


_CONTENT_SNIFFERS: dict[str, Callable[[bytes], bool]] = {
    ".pdf": _looks_like_pdf,
    ".zip": _looks_like_zip,
    ".msg": _looks_like_msg,
}


def sniff_extension_mismatch(path: str | Path, *, filename: str | None = None) -> str | None:
    """Return a short mismatch reason when strict signatures disagree with extension."""
    ext = _ext_of(filename or str(path))
    sniffer = _CONTENT_SNIFFERS.get(ext)
    if not sniffer:
        return None
    head_size = 1024 if ext == ".pdf" else 16
    head = _read_path_head(path, size=head_size)
    if head and not sniffer(head):
        return f"{ext} signature mismatch (head={head[:16].hex()})"
    return None


def filter_upload_files(
    files: Iterable,
    allowed_exts: set[str],
) -> Tuple[list, list[str]]:
    logger = logging.getLogger(__name__)
    valid_files = []
    rejected = []
    for f in files:
        name = (getattr(f, "filename", "") or "").strip()
        if not name:
            continue
        ext = _ext_of(name)
        if ext not in allowed_exts:
            rejected.append(name)
            continue
        sniffer = _CONTENT_SNIFFERS.get(ext)
        if sniffer:
            head_size = 1024 if ext == ".pdf" else 16
            head = _read_head_bytes(f, size=head_size)
            if head and not sniffer(head):
                logger.warning(
                    "Upload rejected by content sniff: name=%s ext=%s head=%s",
                    name,
                    ext,
                    head[:16].hex(),
                )
                rejected.append(name)
                continue
        valid_files.append(f)
    return valid_files, rejected
