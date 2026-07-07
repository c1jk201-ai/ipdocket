"""History notice routes.

Handles CRUD operations for office actions.
"""

from __future__ import annotations

from pathlib import Path

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter
from app.services.case.case_audit_service import record_case_audit
from app.services.citations.cited_reference_service import (
    create_ids_tasks_for_us_family,
    extract_citations_from_pdf_bytes,
    office_action_citation_rows,
    office_action_has_manual_citations,
    replace_office_action_citations_from_text,
    save_auto_office_action_citations,
)
from app.services.history.office_action_service import OfficeActionData, get_office_action_service
from app.services.matter.matter_auto_status import date_only_str as _svc_date_only_str
from app.services.storage.file_asset_service import get_file_asset_service
from app.services.uploads.upload_session_service import get_upload_session_service
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter, require_matter_access
from app.utils.policy_sql import policy_text as text

from ._common import (
    parse_popup_param,
    render_duplicate_confirm,
    render_popup_done,
    validate_csrf_or_redirect,
)
from .file_assets import _looks_like_pgloader_vector_blob, _maybe_decode_pgloader_vector_blob

_TRUTHY = {"1", "true", "yes", "y", "on"}
_NOTICE_BLOCKED_RESPONSE_EXTS: set[str] = set()


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
        return None, f"File is too large for automatic parsing. ({size:,} bytes)"
    if _looks_like_pgloader_vector_blob(abs_path):
        raw = abs_path.read_bytes()
        decoded, ok = _maybe_decode_pgloader_vector_blob(raw)
        if ok:
            return decoded, None
    return abs_path.read_bytes(), None


def _partition_notice_upload_files(
    files: list,
    *,
    enforce_response_block: bool,
) -> tuple[list, list[str]]:
    """Split notice uploads into accepted files and response-only files."""
    accepted = []
    rejected: list[str] = []
    for f in files or []:
        name = (getattr(f, "filename", "") or "").strip()
        if not name:
            continue
        ext = Path(name).suffix.lower()
        if enforce_response_block and ext in _NOTICE_BLOCKED_RESPONSE_EXTS:
            rejected.append(name)
            continue
        accepted.append(f)
    return accepted, rejected


def _staged_file_asset_id(staged_file) -> str | None:
    if isinstance(staged_file, dict):
        return (staged_file.get("file_asset_id") or "").strip() or None
    return (getattr(staged_file, "file_asset_id", "") or "").strip() or None


def _staged_original_name(staged_file) -> str:
    if isinstance(staged_file, dict):
        return (staged_file.get("original_name") or "").strip()
    return (getattr(staged_file, "original_name", "") or "").strip()


def _extract_notice_citations_from_staged_files(staged_files: list | None) -> list:
    file_service = get_file_asset_service()
    drafts = []
    for sf in staged_files or []:
        file_asset_id = _staged_file_asset_id(sf)
        original_name = _staged_original_name(sf)
        ext = Path(original_name).suffix.lower()
        if not file_asset_id or ext != ".pdf":
            continue
        try:
            data = file_service.read_all(file_asset_id)
        except Exception as exc:
            current_app.logger.info(
                "Notice citation extraction skipped: file_asset_id=%s error=%s",
                file_asset_id,
                exc,
            )
            continue
        if ext == ".pdf":
            drafts.extend(extract_citations_from_pdf_bytes(data))
            continue
    return drafts


def _save_notice_citations_and_ids(
    *,
    matter_id: str,
    oa_id: str | None,
    citation_text: str | None,
    staged_files: list | None,
    doc_name: str,
    clear_auto_when_empty: bool = False,
) -> int:
    if not oa_id:
        return 0
    if (citation_text or "").strip():
        rows = replace_office_action_citations_from_text(
            matter_id=str(matter_id),
            office_action_id=str(oa_id),
            text_value=citation_text,
            source="manual",
        )
    else:
        drafts = _extract_notice_citations_from_staged_files(staged_files)
        if drafts:
            rows = save_auto_office_action_citations(
                matter_id=str(matter_id),
                office_action_id=str(oa_id),
                drafts=drafts,
                clear_when_empty=clear_auto_when_empty,
            )
        else:
            if clear_auto_when_empty:
                save_auto_office_action_citations(
                    matter_id=str(matter_id),
                    office_action_id=str(oa_id),
                    drafts=[],
                    clear_when_empty=True,
                )
            return 0
    if not rows:
        return 0
    ids_result = create_ids_tasks_for_us_family(
        source_matter_id=str(matter_id),
        source_oa_id=str(oa_id),
        citations=rows,
        source_doc_name=doc_name,
        actor_id=getattr(current_user, "id", None),
    )
    if ids_result.created_count:
        flash(f"Created {ids_result.created_count} US family IDS filing tasks.", "info")
    return len(rows)


def _try_save_notice_citations_and_ids(
    *,
    matter_id: str,
    oa_id: str | None,
    citation_text: str | None,
    staged_files: list | None,
    doc_name: str,
    clear_auto_when_empty: bool = False,
    context: str,
) -> int:
    try:
        with db.session.begin_nested():
            return _save_notice_citations_and_ids(
                matter_id=matter_id,
                oa_id=oa_id,
                citation_text=citation_text,
                staged_files=staged_files,
                doc_name=doc_name,
                clear_auto_when_empty=clear_auto_when_empty,
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context=context,
            log_key=context,
            log_window_seconds=300,
        )
        current_app.logger.exception(
            "Notice citation save failed (matter_id=%s, oa_id=%s)",
            matter_id,
            oa_id,
        )
        flash(
            "Office correspondence was saved, but cited-reference auto-save failed. Reparse it from the office correspondence view.",
            "warning",
        )
        return 0


def _notice_attachment_rows(oa_id: str) -> list[dict]:
    rows = db.session.execute(
        text(
            """
            SELECT fa.file_asset_id, fa.original_name, fa.byte_size, fa.mime_type, fa.file_path
            FROM office_action_file_asset oafa
            JOIN file_asset fa ON oafa.file_asset_id = fa.file_asset_id
            WHERE oafa.oa_id = :oid
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id},
    ).fetchall()
    return [
        {
            "file_asset_id": r[0],
            "original_name": r[1],
            "byte_size": r[2],
            "mime_type": r[3],
            "file_path": r[4],
        }
        for r in rows
    ]


@bp.route("/<case_id>/history/notice/new", methods=["GET", "POST"])
@login_required
def history_notice_new(case_id: str):
    """Create office correspondence."""
    popup = parse_popup_param(request)
    matter = Matter.query.get_or_404(case_id)
    notice_block_response_uploads = False
    require_matter_access(str(case_id), action="edit_case")

    if request.method == "GET":
        return render_template(
            "case/history_notice_form.html",
            case_id=str(case_id),
            notice_block_response_uploads=notice_block_response_uploads,
            popup=popup,
        )

    # POST handling
    file_service = get_file_asset_service()
    session_service = get_upload_session_service()
    oa_service = get_office_action_service()

    # Parse form data
    doc_name = (request.form.get("doc_name") or "").strip()
    received_date = _svc_date_only_str(request.form.get("received_date") or "")
    notified_date = _svc_date_only_str(request.form.get("notified_date") or "")
    due_date = _svc_date_only_str(request.form.get("due_date") or "")
    extended_due_date = _svc_date_only_str(request.form.get("extended_due_date") or "")
    done_date = _svc_date_only_str(request.form.get("done_date") or "")
    examiner = (request.form.get("examiner") or "").strip()
    # Check for session-based confirm
    upload_session_id = request.form.get("upload_session_id")
    confirm_action = request.form.get("confirm_action")

    if upload_session_id and confirm_action == "proceed":
        # Retrieve staged files from session
        session_data = session_service.retrieve(upload_session_id)
        if not session_data:
            flash("The upload session expired. Please try again.", "warning")
            return redirect(url_for("case_work.history_notice_new", case_id=case_id, popup=popup))

        staged_files = session_data.staged_files
        form_data = session_data.form_data

        filtered_staged = []
        blocked_names: list[str] = []
        for sf in staged_files or []:
            original_name = (
                sf.get("original_name", "")
                if isinstance(sf, dict)
                else getattr(sf, "original_name", "")
            )
            ext = Path((original_name or "").strip()).suffix.lower()
            if notice_block_response_uploads and ext in _NOTICE_BLOCKED_RESPONSE_EXTS:
                blocked_names.append(original_name or "(unnamed)")
                continue
            filtered_staged.append(sf)
        staged_files = filtered_staged

        # Use form data from session
        doc_name = form_data.get("doc_name", doc_name)
        received_date = form_data.get("received_date", received_date)
        notified_date = form_data.get("notified_date", notified_date)
        due_date = form_data.get("due_date", due_date)
        extended_due_date = form_data.get("extended_due_date", extended_due_date)
        done_date = form_data.get("done_date", done_date)
        examiner = form_data.get("examiner", examiner)
        for name in blocked_names:
            flash(
                f"Response package files are not allowed as office-action attachments: {name} "
                "(use Filing / Response Upload instead).",
                "warning",
            )

        session_service.delete(upload_session_id)
    else:
        # Stage files
        files = request.files.getlist("files")
        files, blocked_names = _partition_notice_upload_files(
            files,
            enforce_response_block=notice_block_response_uploads,
        )
        for name in blocked_names:
            flash(
                f"Response package files are not allowed as office-action attachments: {name} "
                "(use Filing / Response Upload instead).",
                "warning",
            )
        staged_files = []

        for f in files or []:
            if not (f.filename or "").strip():
                continue
            try:
                sf = file_service.stage_upload(f, subdir=f"matter/{case_id}/notices")
                staged_files.append(sf)
            except Exception as e:
                flash(f"File Upload failed: {e}", "danger")

        conflict_errors = oa_service.format_attachment_conflict_errors(
            oa_service.find_attachment_conflicts(
                matter_id=str(case_id),
                staged_files=staged_files,
            )
        )
        if conflict_errors:
            for error in conflict_errors:
                flash(error, "warning")
            return redirect(url_for("case_work.history_notice_new", case_id=case_id, popup=popup))

        # Check for duplicates
        duplicates = [sf for sf in staged_files if not sf.is_new]

        if duplicates:
            session_id = session_service.create(
                purpose="notice",
                staged_files=staged_files,
                form_data={
                    "doc_name": doc_name,
                    "received_date": received_date or "",
                    "notified_date": notified_date or "",
                    "due_date": due_date or "",
                    "extended_due_date": extended_due_date or "",
                    "done_date": done_date or "",
                    "examiner": examiner,
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
                confirm_url=url_for("case_work.history_notice_new", case_id=case_id, popup=popup),
                cancel_url=url_for("case_work.case_detail", case_id=case_id),
                upload_session_id=session_id,
            )

    # Create office action
    data = OfficeActionData(
        matter_id=str(case_id),
        doc_name=doc_name,
        received_date=received_date,
        notified_date=notified_date,
        due_date=due_date,
        extended_due_date=extended_due_date,
        done_date=done_date,
        examiner=examiner,
    )

    result = oa_service.create(data, staged_files=staged_files)

    # Commit and audit after service-level nested transaction work.
    try:
        if result.success:
            citation_count = _try_save_notice_citations_and_ids(
                matter_id=str(case_id),
                oa_id=result.oa_id,
                citation_text=None,
                staged_files=staged_files,
                doc_name=doc_name,
                context="history_notice.create.citations",
            )
            record_case_audit(
                case_id=str(case_id),
                action="USER",
                field_name="history.notice.create",
                actor_user_id=getattr(current_user, "id", None),
                old_value=None,
                new_value={
                    "oa_id": result.oa_id,
                    "doc_name": doc_name,
                    "due_date": due_date,
                    "cited_references": citation_count,
                    "files": [getattr(sf, "original_name", None) for sf in (staged_files or [])][
                        :10
                    ],
                },
            )
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if not result.success:
        return redirect(url_for("case_work.history_notice_new", case_id=case_id, popup=popup))

    if popup:
        return render_popup_done(
            title="Office correspondence was registered.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/history/notice/<oa_id>/view")
@login_required
def history_notice_view(case_id: str, oa_id: str):
    """View office correspondence."""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    popup = parse_popup_param(request)
    is_popup = bool(popup)
    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="view")

    # Load existing office action
    row = db.session.execute(
        text(
            """
            SELECT doc_name, received_date, notified_date, due_date, extended_due_date, done_date, examiner, raw_id
            FROM office_action WHERE oa_id = :oid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id, "mid": case_id},
    ).fetchone()

    if not row:
        flash("Office correspondence was not found.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    raw_id = (row[7] or "").strip()
    if raw_id.startswith("MIGRATED_TO_COMM:"):
        comm_id = raw_id[len("MIGRATED_TO_COMM:") :].strip().split()[0].split("|")[0].strip()
        if comm_id:
            flash("This item is categorized as a response, so it opens in the correspondence view.", "info")
            return redirect(
                url_for(
                    "case_work.history_letter_view",
                    case_id=str(case_id),
                    comm_id=comm_id,
                    popup=popup,
                )
            )

    existing_files = _notice_attachment_rows(str(oa_id))

    citation_rows = office_action_citation_rows(str(oa_id))

    return render_template(
        "case/history_notice_view.html",
        matter=matter,
        case_id=str(case_id),
        oa_id=oa_id,
        doc_name=row[0] or "",
        received_date=row[1] or "",
        notified_date=row[2] or "",
        due_date=row[3] or "",
        extended_due_date=row[4] or "",
        done_date=row[5] or "",
        examiner=row[6] or "",
        existing_files=existing_files,
        citation_rows=citation_rows,
        can_reparse_citations=can_access_matter(current_user, str(case_id), action="edit_case"),
        popup=popup,
    )


@bp.route("/<case_id>/history/notice/<oa_id>/citations/reparse", methods=["POST"])
@login_required
def history_notice_reparse_citations(case_id: str, oa_id: str):
    popup = parse_popup_param(request)
    next_target = (request.form.get("next") or "").strip().lower()
    csrf_error = validate_csrf_or_redirect(
        request.form.get("csrf_token"),
        "case_work.history_notice_view",
        case_id=case_id,
        oa_id=oa_id,
        popup=popup,
    )
    if csrf_error:
        return csrf_error

    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    row = db.session.execute(
        text(
            """
            SELECT doc_name
            FROM office_action
            WHERE oa_id = :oid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id, "mid": case_id},
    ).fetchone()
    if not row:
        flash("Office correspondence was not found.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    overwrite_manual = (request.form.get("overwrite_manual") or "").strip().lower() in _TRUTHY
    manual_exists = office_action_has_manual_citations(
        matter_id=str(matter.matter_id),
        office_action_id=str(oa_id),
    )
    drafts = _extract_notice_citations_from_staged_files(_notice_attachment_rows(str(oa_id)))
    rows = save_auto_office_action_citations(
        matter_id=str(matter.matter_id),
        office_action_id=str(oa_id),
        drafts=drafts,
        overwrite_manual=overwrite_manual,
    )
    if rows:
        ids_result = create_ids_tasks_for_us_family(
            source_matter_id=str(matter.matter_id),
            source_oa_id=str(oa_id),
            citations=rows,
            source_doc_name=row[0] or "",
            actor_id=getattr(current_user, "id", None),
        )
        db.session.commit()
        msg = f"Reparsed {len(rows)} cited references from AI/PDF data."
        if ids_result.created_count:
            msg += f" Created {ids_result.created_count} US family IDS filing tasks."
        flash(msg, "success")
    else:
        db.session.rollback()
        if manual_exists and not overwrite_manual:
            flash("Manual cited references exist, so AI reparse results were not used to overwrite them.", "warning")
        else:
            flash("No cited references could be automatically extracted from the first two pages.", "info")

    if next_target == "progress":
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))
    return redirect(
        url_for("case_work.history_notice_view", case_id=case_id, oa_id=oa_id, popup=popup)
    )


@bp.route("/<case_id>/history/notice/<oa_id>/edit", methods=["GET", "POST"])
@login_required
def history_notice_edit(case_id: str, oa_id: str):
    """Office correspondence Edit"""
    from app.extensions import db
    from app.utils.policy_sql import policy_text as text

    popup = parse_popup_param(request)
    matter = Matter.query.get_or_404(case_id)
    notice_block_response_uploads = False

    # Load existing office action
    row = db.session.execute(
        text(
            """
            SELECT doc_name, received_date, notified_date, due_date, extended_due_date, done_date, examiner
            FROM office_action WHERE oa_id = :oid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id, "mid": case_id},
    ).fetchone()

    if not row:
        flash("Office correspondence was not found.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    oa_service = get_office_action_service()

    if request.method == "GET":
        existing_files = oa_service.get_attachments(oa_id)
        return render_template(
            "case/history_notice_form.html",
            case_id=str(case_id),
            oa_id=oa_id,
            is_edit=True,
            form_action=url_for(
                "case_work.history_notice_edit", case_id=case_id, oa_id=oa_id, popup=popup
            ),
            oa={
                "doc_name": row[0] or "",
                "received_date": row[1] or "",
                "notified_date": row[2] or "",
                "due_date": row[3] or "",
                "extended_due_date": row[4] or "",
                "done_date": row[5] or "",
                "examiner": row[6] or "",
            },
            existing_files=existing_files,
            notice_block_response_uploads=notice_block_response_uploads,
            popup=popup,
        )

    # POST handling
    file_service = get_file_asset_service()

    doc_name = (request.form.get("doc_name") or "").strip()
    received_date = _svc_date_only_str(request.form.get("received_date") or "")
    notified_date = _svc_date_only_str(request.form.get("notified_date") or "")
    due_date = _svc_date_only_str(request.form.get("due_date") or "")
    extended_due_date = _svc_date_only_str(request.form.get("extended_due_date") or "")
    done_date = _svc_date_only_str(request.form.get("done_date") or "")
    examiner = (request.form.get("examiner") or "").strip()
    # Stage new files
    files = request.files.getlist("files")
    files, blocked_names = _partition_notice_upload_files(
        files,
        enforce_response_block=notice_block_response_uploads,
    )
    for name in blocked_names:
        flash(
            f"Response package files are not allowed as office-action attachments: {name} "
            "(use Filing / Response Upload instead).",
            "warning",
        )
    staged_files = []
    for f in files or []:
        if not (f.filename or "").strip():
            continue
        try:
            sf = file_service.stage_upload(f, subdir=f"matter/{case_id}/notices")
            staged_files.append(sf)
        except Exception as e:
            flash(f"File Upload failed: {e}", "danger")

    # Get files to remove
    remove_file_ids = request.form.getlist("remove_files")

    data = OfficeActionData(
        matter_id=str(case_id),
        doc_name=doc_name,
        received_date=received_date,
        notified_date=notified_date,
        due_date=due_date,
        extended_due_date=extended_due_date,
        done_date=done_date,
        examiner=examiner,
    )

    result = oa_service.update(
        oa_id,
        data,
        staged_files=staged_files,
        remove_file_ids=remove_file_ids,
    )

    try:
        if result.success:
            citation_count = (
                _try_save_notice_citations_and_ids(
                    matter_id=str(case_id),
                    oa_id=str(oa_id),
                    citation_text=None,
                    staged_files=_notice_attachment_rows(str(oa_id)),
                    doc_name=doc_name,
                    clear_auto_when_empty=True,
                    context="history_notice.update.citations",
                )
                if (staged_files or remove_file_ids)
                else 0
            )
            record_case_audit(
                case_id=str(case_id),
                action="USER",
                field_name="history.notice.update",
                actor_user_id=getattr(current_user, "id", None),
                old_value={"oa_id": oa_id},
                new_value={
                    "doc_name": doc_name,
                    "due_date": due_date,
                    "cited_references": citation_count,
                    "add_files": [
                        getattr(sf, "original_name", None) for sf in (staged_files or [])
                    ][:10],
                    "remove_files": (remove_file_ids or [])[:10],
                },
            )
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if not result.success:
        return redirect(
            url_for("case_work.history_notice_edit", case_id=case_id, oa_id=oa_id, popup=popup)
        )

    if popup:
        return render_popup_done(
            title="Office correspondence was updated.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/history/notice/<oa_id>/delete", methods=["POST"])
@login_required
def history_notice_delete(case_id: str, oa_id: str):
    """Office correspondence Delete"""
    popup = parse_popup_param(request)
    csrf_error = validate_csrf_or_redirect(
        request.form.get("csrf_token"), "case_work.case_detail", case_id=case_id
    )
    if csrf_error:
        return csrf_error
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    oa_service = get_office_action_service()
    result = oa_service.delete(oa_id, case_id)

    try:
        if result.success:
            record_case_audit(
                case_id=str(case_id),
                action="USER",
                field_name="history.notice.delete",
                actor_user_id=getattr(current_user, "id", None),
                old_value={"oa_id": oa_id},
                new_value={"deleted": True},
            )
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()

    for msg in result.messages:
        flash(msg, "success")
    for error in result.errors:
        flash(error, "danger")

    if popup:
        return render_popup_done(
            title="Office correspondence was deleted.",
            back_url=url_for("case_work.case_detail", case_id=case_id),
        )
    return redirect(url_for("case_work.case_detail", case_id=case_id))
