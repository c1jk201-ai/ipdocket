from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models.ip_records import (
    Communication,
    DocumentSearchIndex,
    EmailAttachment,
    EmailMessage,
    EmailMessageMatterLink,
    FileAsset,
    Matter,
    MatterFileAsset,
    OfficeAction,
    RawImportField,
)
from app.services.storage.file_asset_service import FileAssetService
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter, policy_accessible_matter_ids_select
from app.utils.search import sqlalchemy_contains_query

TEXT_EXTENSIONS = {
    ".csv",
    ".eml",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".msg.txt",
    ".text",
    ".txt",
    ".xml",
}


def _clean_text(value: Any, *, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()
    if limit and len(text) > limit:
        return text[:limit]
    return text


def _source_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    for candidate in (raw, raw[:19], raw[:10]):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            continue
    return None


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _max_index_bytes() -> int:
    try:
        return int(current_app.config.get("DMS_INDEX_MAX_TEXT_BYTES") or 512 * 1024)
    except Exception:
        return 512 * 1024


def _read_text_path(path_value: str | None) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    service = FileAssetService()
    max_bytes = max(1024, _max_index_bytes())
    try:
        rel_path = service.normalize_rel_path(raw)
        with service.default_backend.open(rel_path) as stream:
            return _decode_bytes(stream.read(max_bytes + 1)[:max_bytes])
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="document_search_service._read_text_path",
            log_key="document_search.read_text_path",
            log_window_seconds=300,
        )
        return ""


def _file_asset_text(asset: FileAsset) -> str:
    file_path = str(getattr(asset, "file_path", "") or "")
    suffixes = "".join(Path(file_path).suffixes).lower()
    ext = Path(file_path).suffix.lower()
    mime = str(getattr(asset, "mime_type", "") or "").lower()
    if not (mime.startswith("text/") or ext in TEXT_EXTENSIONS or suffixes in TEXT_EXTENSIONS):
        return ""
    return _read_text_path(file_path)


def upsert_document_index(
    *,
    matter_id: str,
    source_type: str,
    source_id: str,
    title: str | None,
    body: str | None,
    file_asset_id: str | None = None,
    mime_type: str | None = None,
    source_date: datetime | None = None,
    url: str | None = None,
    indexed_by_id: int | None = None,
    commit: bool = True,
) -> DocumentSearchIndex:
    mid = str(matter_id or "").strip()
    stype = str(source_type or "").strip().lower()
    sid = str(source_id or "").strip()
    if not mid or not stype or not sid:
        raise ValueError("matter_id, source_type and source_id are required")
    row = DocumentSearchIndex.query.filter_by(
        matter_id=mid,
        source_type=stype,
        source_id=sid,
    ).first()
    now = datetime.utcnow()
    if not row:
        row = DocumentSearchIndex(
            matter_id=mid,
            source_type=stype,
            source_id=sid,
            created_at=now,
        )
    try:
        body_limit = int(current_app.config.get("DMS_INDEX_MAX_BODY_CHARS", 1_000_000))
    except Exception:
        body_limit = 1_000_000
    row.title = _clean_text(title, limit=500) or None
    row.body = _clean_text(body, limit=body_limit)
    row.file_asset_id = str(file_asset_id or "").strip() or None
    row.mime_type = str(mime_type or "").strip() or None
    row.source_date = source_date
    row.url = str(url or "").strip() or None
    row.indexed_by_id = indexed_by_id
    row.updated_at = now
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def rebuild_matter_search_index(
    matter_id: str,
    *,
    indexed_by_id: int | None = None,
) -> dict[str, int]:
    mid = str(matter_id or "").strip()
    matter = db.session.get(Matter, mid)
    if not matter:
        raise ValueError("matter not found")

    counts = {
        "matter_file": 0,
        "communication": 0,
        "office_action": 0,
        "mail": 0,
        "mail_attachment": 0,
        "raw_import": 0,
    }

    file_rows = (
        db.session.query(MatterFileAsset, FileAsset)
        .join(FileAsset, MatterFileAsset.file_asset_id == FileAsset.file_asset_id)
        .filter(MatterFileAsset.matter_id == mid)
        .filter(or_(MatterFileAsset.is_deleted.is_(False), MatterFileAsset.is_deleted.is_(None)))
        .filter(or_(FileAsset.is_deleted.is_(False), FileAsset.is_deleted.is_(None)))
        .limit(2000)
        .all()
    )
    for mfa, asset in file_rows:
        body = _clean_text(
            " ".join(
                str(part or "")
                for part in (
                    mfa.description,
                    mfa.doc_type,
                    mfa.role,
                    asset.original_name,
                    asset.mime_type,
                    _file_asset_text(asset),
                )
            )
        )
        upsert_document_index(
            matter_id=mid,
            source_type="matter_file",
            source_id=str(mfa.matter_file_id),
            title=asset.original_name or mfa.description or "File",
            body=body,
            file_asset_id=asset.file_asset_id,
            mime_type=asset.mime_type,
            source_date=_source_date(mfa.created_at or asset.created_at),
            url=f"/case/{mid}#sec-files",
            indexed_by_id=indexed_by_id,
            commit=False,
        )
        counts["matter_file"] += 1

    comm_rows = Communication.query.filter_by(matter_id=mid).limit(2000).all()
    for comm in comm_rows:
        title = comm.note or comm.to_text or comm.comm_type or ""
        body = _clean_text(
            " ".join(
                str(part or "")
                for part in (
                    comm.comm_type,
                    comm.to_text,
                    comm.body,
                    comm.note,
                    comm.mail_no,
                    comm.letter_no,
                    comm.due_date,
                )
            )
        )
        upsert_document_index(
            matter_id=mid,
            source_type="communication",
            source_id=str(comm.comm_id),
            title=title,
            body=body,
            source_date=_source_date(comm.received_date or comm.sent_date),
            url=f"/case/{mid}#sec-history",
            indexed_by_id=indexed_by_id,
            commit=False,
        )
        counts["communication"] += 1

    office_rows = OfficeAction.query.filter_by(matter_id=mid).limit(2000).all()
    for oa in office_rows:
        body = _clean_text(
            " ".join(
                str(part or "")
                for part in (
                    oa.doc_name,
                    oa.examiner,
                    oa.review_comment,
                    oa.due_date,
                    oa.extended_due_date,
                    oa.comment_due_date,
                )
            )
        )
        upsert_document_index(
            matter_id=mid,
            source_type="office_action",
            source_id=str(oa.oa_id),
            title=oa.doc_name or "Notice",
            body=body,
            source_date=_source_date(oa.received_date or oa.notified_date),
            url=f"/case/{mid}#sec-history",
            indexed_by_id=indexed_by_id,
            commit=False,
        )
        counts["office_action"] += 1

    mail_rows = (
        db.session.query(EmailMessageMatterLink, EmailMessage)
        .join(EmailMessage, EmailMessageMatterLink.email_id == EmailMessage.id)
        .filter(EmailMessageMatterLink.matter_id == mid)
        .limit(2000)
        .all()
    )
    for link, email in mail_rows:
        body = _clean_text(
            " ".join(
                str(part or "")
                for part in (
                    email.subject,
                    email.from_addr,
                    email.to_text,
                    email.cc_text,
                    email.body_text,
                )
            )
        )
        upsert_document_index(
            matter_id=mid,
            source_type="mail",
            source_id=str(email.id),
            title=email.subject or email.from_addr or "",
            body=body,
            source_date=email.received_at or link.selected_at,
            url=None,
            indexed_by_id=indexed_by_id,
            commit=False,
        )
        counts["mail"] += 1

        attachments = EmailAttachment.query.filter_by(email_id=email.id).limit(200).all()
        for att in attachments:
            att_text = _clean_text(
                " ".join(
                    str(part or "")
                    for part in (
                        att.filename,
                        att.mime,
                        _read_text_path(att.extracted_text_path),
                        _read_text_path(att.ocr_text_path),
                    )
                )
            )
            if not att_text:
                continue
            upsert_document_index(
                matter_id=mid,
                source_type="mail_attachment",
                source_id=str(att.id),
                title=att.filename or " ",
                body=att_text,
                mime_type=att.mime,
                source_date=email.received_at,
                url=None,
                indexed_by_id=indexed_by_id,
                commit=False,
            )
            counts["mail_attachment"] += 1

    if matter.raw_id:
        raw_rows = RawImportField.query.filter_by(raw_id=matter.raw_id).limit(2000).all()
        for raw in raw_rows:
            body = _clean_text(f"{raw.sheet_name} {raw.source_column}: {raw.value_text}")
            if not body:
                continue
            upsert_document_index(
                matter_id=mid,
                source_type="raw_import",
                source_id=str(raw.raw_field_id),
                title=f"{raw.sheet_name} / {raw.source_column}",
                body=body,
                source_date=_source_date(raw.created_at),
                url=f"/case/{mid}",
                indexed_by_id=indexed_by_id,
                commit=False,
            )
            counts["raw_import"] += 1

    db.session.commit()
    return counts


def _search_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', query or ""):
        term = next((part for part in raw if part), "")
        term = term.strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def _snippet(text: str, terms: list[str], *, radius: int = 90) -> str:
    clean = _clean_text(text)
    if not clean:
        return ""
    lower = clean.lower()
    pos = -1
    for term in terms:
        if not term:
            continue
        pos = lower.find(term.lower())
        if pos >= 0:
            break
    if pos < 0:
        return clean[: radius * 2].strip()
    start = max(0, pos - radius)
    end = min(len(clean), pos + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end].strip()}{suffix}"


def search_document_knowledge(
    *,
    query: str,
    user: Any,
    matter_id: str | None = None,
    source_type: str | None = None,
    limit: int = 20,
    refresh: bool = False,
) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        return {"items": [], "count": 0, "indexed": None}

    try:
        limit = int(limit)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 50))

    mid = str(matter_id or "").strip() or None
    indexed = None
    if mid:
        if not can_access_matter(user, mid, action="view"):
            raise PermissionError("forbidden")
        if refresh:
            indexed = rebuild_matter_search_index(
                mid,
                indexed_by_id=getattr(user, "id", None),
            )

    dq = DocumentSearchIndex.query
    if mid:
        dq = dq.filter(DocumentSearchIndex.matter_id == mid)
    else:
        dq = dq.filter(DocumentSearchIndex.matter_id.in_(policy_accessible_matter_ids_select(user)))
    if source_type:
        dq = dq.filter(DocumentSearchIndex.source_type == str(source_type).strip().lower())
    dq = dq.filter(
        or_(
            sqlalchemy_contains_query(DocumentSearchIndex.title, q),
            sqlalchemy_contains_query(DocumentSearchIndex.body, q),
        )
    )
    rows = (
        dq.order_by(
            DocumentSearchIndex.source_date.desc().nullslast(), DocumentSearchIndex.id.desc()
        )
        .limit(limit)
        .all()
    )
    terms = _search_terms(q)
    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "matter_id": row.matter_id,
                "source_type": row.source_type,
                "source_id": row.source_id,
                "file_asset_id": row.file_asset_id,
                "title": row.title or row.source_type,
                "snippet": _snippet(row.body or row.title or "", terms),
                "url": row.url,
                "source_date": row.source_date.isoformat() if row.source_date else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "evidence": {
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "snippet": _snippet(row.body or row.title or "", terms),
                },
            }
        )
    return {"items": items, "count": len(items), "indexed": indexed}
