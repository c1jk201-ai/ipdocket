"""History letter routes.

Handles CRUD operations for communications (letters).
"""

from __future__ import annotations

import tempfile
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.models.email_automation import EmailMessage, EmailMessageMatterLink
from app.models.ip_records import FileAsset, Matter
from app.services.core.staff_options import (
    build_staff_owner_options,
    default_staff_owner_value,
    resolve_staff_party_id,
)
from app.services.history.communication_service import CommunicationData, get_communication_service
from app.services.mail.email_ingestion import _parse_eml
from app.services.matter.matter_auto_status import date_only_str as _svc_date_only_str
from app.services.storage.file_asset_service import get_file_asset_service
from app.services.uploads.upload_session_service import get_upload_session_service
from app.utils.error_logging import report_swallowed_exception
from app.utils.html_sanitizer import sanitize_email_html
from app.utils.mime_headers import decode_mime_encoded_words, normalize_uploaded_filename
from app.utils.permissions import require_matter_access

from ._common import (
    parse_popup_param,
    render_duplicate_confirm,
    render_popup_done,
    validate_csrf_or_redirect,
)
from .file_assets import (
    _extract_eml_body_safe,
    _extract_msg_attachment_filename_safe,
    _extract_msg_body_safe,
    _looks_like_pgloader_vector_blob,
    _maybe_decode_pgloader_vector_blob,
)


def file_asset_id_for_path(path: str | None) -> str | None:
    raw = (path or "").strip()
    if not raw:
        return None
    norm = raw.replace("\\", "/").strip("/")
    candidates = {norm}
    legacy_prefix = "data/uploads/"
    if norm.lower().startswith(legacy_prefix):
        candidates.add(norm[len(legacy_prefix) :])
    else:
        candidates.add(f"{legacy_prefix}{norm}")
    row = (
        FileAsset.query.with_entities(FileAsset.file_asset_id)
        .filter(FileAsset.file_path.in_(sorted(candidates)))
        .first()
    )
    return str(row[0]) if row and row[0] else None

_EMAIL_PREVIEW_MAX_BYTES = (
    2 * 1024 * 1024
)  # 2MB guard for popup latency and large attachments.
_TRUTHY = {"1", "true", "yes", "y", "on"}


def _load_file_asset_bytes(file_service, file_path: str, *, max_bytes: int | None = None):
    try:
        abs_path = file_service.abs_path(file_path)
    except Exception:
        return None, "Invalid file path."
    if not abs_path.exists():
        return None, "File not found."
    try:
        size = abs_path.stat().st_size
    except Exception:
        size = None
    if max_bytes and size and size > max_bytes:
        return None, f"File is too large for preview. ({size:,} bytes)"
    if _looks_like_pgloader_vector_blob(abs_path):
        raw = abs_path.read_bytes()
        decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
        if ok:
            return decoded, None
    return abs_path.read_bytes(), None


def _parse_eml_bytes(data: bytes) -> dict:
    meta, body_text, body_html, attachments = _parse_eml(data or b"")
    date_parsed = meta.get("date_parsed")
    return {
        "subject": meta.get("subject") or "(Title None)",
        "from_addr": meta.get("from") or "",
        "to_addr": meta.get("to") or "",
        "cc_addr": meta.get("cc") or "",
        "date_str": date_parsed.isoformat() if date_parsed else (meta.get("date") or ""),
        "date_parsed": date_parsed,
        "body_html": body_html,
        "body_text": body_text,
        "attachments": [
            {
                "filename": item.get("filename") or "attachment",
                "content_type": item.get("content_type") or "",
                "size": item.get("size") or 0,
            }
            for item in attachments
        ],
    }


def _parse_msg_bytes(data: bytes) -> tuple[dict | None, str | None]:
    msg = None
    tmp_path: Path | None = None
    try:
        import extract_msg

        with tempfile.NamedTemporaryFile(delete=False, suffix=".msg") as tmp:
            tmp.write(data or b"")
            tmp_path = Path(tmp.name)
        try:
            msg = extract_msg.Message(str(tmp_path), delayAttachments=True)
        except TypeError:
            msg = extract_msg.Message(str(tmp_path))

        date_value = getattr(msg, "date", None)
        date_parsed = date_value
        date_str = str(date_value) if date_value else ""
        if isinstance(date_value, str) and date_value.strip():
            try:
                date_parsed = parsedate_to_datetime(date_value)
            except Exception:
                date_parsed = None

        body_html, body_text = _extract_msg_body_safe(
            msg, file_asset_id="history_msg_preview"
        )
        safe_body_html = sanitize_email_html(body_html) if body_html else None
        if safe_body_html is not None and not safe_body_html.strip():
            safe_body_html = None

        attachments = []
        for idx, att in enumerate(getattr(msg, "attachments", []) or [], start=1):
            attachments.append(
                {
                    "filename": _extract_msg_attachment_filename_safe(
                        att, index=idx, file_asset_id="history_msg_preview"
                    ),
                    "content_type": "application/octet-stream",
                    "size": 0,
                }
            )

        return {
            "subject": decode_mime_encoded_words(getattr(msg, "subject", None))
            or "(Title None)",
            "from_addr": decode_mime_encoded_words(getattr(msg, "sender", None)),
            "to_addr": decode_mime_encoded_words(getattr(msg, "to", None)),
            "cc_addr": decode_mime_encoded_words(getattr(msg, "cc", None)),
            "date_str": date_str,
            "date_parsed": date_parsed,
            "body_html": safe_body_html,
            "body_text": body_text,
            "attachments": attachments,
        }, None
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="case.history_letter._parse_msg_bytes",
            log_key="case.history_letter._parse_msg_bytes",
            log_window_seconds=300,
        )
        return None, str(exc)
    finally:
        if msg is not None:
            try:
                msg.close()
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="case.history_letter._parse_msg_bytes.close",
                    log_key="case.history_letter._parse_msg_bytes.close",
                    log_window_seconds=300,
                )
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="case.history_letter._parse_msg_bytes.unlink",
                    log_key="case.history_letter._parse_msg_bytes.unlink",
                    log_window_seconds=300,
                )


def _staff_label_by_party_id(party_id: str | None) -> str:
    """Resolve staff display label without loading the entire staff list."""
    if not party_id:
        return ""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    row = db.session.execute(
        text(
            """
            SELECT p.name_display, ps.staff_code
            FROM party p
            LEFT JOIN party_staff ps ON p.party_id = ps.party_id
            WHERE p.party_id = :pid
            LIMIT 1
            """
        ).execution_options(policy_bypass=True),
        {"pid": str(party_id)},
    ).fetchone()
    if not row:
        return ""
    name, code = row[0], row[1]
    if name and code:
        return f"{name} ({code})"
    return (name or "").strip() or (code or "").strip()


def _get_active_staff_options():
    """Get active staff options for searchable owner pickers."""
    return build_staff_owner_options(category="all")


def _current_staff_value():
    """Get current user's default staff picker value."""
    if current_user.is_authenticated:
        return default_staff_owner_value(current_user)
    return ""


def _find_staff_party_id_by_input(raw: str):
    """Find staff party ID by picker text, code, email, name, or ID."""
    return resolve_staff_party_id(raw)


def _normalize_edit_comm_type(raw: str | None, existing: str | None) -> str:
    """Normalize editable communication types without downgrading responses."""
    existing_type = (existing or "").strip().upper()
    submitted_type = (raw or "").strip().upper()

    # Response upload rows are managed by the response/upload workflow. The generic
    # letter edit form may update dates, owner, title, and attachments, but must not
    # silently reclassify them as normal letters.
    if existing_type == "R":
        return "R"

    if submitted_type in {"M", "T"}:
        return submitted_type
    if existing_type in {"M", "T"}:
        return existing_type
    return "M"


@bp.route("/<case_id>/history/letter/new", methods=["GET", "POST"])
@login_required
def history_letter_new(case_id: str):
    """  Registration"""
    popup = parse_popup_param(request)
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    staff_options = _get_active_staff_options()
    default_staff_value = _current_staff_value()

    if request.method == "GET":
        return render_template(
            "case/history_letter_form.html",
            case_id=str(case_id),
            staff_options=staff_options,
            default_staff_value=default_staff_value,
            popup=popup,
        )

    # POST handling
    file_service = get_file_asset_service()
    session_service = get_upload_session_service()
    comm_service = get_communication_service()

    # Parse form data
    direction = (request.form.get("direction") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    to_text = (request.form.get("to_text") or "").strip()
    received_date = _svc_date_only_str(request.form.get("received_date") or "")
    sent_date = _svc_date_only_str(request.form.get("sent_date") or "")
    due_date = _svc_date_only_str(request.form.get("due_date") or "")
    done_date = _svc_date_only_str(request.form.get("done_date") or "")
    staff_raw = (request.form.get("manager") or "").strip()
    owner_staff_party_id = _find_staff_party_id_by_input(staff_raw)
    comm_type = _normalize_edit_comm_type(request.form.get("comm_type"), "M")

    # Check for session-based confirm
    upload_session_id = request.form.get("upload_session_id")
    confirm_action = request.form.get("confirm_action")

    if upload_session_id and confirm_action == "proceed":
        # Retrieve staged files from session
        session_data = session_service.retrieve(upload_session_id)
        if not session_data:
            flash(" . Retry.", "warning")
            return redirect(url_for("case_work.history_letter_new", case_id=case_id, popup=popup))

        staged_files = session_data.staged_files
        form_data = session_data.form_data

        # Use form data from session
        direction = form_data.get("direction", direction)
        subject = form_data.get("subject", subject)
        to_text = form_data.get("to_text", to_text)
        received_date = form_data.get("received_date", received_date)
        sent_date = form_data.get("sent_date", sent_date)
        due_date = form_data.get("due_date", due_date)
        done_date = form_data.get("done_date", done_date)
        owner_staff_party_id = form_data.get("owner_staff_party_id", owner_staff_party_id)
        comm_type = _normalize_edit_comm_type(form_data.get("comm_type", comm_type), "M")

        session_service.delete(upload_session_id)
    else:
        # Stage files
        files = request.files.getlist("files")
        staged_files = []

        for f in files or []:
            if not (f.filename or "").strip():
                continue
            try:
                sf = file_service.stage_upload(f, subdir=f"matter/{case_id}/letters")
                staged_files.append(sf)
            except Exception as e:
                flash(f"File Upload failed: {e}", "danger")

        # Check for duplicates
        duplicates = [sf for sf in staged_files if not sf.is_new]

        if duplicates:
            session_id = session_service.create(
                purpose="letter",
                staged_files=staged_files,
                form_data={
                    "direction": direction,
                    "subject": subject,
                    "to_text": to_text,
                    "received_date": received_date or "",
                    "sent_date": sent_date or "",
                    "due_date": due_date or "",
                    "done_date": done_date or "",
                    "owner_staff_party_id": owner_staff_party_id,
                    "comm_type": comm_type,
                },
            )
            return render_duplicate_confirm(
                duplicates=[
                    {
                        "upload_name": sf.original_name,
                        "original_name": sf.original_name,
                        "created_at": None,
                    }
                    for sf in duplicates
                ],
                confirm_url=url_for("case_work.history_letter_new", case_id=case_id, popup=popup),
                cancel_url=url_for("case_work.case_detail", case_id=case_id),
                upload_session_id=session_id,
            )

    # Create communication
    data = CommunicationData(
        matter_id=str(case_id),
        direction=direction,
        subject=subject,
        to_text=to_text,
        received_date=received_date,
        sent_date=sent_date,
        due_date=due_date,
        done_date=done_date,
        owner_staff_party_id=owner_staff_party_id,
        comm_type=comm_type,
        source="history_letter_new",
        actor_user_id=getattr(current_user, "id", None),
    )

    result = comm_service.create(data, staged_files=staged_files)

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if not result.success:
        try:
            from app.extensions import db

            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="history_letter_new.rollback_on_failure",
                log_key="history_letter_new.rollback_on_failure",
                log_window_seconds=300,
            )
        return redirect(url_for("case_work.history_letter_new", case_id=case_id, popup=popup))

    try:
        from app.extensions import db

        db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="history_letter_new.rollback_on_commit_failure",
                log_key="history_letter_new.rollback_on_commit_failure",
                log_window_seconds=300,
            )
        report_swallowed_exception(
            exc,
            context="history_letter_new.commit_failed",
            log_key="history_letter_new.commit_failed",
            log_window_seconds=300,
        )
        flash(" Save In Progress Error . Retry.", "danger")
        return redirect(url_for("case_work.history_letter_new", case_id=case_id, popup=popup))

    if popup:
        return render_popup_done(
            title=" Registration.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/history/letter/<comm_id>/view")
@login_required
def history_letter_view(case_id: str, comm_id: str):
    """ Search (Read-only)"""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    popup = parse_popup_param(request)
    is_popup = bool(popup)
    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="view")

    load_email_arg = request.args.get("load_email")
    if load_email_arg is None:
        # Default:
        # - popup=1: quick preview starts with email body loading off
        # - normal page: keep existing eager loading behavior
        load_email = not is_popup
    else:
        load_email = (load_email_arg or "").strip().lower() in _TRUTHY
    email_body_loaded = bool(load_email)
    email_hint = (
        "Email body preview is not loaded in the quick popup."
        if is_popup and not load_email
        else None
    )

    # Load existing communication
    row = db.session.execute(
        text(
            """
            SELECT comm_type, received_date, sent_date, due_date, done_date, to_text, note, owner_staff_party_id, body
            FROM communication WHERE comm_id = :cid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"cid": comm_id, "mid": case_id},
    ).fetchone()

    if not row:
        if current_app.config.get("TESTING"):
            current_app.logger.warning(
                "history_letter_view: communication not found (case_id=%s comm_id=%s)",
                case_id,
                comm_id,
            )
        flash("   none.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    rows = db.session.execute(
        text(
            """
            SELECT fa.file_asset_id, fa.original_name, fa.byte_size, fa.mime_type, fa.file_path
            FROM communication_file_asset cfa
            JOIN file_asset fa ON cfa.file_asset_id = fa.file_asset_id
            WHERE cfa.comm_id = :cid
        """
        ).execution_options(policy_bypass=True),
        {"cid": comm_id},
    ).fetchall()

    existing_files = [
        {
            "file_asset_id": r[0],
            "original_name": r[1],
            "byte_size": r[2],
            "mime_type": r[3],
            "file_path": r[4],
        }
        for r in rows
    ]

    manager_label = _staff_label_by_party_id(str(row[7]) if row[7] else None)

    comm_type = (row[0] or "").strip().upper()
    sent_date = row[2] or ""
    if False and comm_type == "R":
        direction = ""
    else:
        direction = "Send" if sent_date else ""

    email_preview = None
    email_error = None
    email_source = None
    email_file_asset_id = None
    email_file_ext = None
    can_load_email_preview = False

    if comm_type != "R":
        link = EmailMessageMatterLink.query.filter_by(comm_id=str(comm_id)).first()
        if link and link.email_id:
            email_q = EmailMessage.query.filter_by(id=str(link.email_id)).order_by(
                EmailMessage.received_at.desc()
            )
        else:
            email_q = EmailMessage.query.filter_by(linked_comm_id=comm_id).order_by(
                EmailMessage.received_at.desc()
            )
        if not load_email:
            # popup Default from  body_html/body_text    (/  )
            from sqlalchemy.orm import load_only

            email_q = email_q.options(
                load_only(
                    EmailMessage.id,
                    EmailMessage.subject,
                    EmailMessage.from_addr,
                    EmailMessage.to_text,
                    EmailMessage.cc_text,
                    EmailMessage.received_at,
                    EmailMessage.raw_eml_path,
                )
            )
        email_msg = email_q.first()

        if email_msg:
            can_load_email_preview = True
            email_source = " "
            email_file_asset_id = file_asset_id_for_path(email_msg.raw_eml_path)
            if email_msg.raw_eml_path:
                email_file_ext = Path(email_msg.raw_eml_path).suffix.lower()

            # popup Default (load_email=False)from Data() Display
            email_preview = {
                "subject": email_msg.subject or "(Title None)",
                "from_addr": email_msg.from_addr or "",
                "to_addr": email_msg.to_text or "",
                "cc_addr": email_msg.cc_text or "",
                "date_str": email_msg.received_at.isoformat() if email_msg.received_at else "",
                "date_parsed": email_msg.received_at,
                "body_html": None,
                "body_text": None,
                "attachments": [],
            }

            if load_email:
                body_html = (
                    sanitize_email_html(email_msg.body_html)
                    if getattr(email_msg, "body_html", None)
                    else None
                )
                if body_html is not None and not body_html.strip():
                    body_html = None
                body_text = getattr(email_msg, "body_text", None)

                # DB    Original   EML to ( )
                if (not body_html and not body_text) and email_msg.raw_eml_path:
                    ext = (email_file_ext or "").lower()
                    if ext and ext != ".eml":
                        email_error = "This email source is not EML, so automatic preview was skipped on this page."
                    else:
                        file_service = get_file_asset_service()
                        data, err = _load_file_asset_bytes(
                            file_service, email_msg.raw_eml_path, max_bytes=_EMAIL_PREVIEW_MAX_BYTES
                        )
                        if data:
                            try:
                                email_preview = _parse_eml_bytes(data)
                                email_source = "Email source (original EML)"
                            except Exception as exc:
                                email_error = f"EML parse failed: {exc}"
                        else:
                            email_error = err
                else:
                    email_preview["body_html"] = body_html
                    email_preview["body_text"] = body_text

        if not email_msg:
            # from Email File (EML , MSG )    
            email_candidates = [
                f
                for f in existing_files
                if (f.get("original_name") or "").lower().endswith((".eml", ".msg"))
            ]
            email_candidates.sort(
                key=lambda f: 0 if (f.get("original_name") or "").lower().endswith(".eml") else 1
            )
            if email_candidates:
                chosen = email_candidates[0]
                email_source = chosen.get("original_name") or "attached email"
                email_file_asset_id = chosen.get("file_asset_id")
                email_file_ext = Path(chosen.get("original_name") or "").suffix.lower() or None
                if (email_file_ext or "").lower() == ".eml":
                    can_load_email_preview = True

                if load_email:
                    # :  from MSG Auto   (  )
                    if (email_file_ext or "").lower() == ".msg":
                        email_error = "MSG email is not auto-previewed on this page. Use Open MSG instead."
                    elif (email_file_ext or "").lower() == ".eml":
                        file_service = get_file_asset_service()
                        data, err = _load_file_asset_bytes(
                            file_service,
                            chosen.get("file_path") or "",
                            max_bytes=_EMAIL_PREVIEW_MAX_BYTES,
                        )
                        if data:
                            try:
                                email_preview = _parse_eml_bytes(data)
                                email_source = chosen.get("original_name") or "EML"
                            except Exception as exc:
                                email_error = f"{chosen.get('original_name')}: {exc}"
                        else:
                            email_error = err

        # email_preview None Email File  (: MSG , popup Default  )
        if not email_preview and (email_file_asset_id or email_error):
            email_preview = None

    return render_template(
        "case/history_letter_view.html",
        matter=matter,
        case_id=str(case_id),
        comm_id=comm_id,
        comm_type=comm_type,
        direction=direction,
        received_date=row[1] or "",
        sent_date=sent_date,
        due_date=row[3] or "",
        done_date=row[4] or "",
        to_text=row[5] or "",
        subject=(row[6] or "").strip()
        or ((row[8] or "").strip()[:200] if len(row) > 8 and row[8] else ""),
        manager_label=manager_label,
        existing_files=existing_files,
        email_preview=email_preview,
        email_error=email_error,
        email_source=email_source,
        email_file_asset_id=email_file_asset_id,
        email_file_ext=email_file_ext,
        email_body_loaded=email_body_loaded,
        can_load_email_preview=can_load_email_preview,
        email_hint=email_hint,
        popup=popup,
    )


@bp.route("/<case_id>/history/letter/<comm_id>/edit", methods=["GET", "POST"])
@login_required
def history_letter_edit(case_id: str, comm_id: str):
    """ Edit"""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    popup = parse_popup_param(request)
    Matter.query.get_or_404(case_id)

    # Load existing communication
    row = db.session.execute(
        text(
            """
            SELECT received_date, sent_date, due_date, done_date, to_text, note, owner_staff_party_id, comm_type, body
            FROM communication WHERE comm_id = :cid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"cid": comm_id, "mid": case_id},
    ).fetchone()

    if not row:
        flash("   none.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    staff_options = _get_active_staff_options()
    comm_service = get_communication_service()

    if request.method == "GET":
        selected_staff_value = ""
        owner_id = str(row[6]) if row[6] else ""
        for s in staff_options:
            if (s.get("staff_party_id") or "") == owner_id:
                selected_staff_value = s.get("value") or ""
                break
        if not selected_staff_value and owner_id:
            selected_staff_value = _staff_label_by_party_id(owner_id)

        existing_files = comm_service.get_attachments(comm_id)

        return render_template(
            "case/history_letter_form.html",
            case_id=str(case_id),
            comm_id=comm_id,
            is_edit=True,
            form_action=url_for(
                "case_work.history_letter_edit", case_id=case_id, comm_id=comm_id, popup=popup
            ),
            row={
                "received_date": row[0] or "",
                "sent_date": row[1] or "",
                "due_date": row[2] or "",
                "done_date": row[3] or "",
                "to_text": row[4] or "",
                "subject": (row[5] or "").strip()
                or ((row[8] or "").strip()[:200] if len(row) > 8 and row[8] else ""),
                "direction": "Send" if row[1] else "",
            },
            comm_type=row[7] or "M",
            selected_staff_value=selected_staff_value,
            existing_files=existing_files,
            staff_options=staff_options,
            default_staff_value=_current_staff_value(),
            popup=popup,
        )

    # POST handling
    file_service = get_file_asset_service()

    direction = (request.form.get("direction") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    to_text = (request.form.get("to_text") or "").strip()
    received_date = _svc_date_only_str(request.form.get("received_date") or "")
    sent_date = _svc_date_only_str(request.form.get("sent_date") or "")
    due_date = _svc_date_only_str(request.form.get("due_date") or "")
    done_date = _svc_date_only_str(request.form.get("done_date") or "")
    staff_raw = (request.form.get("manager") or "").strip()
    owner_staff_party_id = _find_staff_party_id_by_input(staff_raw)
    comm_type = _normalize_edit_comm_type(request.form.get("comm_type"), row[7])

    # Stage new files
    files = request.files.getlist("files")
    staged_files = []
    for f in files or []:
        if not (f.filename or "").strip():
            continue
        try:
            sf = file_service.stage_upload(f, subdir=f"matter/{case_id}/letters")
            staged_files.append(sf)
        except Exception as e:
            flash(f"File Upload failed: {e}", "danger")

    # Get files to remove
    remove_file_ids = request.form.getlist("remove_files")

    data = CommunicationData(
        matter_id=str(case_id),
        direction=direction,
        subject=subject,
        to_text=to_text,
        received_date=received_date,
        sent_date=sent_date,
        due_date=due_date,
        done_date=done_date,
        owner_staff_party_id=owner_staff_party_id,
        comm_type=comm_type,
        source="history_letter_edit",
        actor_user_id=getattr(current_user, "id", None),
    )

    result = comm_service.update(
        comm_id,
        data,
        staged_files=staged_files,
        remove_file_ids=remove_file_ids,
    )

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if result.success:
        try:
            from app.extensions import db

            db.session.commit()
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="history_letter_edit.rollback_on_commit_failure",
                    log_key="history_letter_edit.rollback_on_commit_failure",
                    log_window_seconds=300,
                )
            report_swallowed_exception(
                exc,
                context="history_letter_edit.commit_failed",
                log_key="history_letter_edit.commit_failed",
                log_window_seconds=300,
            )
            flash(" Edit In Progress Error . Retry.", "danger")
            return redirect(
                url_for(
                    "case_work.history_letter_edit", case_id=case_id, comm_id=comm_id, popup=popup
                )
            )
    else:
        try:
            from app.extensions import db

            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="history_letter_edit.rollback_on_failure",
                log_key="history_letter_edit.rollback_on_failure",
                log_window_seconds=300,
            )

    if popup:
        return render_popup_done(
            title=" Edit.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/history/letter/<comm_id>/delete", methods=["POST"])
@login_required
def history_letter_delete(case_id: str, comm_id: str):
    """ Delete"""
    popup = parse_popup_param(request)
    csrf_error = validate_csrf_or_redirect(
        request.form.get("csrf_token"), "case_work.case_detail", case_id=case_id
    )
    if csrf_error:
        return csrf_error
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    comm_service = get_communication_service()
    result = comm_service.delete(comm_id, case_id)

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if result.success:
        try:
            from app.extensions import db

            db.session.commit()
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception as rollback_exc:
                report_swallowed_exception(
                    rollback_exc,
                    context="history_letter_delete.rollback_on_commit_failure",
                    log_key="history_letter_delete.rollback_on_commit_failure",
                    log_window_seconds=300,
                )
            report_swallowed_exception(
                exc,
                context="history_letter_delete.commit_failed",
                log_key="history_letter_delete.commit_failed",
                log_window_seconds=300,
            )
            flash(" Delete In Progress Error . Retry.", "danger")
            return redirect(url_for("case_work.case_detail", case_id=case_id))
    else:
        try:
            from app.extensions import db

            db.session.rollback()
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="history_letter_delete.rollback_on_failure",
                log_key="history_letter_delete.rollback_on_failure",
                log_window_seconds=300,
            )

    if popup:
        return render_popup_done(
            title=" Delete.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))
