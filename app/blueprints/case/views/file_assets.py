"""File asset access routes.

Handles file download, EML viewing, and attachment listing.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import (
    Communication,
    CommunicationFileAsset,
    FileAsset,
    OfficeAction,
    OfficeActionFileAsset,
)
from app.services.files.file_classification import is_previewable
from app.services.history.history_merge_service import (
    is_email_asset_like,
    load_history_merge_groups_for_matter,
    split_history_row_key,
)
from app.services.storage.file_asset_access import AuthorizedFileAsset, FileAssetAccessService
from app.services.storage.file_asset_scan_service import (
    SCAN_STATUS_ERROR,
    SCAN_STATUS_INFECTED,
    FileAssetScanBlocked,
    normalize_file_asset_scan_status,
)
from app.services.storage.file_asset_service import get_file_asset_service
from app.utils.error_logging import report_swallowed_exception
from app.utils.html_sanitizer import sanitize_email_html
from app.utils.mime_headers import decode_mime_encoded_words, normalize_uploaded_filename
from app.utils.policy_sql import policy_text as text

_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_END_OF_CHAIN = 0xFFFFFFFE
_OLE_FREE_SECTOR = 0xFFFFFFFF


def _sanitize_download_name(name: str | None, *, fallback: str) -> str:
    """Normalize download filenames so header generation cannot fail."""
    try:
        cleaned = os.path.basename(name or "")
        cleaned = unicodedata.normalize("NFC", cleaned).replace("\x00", "")
        invalid = '<>:"/\\|?*\r\n\t'
        cleaned = cleaned.translate({ord(ch): "_" for ch in invalid}).strip(" .")
        return cleaned or fallback
    except Exception:
        return fallback


def _authorize_file_or_abort(case_id: str, file_asset_id: str) -> AuthorizedFileAsset:
    try:
        return FileAssetAccessService.authorize_read(current_user, str(case_id), str(file_asset_id))
    except FileNotFoundError:
        abort(404, "File   none.")
    except FileAssetScanBlocked as exc:
        status = normalize_file_asset_scan_status(exc.status)
        if status == SCAN_STATUS_INFECTED:
            abort(409, "This file failed the virus scan.")
        if status == SCAN_STATUS_ERROR:
            abort(409, "File scan failed and the file cannot be opened.")
        abort(409, "File scan is not complete.")
    except PermissionError:
        abort(403, "You do not have permission to access this file.")


def _require_attachment_list_access(case_id: str) -> None:
    if not FileAssetAccessService.can_read_matter(current_user, str(case_id)):
        abort(403, "You do not have permission to access this attachment list.")


def _extract_msg_body_safe(msg, *, file_asset_id: str) -> tuple[str | None, str | None]:
    """
    Extract MSG html/text body defensively.

    Some MSG files fail to decode htmlBody with a guessed legacy charset.
    In that case we still return plain-text body if available.
    """
    body_html: str | None = None
    body_text: str | None = None

    try:
        html = msg.htmlBody
        if isinstance(html, bytes):
            body_html = html.decode("utf-8", errors="replace")
        elif html is not None:
            body_html = str(html)
    except Exception as exc:
        current_app.logger.warning("MSG html body parse failed for %s: %s", file_asset_id, exc)

    try:
        text = msg.body
        if text is not None:
            body_text = str(text)
    except Exception as exc:
        current_app.logger.warning("MSG text body parse failed for %s: %s", file_asset_id, exc)

    return body_html, body_text


def _extract_msg_attachment_filename_safe(att, *, index: int, file_asset_id: str) -> str:
    """Return a safe attachment filename from extract-msg attachment object."""
    try:
        name = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None)
        if name:
            return normalize_uploaded_filename(str(name), default=f"attachment_{index}")
    except Exception as exc:
        current_app.logger.warning(
            "MSG attachment filename parse failed for %s (idx=%s): %s",
            file_asset_id,
            index,
            exc,
        )
    return f"attachment_{index}"


def _decode_email_bytes_safe(
    raw: bytes | None,
    *,
    preferred_charset: str | None = None,
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    data = bytes(raw)
    if not data:
        return ""

    candidates = []
    if preferred_charset:
        candidates.append(str(preferred_charset).strip())
    candidates.extend(["utf-8", "latin-1"])

    seen: set[str] = set()
    for enc in candidates:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_email_part_text_safe(part) -> str | None:
    try:
        content = part.get_content()
        if isinstance(content, str):
            if (part.get_content_type() or "").lower() == "text/html" and "<" not in content:
                try:
                    payload = part.get_payload(decode=True)
                except Exception:
                    payload = None
                if payload and (b"<" in payload or b">" in payload):
                    fallback = _decode_email_bytes_safe(payload)
                    if fallback:
                        return fallback
            return content
        if isinstance(content, (bytes, bytearray)):
            return _decode_email_bytes_safe(
                bytes(content),
                preferred_charset=part.get_content_charset(),
            )
    except Exception as exc:
        current_app.logger.debug("Failed get_content() for email part: %s", exc)

    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    return _decode_email_bytes_safe(payload, preferred_charset=part.get_content_charset())


def _extract_eml_body_safe(msg) -> tuple[str | None, str | None]:
    body_html = None
    body_text = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = (part.get_content_type() or "").lower()
            if ct == "text/html" and body_html is None:
                body_html = _extract_email_part_text_safe(part)
            elif ct == "text/plain" and body_text is None:
                body_text = _extract_email_part_text_safe(part)
    else:
        ct = (msg.get_content_type() or "").lower()
        if ct == "text/html":
            body_html = _extract_email_part_text_safe(msg)
        else:
            body_text = _extract_email_part_text_safe(msg)

    return body_html, body_text


def _ole_header_suggests_truncation(data: bytes) -> bool:
    """
    Best-effort: detect obviously truncated OLE/CFBF streams.

    This is mainly to provide a clearer error message when legacy-migrated MSG/PDF/etc files
    were cut (often 4096 bytes) during staging load.
    """
    if len(data) < 512:
        return True
    try:
        sector_shift = int.from_bytes(data[30:32], "little", signed=False)
        sector_size = 1 << sector_shift
        if sector_size <= 0:
            return True
        if len(data) < 512 + sector_size:
            return True

        available_sectors = (len(data) - 512) // sector_size
        num_fat_sectors = int.from_bytes(data[44:48], "little", signed=False)
        if num_fat_sectors > available_sectors:
            return True

        first_dir_sector = int.from_bytes(data[48:52], "little", signed=False)
        if (
            first_dir_sector not in (_OLE_END_OF_CHAIN, _OLE_FREE_SECTOR)
            and first_dir_sector >= available_sectors
        ):
            return True

        first_mini_fat = int.from_bytes(data[60:64], "little", signed=False)
        num_mini_fat = int.from_bytes(data[64:68], "little", signed=False)
        if (
            num_mini_fat
            and first_mini_fat not in (_OLE_END_OF_CHAIN, _OLE_FREE_SECTOR)
            and first_mini_fat >= available_sectors
        ):
            return True
    except Exception:
        return False

    return False


def _looks_like_pgloader_vector_blob(abs_path: Path) -> bool:
    """
    Detect legacy MSSQL→Postgres(pgloader) BLOB text representation.

    Some staging loads stored binary values as ASCII like `#(208 207 17 ...)` (Common Lisp vector print form),
    which then got written to disk as-is. We can detect this cheaply by checking the first bytes.
    """
    try:
        with abs_path.open("rb") as f:
            head = f.read(128)
    except Exception:
        return False
    return head.lstrip().startswith(b"#(")


def _maybe_decode_pgloader_vector_blob(raw: bytes) -> tuple[bytes, bool]:
    """
    Decode pgloader/legacy `#( … )` byte-vector text into real bytes.

    Returns (data, decoded_flag). If input doesn't match the pattern, returns (raw, False).
    """
    if not raw:
        return raw, False

    s = raw.lstrip()
    if not s.startswith(b"#("):
        return raw, False
    if not s.rstrip().endswith(b")"):
        return raw, False

    out = bytearray()
    num: int | None = None
    for b in s[2:]:
        if 48 <= b <= 57:  # 0-9
            num = (num or 0) * 10 + (b - 48)
            if num > 255:
                return raw, False
            continue

        if num is not None:
            out.append(num)
            num = None

        if b == 41:  # ')'
            break

    if num is not None:
        out.append(num)

    if not out:
        return raw, False

    return bytes(out), True


def _assert_file_asset_linked_to_matter(*, matter_id: str, file_asset_id: str) -> bool:
    """
    Check if file asset is linked to matter via any relationship.

    Checks:
    - matter_file_asset
    - communication_file_asset (via communication.matter_id)
    - office_action_file_asset (via office_action.matter_id)
    - matter_memo_file_asset (via matter_memo.matter_id)
    """
    return FileAssetAccessService.is_linked_to_matter(
        matter_id=str(matter_id), file_asset_id=str(file_asset_id)
    )


@bp.route("/<case_id>/file/<file_asset_id>/download")
@login_required
def download_file_asset(case_id: str, file_asset_id: str):
    """Download a file asset linked to a matter."""
    asset = _authorize_file_or_abort(case_id, file_asset_id)
    file_path = asset.file_path
    original_name = asset.original_name
    mime_type = asset.mime_type
    storage_type = asset.storage_type

    file_service = get_file_asset_service()
    download_name = _sanitize_download_name(
        original_name,
        fallback=f"file_{file_asset_id}",
    )

    # Handle S3 / Non-Local
    if (storage_type or "local").lower() == "s3":
        try:
            stream = file_service.open_stream(file_asset_id)

            inline_requested = (request.args.get("inline") or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
            )
            as_attachment = True
            if inline_requested and is_previewable(original_name, mime_type):
                as_attachment = False

            return send_file(
                stream,
                as_attachment=as_attachment,
                download_name=download_name,
                mimetype=mime_type or "application/octet-stream",
            )
        except Exception as e:
            current_app.logger.error(f"S3 download failed for {file_asset_id}: {e}")
            abort(404, "File   none.")

    # Legacy / Local File Logic
    try:
        abs_path = file_service.abs_path(file_path)
    except ValueError:
        current_app.logger.error(f"Path traversal attempt: {file_path}")
        abort(400, " File .")

    if not abs_path.exists():
        current_app.logger.error(f"File not found on disk: {abs_path}")
        abort(404, "File   none.")

    inline_requested = (request.args.get("inline") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    as_attachment = True
    if inline_requested and is_previewable(original_name, mime_type):
        as_attachment = False

    if _looks_like_pgloader_vector_blob(abs_path):
        raw = abs_path.read_bytes()
        decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
        if ok:
            # Check for text metadata masquerading as ZIP.
            if decoded.lstrip().startswith(b"<Version="):
                flash("Only legacy metadata exists for this file; the source file is missing from migration.", "danger")
                return redirect(url_for("case_work.case_detail", case_id=case_id))

            # Check for obviously truncated ZIP files
            # (Valid ZIP must have EOCD record at the end)
            if (original_name or "").lower().endswith(".zip") and len(decoded) > 0:
                is_valid_zip = False
                try:
                    if zipfile.is_zipfile(BytesIO(decoded)):
                        is_valid_zip = True
                except Exception as exc:
                    report_swallowed_exception(
                        exc,
                        context="case.file_assets.zipfile_is_zipfile",
                        log_key="case.file_assets.zipfile_is_zipfile",
                        log_window_seconds=300,
                    )

                if not is_valid_zip:
                    # Provide specific warning if it looks like the 4KB truncation issue
                    if len(decoded) <= 4096:
                        flash(
                            "ZIP File  from  . (4KB Truncation)",
                            "danger",
                        )
                    else:
                        flash(
                            " ZIP File. Preview Download .", "danger"
                        )
                    return redirect(url_for("case_work.case_detail", case_id=case_id))

            return send_file(
                BytesIO(decoded),
                as_attachment=as_attachment,
                download_name=download_name,
                mimetype=mime_type or "application/octet-stream",
            )

    return send_file(
        abs_path,
        as_attachment=as_attachment,
        download_name=download_name,
        mimetype=mime_type or "application/octet-stream",
    )


def _parse_and_render_eml(
    case_id, file_asset_id, abs_path, original_name, *, data: bytes | None = None
):
    """Helper to parse and render EML content."""
    try:
        if data is None:
            with open(abs_path, "rb") as f:
                msg = BytesParser(policy=policy.default).parse(f)
        else:
            msg = BytesParser(policy=policy.default).parsebytes(data)

        # Extract email metadata
        subject = decode_mime_encoded_words(msg.get("Subject", "(Title None)"))
        from_addr = decode_mime_encoded_words(msg.get("From", ""))
        to_addr = decode_mime_encoded_words(msg.get("To", ""))
        cc_addr = decode_mime_encoded_words(msg.get("Cc", ""))
        date_str = msg.get("Date", "")

        date_parsed = None
        if date_str:
            try:
                date_parsed = parsedate_to_datetime(date_str)
            except (TypeError, ValueError):
                # Malformed Date header is common; ignore for preview rendering.
                date_parsed = None

        # Extract body defensively (malformed charset headers are common in legacy EMLs).
        body_html, body_text = _extract_eml_body_safe(msg)

        # Extract attachments info
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                cd = part.get("Content-Disposition", "")
                if "attachment" in cd:
                    filename = normalize_uploaded_filename(
                        part.get_filename(), default="attachment"
                    )
                    attachments.append(
                        {
                            "filename": filename,
                            "content_type": part.get_content_type(),
                        }
                    )

        safe_body_html = sanitize_email_html(body_html) if body_html else None
        if safe_body_html is not None and not safe_body_html.strip():
            safe_body_html = None

        return render_template(
            "case/view_eml.html",
            case_id=case_id,
            file_asset_id=file_asset_id,
            original_name=original_name,
            subject=subject,
            from_addr=from_addr,
            to_addr=to_addr,
            cc_addr=cc_addr,
            date_str=date_str,
            date_parsed=date_parsed,
            body_html=safe_body_html,
            body_text=body_text,
            attachments=attachments,
        )

    except Exception as e:
        current_app.logger.error(f"Failed to parse EML {file_asset_id}: {e}")
        flash("Email  Failed.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/file/<file_asset_id>/view_eml")
@login_required
def view_eml_file_asset(case_id: str, file_asset_id: str):
    """View an EML file as HTML."""
    asset = _authorize_file_or_abort(case_id, file_asset_id)
    file_path = asset.file_path
    original_name = asset.original_name
    storage_type = asset.storage_type

    # Only allow EML files
    if not (original_name or "").lower().endswith(".eml"):
        abort(400, "EML File Preview .")

    file_service = get_file_asset_service()

    # S3 / New Storage
    if (storage_type or "local").lower() == "s3":
        try:
            content = file_service.read_all(file_asset_id)
            return _parse_and_render_eml(case_id, file_asset_id, None, original_name, data=content)
        except Exception as e:
            current_app.logger.error(f"S3 EML read fail: {e}")
            abort(404, "File   none.")

    # Legacy / Local
    try:
        abs_path = file_service.abs_path(file_path)
    except ValueError:
        abort(400, " File .")

    if not abs_path.exists():
        abort(404, "File   none.")

    if _looks_like_pgloader_vector_blob(abs_path):
        raw = abs_path.read_bytes()
        decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
        if ok:
            return _parse_and_render_eml(
                case_id, file_asset_id, abs_path, original_name, data=decoded
            )

    return _parse_and_render_eml(case_id, file_asset_id, abs_path, original_name)


@bp.route("/<case_id>/history/letter/<comm_id>/attachments")
@login_required
def history_letter_attachments(case_id: str, comm_id: str):
    """List attachments for a communication."""
    _require_attachment_list_access(case_id)

    # Verify communication belongs to matter
    check = db.session.execute(
        text(
            "SELECT 1 FROM communication WHERE comm_id = :cid AND matter_id = :mid"
        ).execution_options(policy_bypass=True),
        {"cid": comm_id, "mid": case_id},
    ).scalar()

    if not check:
        abort(404, "   none.")

    # Cross-link: if this communication was created from email ingestion, show the source email.
    email_id = None
    try:
        email_id = (
            db.session.execute(
                text(
                    """
                    SELECT email_id
                    FROM email_message_matter_link
                    WHERE comm_id = :cid
                    LIMIT 1
                    """
                ).execution_options(policy_bypass=True),
                {"cid": comm_id},
            ).scalar()
            or None
        )
        email_id = str(email_id) if email_id else None
    except Exception:
        # Best-effort; migrations may be missing in some environments.
        email_id = None

    # Get attachments
    rows = db.session.execute(
        text(
            """
            SELECT
              fa.file_asset_id,
              fa.original_name,
              fa.byte_size,
              fa.mime_type,
              COALESCE(NULLIF(TRIM(cfa.role), ''), '') AS role,
              COALESCE(NULLIF(TRIM(cfa.description), ''), '') AS description
            FROM communication_file_asset cfa
            JOIN file_asset fa ON cfa.file_asset_id = fa.file_asset_id
            WHERE cfa.comm_id = :cid
              AND COALESCE(cfa.is_deleted, false) = false
              AND COALESCE(fa.is_deleted, false) = false
            ORDER BY COALESCE(NULLIF(TRIM(cfa.created_at), ''), '') ASC, cfa.comm_file_id ASC
        """
        ).execution_options(policy_bypass=True),
        {"cid": comm_id},
    ).fetchall()

    attachments = [
        {
            "file_asset_id": row[0],
            "original_name": row[1],
            "byte_size": row[2],
            "mime_type": row[3],
            "role": row[4],
            "description": row[5],
        }
        for row in rows
    ]

    return render_template(
        "case/history_attachments.html",
        case_id=case_id,
        kind="",
        item_type="letter",
        item_id=comm_id,
        attachments=attachments,
        email_id=email_id,
    )


@bp.route("/<case_id>/history/notice/<oa_id>/attachments")
@login_required
def history_notice_attachments(case_id: str, oa_id: str):
    """List attachments for an office action."""
    _require_attachment_list_access(case_id)

    # Verify office action belongs to matter
    check = db.session.execute(
        text(
            "SELECT 1 FROM office_action WHERE oa_id = :oid AND matter_id = :mid"
        ).execution_options(policy_bypass=True),
        {"oid": oa_id, "mid": case_id},
    ).scalar()

    if not check:
        abort(404, "Office correspondence   none.")

    # Get attachments
    rows = db.session.execute(
        text(
            """
            SELECT
              fa.file_asset_id,
              fa.original_name,
              fa.byte_size,
              fa.mime_type,
              COALESCE(NULLIF(TRIM(oafa.role), ''), '') AS role,
              COALESCE(NULLIF(TRIM(oafa.description), ''), '') AS description
            FROM office_action_file_asset oafa
            JOIN file_asset fa ON oafa.file_asset_id = fa.file_asset_id
            WHERE oafa.oa_id = :oid
              AND COALESCE(oafa.is_deleted, false) = false
              AND COALESCE(fa.is_deleted, false) = false
            ORDER BY COALESCE(NULLIF(TRIM(oafa.created_at), ''), '') ASC, oafa.oa_file_id ASC
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id},
    ).fetchall()

    attachments = [
        {
            "file_asset_id": row[0],
            "original_name": row[1],
            "byte_size": row[2],
            "mime_type": row[3],
            "role": row[4],
            "description": row[5],
        }
        for row in rows
    ]

    return render_template(
        "case/history_attachments.html",
        case_id=case_id,
        kind="Office correspondence",
        item_type="notice",
        item_id=oa_id,
        attachments=attachments,
    )


@bp.route("/<case_id>/history/merge/<group_id>/attachments")
@login_required
def history_merge_attachments(case_id: str, group_id: str):
    """List deduplicated attachments for a saved history merge group."""
    _require_attachment_list_access(case_id)

    groups = load_history_merge_groups_for_matter(str(case_id))
    group = next(
        (g for g in groups if str(g.get("group_id") or "").strip() == str(group_id or "").strip()),
        None,
    )
    if not group:
        abort(404, "    none.")

    member_keys = [str(k or "").strip() for k in (group.get("member_keys") or [])]
    member_keys = [k for k in member_keys if k]
    if not member_keys:
        return render_template(
            "case/history_attachments.html",
            case_id=case_id,
            kind="",
            item_type="merge",
            item_id=group_id,
            attachments=[],
        )

    comm_ids: list[str] = []
    oa_ids: list[str] = []
    for key in member_keys:
        parsed = split_history_row_key(key)
        if not parsed:
            continue
        kind, row_id = parsed
        if kind == "letter":
            comm_ids.append(row_id)
        elif kind == "notice":
            oa_ids.append(row_id)

    comm_doc_by_id: dict[str, str] = {}
    oa_doc_by_id: dict[str, str] = {}

    if comm_ids:
        for comm_id, note, body, comm_type in (
            db.session.query(
                Communication.comm_id,
                Communication.note,
                Communication.body,
                Communication.comm_type,
            )
            .filter(Communication.comm_id.in_(comm_ids))
            .all()
        ):
            doc_name = (note or "").strip() or (str(body or "").strip()[:120]) or (comm_type or "")
            comm_doc_by_id[str(comm_id)] = doc_name
    if oa_ids:
        for oa_id, doc_name in (
            db.session.query(OfficeAction.oa_id, OfficeAction.doc_name)
            .filter(OfficeAction.oa_id.in_(oa_ids))
            .all()
        ):
            oa_doc_by_id[str(oa_id)] = str(doc_name or "").strip()

    dedup: dict[str, dict] = {}

    if comm_ids:
        comm_rows = (
            db.session.query(
                CommunicationFileAsset.comm_id,
                FileAsset.file_asset_id,
                FileAsset.original_name,
                FileAsset.byte_size,
                FileAsset.mime_type,
                FileAsset.file_path,
                CommunicationFileAsset.role,
                CommunicationFileAsset.description,
            )
            .join(FileAsset, FileAsset.file_asset_id == CommunicationFileAsset.file_asset_id)
            .filter(CommunicationFileAsset.comm_id.in_(comm_ids))
            .filter(CommunicationFileAsset.is_deleted.is_(False))
            .filter(FileAsset.is_deleted.is_(False))
            .all()
        )
        for (
            comm_id,
            file_asset_id,
            original_name,
            byte_size,
            mime_type,
            file_path,
            role,
            description,
        ) in comm_rows:
            fid = str(file_asset_id or "").strip()
            if not fid:
                continue
            item = dedup.get(fid)
            if not item:
                item = {
                    "file_asset_id": fid,
                    "original_name": original_name,
                    "byte_size": byte_size,
                    "mime_type": mime_type,
                    "role": role
                    or (
                        "Original"
                        if is_email_asset_like(original_name, mime_type, file_path)
                        else ""
                    ),
                    "description": description or "",
                    "_sources": [],
                }
                dedup[fid] = item
            source_doc = comm_doc_by_id.get(str(comm_id), "")
            source_label = f": {source_doc or comm_id}"
            if source_label not in item["_sources"]:
                item["_sources"].append(source_label)

    if oa_ids:
        notice_rows = (
            db.session.query(
                OfficeActionFileAsset.oa_id,
                FileAsset.file_asset_id,
                FileAsset.original_name,
                FileAsset.byte_size,
                FileAsset.mime_type,
                FileAsset.file_path,
                OfficeActionFileAsset.role,
                OfficeActionFileAsset.description,
            )
            .join(FileAsset, FileAsset.file_asset_id == OfficeActionFileAsset.file_asset_id)
            .filter(OfficeActionFileAsset.oa_id.in_(oa_ids))
            .filter(OfficeActionFileAsset.is_deleted.is_(False))
            .filter(FileAsset.is_deleted.is_(False))
            .all()
        )
        for (
            oa_id,
            file_asset_id,
            original_name,
            byte_size,
            mime_type,
            file_path,
            role,
            description,
        ) in notice_rows:
            fid = str(file_asset_id or "").strip()
            if not fid:
                continue
            item = dedup.get(fid)
            if not item:
                item = {
                    "file_asset_id": fid,
                    "original_name": original_name,
                    "byte_size": byte_size,
                    "mime_type": mime_type,
                    "role": role
                    or (
                        "Original"
                        if is_email_asset_like(original_name, mime_type, file_path)
                        else ""
                    ),
                    "description": description or "",
                    "_sources": [],
                }
                dedup[fid] = item
            source_doc = oa_doc_by_id.get(str(oa_id), "")
            source_label = f"Notice: {source_doc or oa_id}"
            if source_label not in item["_sources"]:
                item["_sources"].append(source_label)

    attachments: list[dict] = []
    for item in dedup.values():
        sources = item.pop("_sources", [])
        source_text = ", ".join(str(s) for s in sources[:6] if s)
        if source_text:
            desc = str(item.get("description") or "").strip()
            item["description"] = (
                f"{desc} / : {source_text}" if desc else f": {source_text}"
            )
        attachments.append(item)

    attachments.sort(
        key=lambda a: ((a.get("original_name") or "").lower(), str(a.get("file_asset_id") or ""))
    )

    group_title = str(group.get("title") or "").strip()
    kind_label = f" ({group_title})" if group_title else ""
    return render_template(
        "case/history_attachments.html",
        case_id=case_id,
        kind=kind_label,
        item_type="merge",
        item_id=group_id,
        attachments=attachments,
    )


@bp.route("/<case_id>/file/<file_asset_id>/view_msg")
@login_required
def view_msg_file_asset(case_id: str, file_asset_id: str):
    """View a MSG (Outlook) file as HTML."""
    try:
        import extract_msg
    except ImportError:
        abort(500, "extract-msg library is not installed.")

    asset = _authorize_file_or_abort(case_id, file_asset_id)
    file_path = asset.file_path
    original_name = asset.original_name
    storage_type = asset.storage_type

    # Only allow .msg files
    if not (original_name or "").lower().endswith(".msg"):
        abort(400, "MSG File Preview .")

    file_service = get_file_asset_service()
    decoded_bytes: bytes | None = None
    abs_path: Path | None = None

    # S3 / New Storage
    if (storage_type or "local").lower() == "s3":
        try:
            decoded_bytes = file_service.read_all(file_asset_id)
        except Exception as e:
            report_swallowed_exception(
                e,
                context="case.file_assets.view_msg.s3_read",
                log_key="case.file_assets.view_msg.s3_read",
                log_window_seconds=300,
            )
            current_app.logger.error(f"S3 MSG read fail: {e}")
            abort(404, "File   none.")
    else:
        # Legacy / Local
        try:
            abs_path = file_service.abs_path(file_path)
        except ValueError:
            abort(400, " File .")

        if not abs_path.exists():
            abort(404, "File   none.")

        if _looks_like_pgloader_vector_blob(abs_path):
            raw = abs_path.read_bytes()
            decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
            if ok:
                decoded_bytes = decoded

    msg = None
    tmp_path: Path | None = None
    try:
        try:
            if decoded_bytes is not None:
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".msg")
                temp_file.write(decoded_bytes)
                temp_file.flush()
                temp_file.close()
                tmp_path = Path(temp_file.name)
                msg = extract_msg.Message(str(tmp_path), delayAttachments=True)
            else:
                msg = extract_msg.Message(str(abs_path), delayAttachments=True)
        except Exception as e:
            # Check if it's an OLE file (MSG)
            is_ole_file = False
            try:
                if decoded_bytes is not None:
                    is_ole_file = decoded_bytes[:8] == _OLE_SIGNATURE
                else:
                    with open(abs_path, "rb") as f:
                        header = f.read(8)
                        if header == _OLE_SIGNATURE:
                            is_ole_file = True
            except Exception as exc:
                current_app.logger.warning(
                    f"Failed to check MSG OLE header for {file_asset_id}: {exc}"
                )

            if is_ole_file:
                if decoded_bytes is not None:
                    report_swallowed_exception(
                        e,
                        context="case.file_assets.view_msg.parse_ole.decoded",
                        log_key="case.file_assets.view_msg.parse_ole.decoded",
                        log_window_seconds=300,
                    )
                    current_app.logger.error(
                        f"Failed to parse MSG (OLE, decoded legacy) {file_asset_id} (len={len(decoded_bytes)}): {e}"
                    )
                else:
                    report_swallowed_exception(
                        e,
                        context="case.file_assets.view_msg.parse_ole",
                        log_key="case.file_assets.view_msg.parse_ole",
                        log_window_seconds=300,
                    )
                    current_app.logger.error(f"Failed to parse MSG (OLE) {file_asset_id}: {e}")

                if decoded_bytes is not None and _ole_header_suggests_truncation(decoded_bytes):
                    flash(
                        "MSG File  from  Save Preview . (Original /Upload Required)",
                        "danger",
                    )
                else:
                    flash(" Password MSG File  exists.", "danger")
                return redirect(url_for("case_work.case_detail", case_id=case_id))

            # Fallback: Try parsing as EML (text-based) if NOT an OLE file
            # But first, verify it actually LOOKS like an EML (has headers)
            try:
                if decoded_bytes is not None:
                    _msg_check = BytesParser(policy=policy.default).parsebytes(decoded_bytes)
                else:
                    with open(abs_path, "rb") as f:
                        _msg_check = BytesParser(policy=policy.default).parse(f)
                if not _msg_check.keys():
                    # No headers -> likely not an email, but garbage text or binary
                    raise ValueError("No email headers found")
            except Exception as check_err:
                report_swallowed_exception(
                    check_err,
                    context="case.file_assets.view_msg.fallback_eml_check",
                    log_key="case.file_assets.view_msg.fallback_eml_check",
                    log_window_seconds=300,
                )
                current_app.logger.error(
                    f"MSG parsing failed and EML fallback rejected: {check_err}"
                )
                flash("MSG File  Failed.", "danger")
                return redirect(url_for("case_work.case_detail", case_id=case_id))

            current_app.logger.info(
                f"MSG parsing failed for {file_asset_id}, trying EML fallback. Error: {e}"
            )
            if decoded_bytes is not None:
                return _parse_and_render_eml(
                    case_id, file_asset_id, abs_path, original_name, data=decoded_bytes
                )
            return _parse_and_render_eml(case_id, file_asset_id, abs_path, original_name)

        try:
            # Extract metadata
            subject = msg.subject or "(Title None)"
            from_addr = msg.sender or ""
            to_addr = msg.to or ""
            cc_addr = msg.cc or ""
            date_str = str(msg.date) if msg.date else ""
            date_parsed = msg.date  # extract-msg date is datetime object or None

            # Extract body
            body_html, body_text = _extract_msg_body_safe(msg, file_asset_id=file_asset_id)

            # Attachments
            attachments = []
            for idx, att in enumerate(msg.attachments, start=1):
                filename = _extract_msg_attachment_filename_safe(
                    att, index=idx, file_asset_id=file_asset_id
                )
                attachments.append(
                    {
                        "filename": filename,
                        # extract-msg does not reliably expose MIME type per attachment.
                        "content_type": "application/octet-stream",
                    }
                )

            # Sanitize HTML
            safe_body_html = sanitize_email_html(body_html) if body_html else None
            if safe_body_html is not None and not safe_body_html.strip():
                safe_body_html = None

            return render_template(
                "case/view_eml.html",
                case_id=case_id,
                file_asset_id=file_asset_id,
                original_name=original_name,
                subject=subject,
                from_addr=from_addr,
                to_addr=to_addr,
                cc_addr=cc_addr,
                date_str=date_str,
                date_parsed=date_parsed,
                body_html=safe_body_html,
                body_text=body_text,
                attachments=attachments,
            )

        except Exception as e:
            report_swallowed_exception(
                e,
                context="case.file_assets.view_msg.process_content",
                log_key="case.file_assets.view_msg.process_content",
                log_window_seconds=300,
            )
            current_app.logger.error(f"Error processing MSG content {file_asset_id}: {e}")
            flash("MSG Content Process Failed.", "danger")
            return redirect(url_for("case_work.case_detail", case_id=case_id))
    finally:
        try:
            if msg:
                msg.close()
        except Exception:
            current_app.logger.warning("MSG close failed for %s", file_asset_id, exc_info=True)
        if tmp_path:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                current_app.logger.warning(
                    "MSG temp cleanup failed for %s", file_asset_id, exc_info=True
                )
