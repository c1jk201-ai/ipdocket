import uuid
from datetime import date
from pathlib import Path

from flask import current_app, jsonify, render_template, request, send_file, send_from_directory
from flask_login import current_user, login_required
from sqlalchemy import or_

from app.blueprints.document import bp
from app.extensions import db
from app.models.case import Case
from app.models.document import Document, DocumentVersion, Folder
from app.models.letter import Letter
from app.models.ip_records import Matter
from app.services.audit.entity_audit import record_entity_change_audit, snapshot_attrs
from app.services.storage.file_asset_service import FileAssetService, UploadTooLargeError
from app.utils.api_errors import json_error
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import is_admin, is_manager

_LETTER_AUDIT_FIELDS = (
    "id",
    "direction",
    "case_id",
    "title",
    "correspondent",
    "method",
    "date_sent_received",
    "tracking_no",
    "status",
    "file_path",
    "created_by_id",
    "created_at",
)


def _letter_audit_snapshot(letter: Letter) -> dict[str, object]:
    return snapshot_attrs(letter, _LETTER_AUDIT_FIELDS)


def _can_access_folder(folder: Folder) -> bool:
    if not folder:
        return False
    if folder.is_team:
        return True
    return folder.owner_id == current_user.id


@bp.route("/")
@login_required
def index():
    view = request.args.get("view", "public")
    return render_template("document/index.html", page=view)


# --- Letter APIs ---


@bp.route("/api/letters", methods=["GET", "POST"])
@login_required
def api_letters():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw_case_id = data.get("case_id")
        if raw_case_id in ("", None):
            case_id = None
        else:
            try:
                case_id = int(raw_case_id)
            except (TypeError, ValueError):
                return jsonify({"error": "invalid case_id"}), 400
        if case_id is not None:
            case = db.session.get(Case, case_id)
            if not case:
                return jsonify({"error": "case not found"}), 400
            if not (is_admin(current_user) or is_manager(current_user)):
                has_owner = bool(case.manager_id or case.attorney_id)
                if (
                    has_owner
                    and case.manager_id != current_user.id
                    and case.attorney_id != current_user.id
                ):
                    return jsonify({"error": "forbidden"}), 403

        raw_date = (data.get("date") or "").strip()
        if raw_date:
            try:
                parsed_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                return jsonify({"error": "invalid date"}), 400
        else:
            parsed_date = date.today()
        l = Letter(
            direction=data.get("direction") or "out",
            case_id=case_id,
            title=data.get("title") or "No Title",
            correspondent=data.get("correspondent"),
            method=data.get("method"),
            date_sent_received=parsed_date,
            tracking_no=data.get("tracking_no"),
            status=data.get("status") or "sent",
            created_by_id=current_user.id,
        )
        db.session.add(l)
        db.session.flush()
        record_entity_change_audit(
            action="document.letter.create",
            target_type="letter",
            target_id=l.id,
            actor_id=getattr(current_user, "id", None),
            after=_letter_audit_snapshot(l),
            meta={"letter_id": l.id, "case_id": case_id, "source": "document.api_letters"},
            title=l.title,
            include_snapshots=True,
        )
        db.session.commit()
        return jsonify({"id": l.id}), 201

    # GET list
    q = db.session.query(Letter, Case).outerjoin(Case, Letter.case_id == Case.id)
    if not is_admin(current_user):
        q = q.filter(Letter.created_by_id == current_user.id)
    rows = q.order_by(Letter.date_sent_received.desc()).limit(100).all()
    case_refs = {r.Case.ref_no for r in rows if r.Case and r.Case.ref_no}
    matter_map = {}
    if case_refs:
        matters = Matter.query.filter(
            or_(
                Matter.our_ref.in_(case_refs),
                Matter.old_our_ref.in_(case_refs),
                Matter.your_ref.in_(case_refs),
            )
        ).all()
        for m in matters:
            for ref in (m.our_ref, m.old_our_ref, m.your_ref):
                if ref and ref in case_refs and ref not in matter_map:
                    matter_map[ref] = m.matter_id
    return jsonify(
        [
            {
                "id": r.Letter.id,
                "direction": r.Letter.direction,
                "case_ref": r.Case.ref_no if r.Case else "",
                "matter_id": matter_map.get(r.Case.ref_no) if r.Case else None,
                "title": r.Letter.title,
                "correspondent": r.Letter.correspondent,
                "method": r.Letter.method,
                "date": (
                    r.Letter.date_sent_received.isoformat() if r.Letter.date_sent_received else None
                ),
                "tracking_no": r.Letter.tracking_no,
                "status": r.Letter.status,
            }
            for r in rows
        ]
    )


@bp.route("/api/letters/<int:lid>", methods=["DELETE"])
@login_required
def api_letter_detail(lid: int):
    l = db.session.get(Letter, lid)
    if not l:
        return jsonify({"error": "not found"}), 404
    if not is_admin(current_user) and l.created_by_id != current_user.id:
        return jsonify({"error": "forbidden"}), 403
    before = _letter_audit_snapshot(l)
    record_entity_change_audit(
        action="document.letter.delete",
        target_type="letter",
        target_id=l.id,
        actor_id=getattr(current_user, "id", None),
        before=before,
        meta={"letter_id": l.id, "case_id": l.case_id, "source": "document.api_letter_detail"},
        title=l.title,
        include_snapshots=True,
    )
    db.session.delete(l)
    db.session.commit()
    return jsonify({"success": True})


# ---- Minimal DMS JSON APIs ----


@bp.route("/api/folders", methods=["GET", "POST"])
@login_required
def api_folders():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name required"}), 400
        scope = (data.get("scope") or "public").lower()
        is_team = scope != "private"
        f = Folder(
            name=name,
            parent_id=data.get("parent_id"),
            is_team=is_team,
            owner_id=None if is_team else current_user.id,
        )
        db.session.add(f)
        db.session.commit()
        return jsonify({"id": f.id}), 201
    scope = (request.args.get("scope") or "public").lower()
    q = Folder.query
    if scope == "private":
        q = q.filter_by(is_team=False, owner_id=current_user.id)
    else:
        q = q.filter_by(is_team=True)
    rows = q.order_by(Folder.id.desc()).limit(500).all()
    return jsonify(
        [
            {"id": r.id, "name": r.name, "parent_id": r.parent_id, "is_team": bool(r.is_team)}
            for r in rows
        ]
    )


@bp.route("/api/docs", methods=["GET"])
@login_required
def api_docs():
    folder_id = request.args.get("folder_id", type=int)
    if not folder_id:
        return jsonify([])
    folder = db.session.get(Folder, folder_id)
    if not folder:
        return jsonify({"error": "not found"}), 404
    if not _can_access_folder(folder):
        return jsonify({"error": "forbidden"}), 403
    docs = (
        Document.query.filter_by(folder_id=folder_id).order_by(Document.id.desc()).limit(500).all()
    )
    out = []
    for d in docs:
        ver = None
        if d.current_version_id:
            ver = db.session.get(DocumentVersion, d.current_version_id)
        out.append(
            {
                "id": d.id,
                "title": d.title,
                "current_version_id": d.current_version_id,
                "size": (ver.size if ver else 0),
            }
        )
    return jsonify(out)


@bp.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    file = request.files.get("file")
    folder_id = request.form.get("folder_id", type=int)
    title = request.form.get("title") or (getattr(file, "filename", None) or "")
    if not (file and folder_id and title):
        return json_error("bad_request", "file, folder_id required", status=400)
    folder = db.session.get(Folder, folder_id)
    if not folder:
        return json_error("not_found", "folder not found", status=404)
    if not _can_access_folder(folder):
        return json_error("forbidden", "forbidden", status=403)

    orig = (file.filename or "").strip() or "file.bin"
    ext = Path(orig).suffix.lower()
    #   Extend/ 
    if (not ext) or (len(ext) > 10):
        ext = ".bin"
    save_name = f"{uuid.uuid4().hex}{ext}"
    rel_path = Path("docs") / save_name
    service = FileAssetService()
    stored = None
    try:
        stored = service.store_upload_to_path(file, rel_path=rel_path, overwrite=True)
        with db.session.begin():
            doc = Document(folder_id=folder_id, title=title)
            db.session.add(doc)
            db.session.flush()
            ver = DocumentVersion(
                document_id=doc.id,
                version_no=1,
                file_path=stored.rel_path,
                size=stored.byte_size,
                checksum=stored.sha256,
                uploaded_by=current_user.id,
            )
            db.session.add(ver)
            db.session.flush()
            doc.current_version_id = ver.id
        return jsonify({"id": doc.id, "version_id": ver.id})
    except UploadTooLargeError as exc:
        if stored:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as cleanup_exc:
                # Best-effort cleanup after rejected uploads.
                report_swallowed_exception(
                    cleanup_exc,
                    context="document.routes.api_upload.cleanup_delete_file",
                    log_key="document.routes.api_upload.cleanup_delete_file",
                    log_window_seconds=300,
                )
        return json_error("file_too_large", str(exc) or "file too large", status=413)
    except ValueError as exc:
        current_app.logger.warning("Document upload rejected: %s", exc)
        if stored:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as cleanup_exc:
                # Best-effort cleanup after rejected uploads.
                report_swallowed_exception(
                    cleanup_exc,
                    context="document.routes.api_upload.cleanup_delete_file",
                    log_key="document.routes.api_upload.cleanup_delete_file",
                    log_window_seconds=300,
                )
        return json_error("invalid_path", "invalid path", status=400)
    except Exception as exc:
        report_swallowed_exception(exc, context="document.api_upload")
        try:
            db.session.rollback()
        except Exception as rollback_exc:
            report_swallowed_exception(
                rollback_exc,
                context="document.routes.api_upload.rollback",
                log_key="document.routes.api_upload.rollback",
                log_window_seconds=300,
            )
        if stored:
            try:
                service.delete_physical_file(stored.rel_path)
            except Exception as cleanup_exc:
                # Best-effort cleanup after failed uploads.
                report_swallowed_exception(
                    cleanup_exc,
                    context="document.routes.api_upload.cleanup_delete_file",
                    log_key="document.routes.api_upload.cleanup_delete_file",
                    log_window_seconds=300,
                )
        return json_error("upload_failed", "upload failed", status=500)


@bp.route("/download/<int:version_id>")
@login_required
def download(version_id: int):
    ver = db.session.get(DocumentVersion, version_id)
    if not ver:
        return json_error("not_found", "not found", status=404)
    doc = ver.document or db.session.get(Document, ver.document_id)
    folder = doc.folder if doc else None
    if not doc or not folder:
        return json_error("not_found", "not found", status=404)
    if not _can_access_folder(folder):
        return json_error("forbidden", "forbidden", status=403)

    service = FileAssetService()
    rel_path = ""
    try:
        rel_path = service.normalize_rel_path(ver.file_path)
    except Exception:
        current_app.logger.warning(
            "Document download blocked due to invalid path (version_id=%s)", version_id
        )
        return json_error("not_found", "not found", status=404)

    # 1. Try Local First (Legacy & Local Mode)
    # We check local existence regardless of storage_type to support migration transition
    try:
        abs_path = service.abs_path(rel_path)
        if abs_path.exists():
            return send_from_directory(str(service.upload_root), rel_path, as_attachment=True)
    except Exception as exc:
        current_app.logger.debug(
            "Local document open failed (version_id=%s): %s",
            version_id,
            exc,
            exc_info=True,
        )

    # 2. Try S3 if configured
    if (current_app.config.get("STORAGE_TYPE") or "local").lower() == "s3":
        try:
            stream = service.default_backend.open(rel_path)
            # Determine filename
            filename = "document"
            if doc and doc.title:
                filename = doc.title
                # Ensure extension matches
                if ver.file_path and Path(ver.file_path).suffix:
                    ext = Path(ver.file_path).suffix
                    if not filename.endswith(ext):
                        filename += ext

            return send_file(stream, as_attachment=True, download_name=filename)
        except Exception as e:
            current_app.logger.warning(
                f"Document S3 download failed (version_id={version_id}): {e}"
            )

    return json_error("file_missing", "file missing", status=404)
