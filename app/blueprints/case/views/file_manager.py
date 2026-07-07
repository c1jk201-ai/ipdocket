"""File manager routes.

Handles file manager operations: upload, folder creation, move, delete.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import FileAsset, Matter, MatterFileAsset
from app.services.case.case_audit_service import record_case_audit
from app.services.files.file_classification import classify_doc_type, is_previewable
from app.services.storage.file_asset_service import get_file_asset_service
from app.utils.permissions import require_matter_access
from app.utils.policy_sql import policy_text as text
from config import Config


def _normalize_parent_id(raw_value: str | None) -> str | None:
    parent_id = (raw_value or "").strip()
    if not parent_id or parent_id == "None":
        return None
    return parent_id


def _is_valid_parent_folder(*, case_id: str, parent_id: str | None) -> bool:
    if not parent_id:
        return True
    exists = db.session.execute(
        text(
            """
            SELECT 1
            FROM matter_file_asset
            WHERE matter_file_id = :pid
              AND matter_id = :mid
              AND role = 'folder'
            """
        ),
        {"pid": parent_id, "mid": str(case_id)},
    ).scalar()
    return bool(exists)


def _would_create_cycle(*, case_id: str, item_id: str, new_parent_id: str) -> bool:
    """Return True if moving item_id under new_parent_id would create a cycle."""
    current = new_parent_id
    visited: set[str] = set()
    while current:
        if current == item_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        current = db.session.execute(
            text(
                """
                SELECT parent_id
                FROM matter_file_asset
                WHERE matter_file_id = :id AND matter_id = :mid
                """
            ),
            {"id": current, "mid": str(case_id)},
        ).scalar()
    return False


@bp.route("/<case_id>/fm/upload", methods=["POST"])
@login_required
def upload_fm_file(case_id):
    """Upload a file to the file manager."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    file = request.files.get("file")
    category = request.form.get("category", "internal")  # internal or submission
    parent_id = _normalize_parent_id(request.form.get("parent_id"))

    redirect_url = (
        url_for("case_work.case_detail", case_id=case_id, fm_folder_id=parent_id) + "#sec-files"
    )

    if not file:
        flash("No file selected", "warning")
        return redirect(redirect_url)

    if not _is_valid_parent_folder(case_id=case_id, parent_id=parent_id):
        flash("target Folder   none.", "danger")
        return redirect(redirect_url)

    try:
        file_service = get_file_asset_service()

        # Use FileAssetService for consistent handling
        now = datetime.now()
        ym_path = now.strftime("%Y/%m")
        subdir = str(Path("fm") / ym_path)

        staged = file_service.stage_upload(file, subdir=subdir)

        doc_type, tags = classify_doc_type(staged.original_name)
        previewable = is_previewable(staged.original_name, staged.mime_type)

        # Create MatterFileAsset link
        mfa = MatterFileAsset(
            matter_file_id=uuid.uuid4().hex,
            matter_id=str(case_id),
            file_asset_id=staged.file_asset_id,
            role=category,
            parent_id=parent_id,
            doc_type=doc_type,
            tags=tags,
            previewable=previewable,
            created_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        db.session.add(mfa)

        # ✅ Audit (UPLOAD)
        record_case_audit(
            case_id=str(case_id),
            action="UPLOAD",
            field_name="fm.upload",
            actor_user_id=getattr(current_user, "id", None),
            old_value=None,
            new_value={
                "file_asset_id": staged.file_asset_id,
                "filename": staged.original_name,
                "category": category,
                "doc_type": doc_type,
                "parent_id": parent_id,
            },
        )
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = MatterFileAsset.query.filter_by(
                matter_id=str(case_id),
                file_asset_id=staged.file_asset_id,
                role=category,
            ).first()
            if not existing:
                raise

            if parent_id and (existing.parent_id or "") != parent_id:
                existing.parent_id = parent_id

            try:
                existing_tags = existing.tags if isinstance(existing.tags, list) else []
            except Exception:
                existing_tags = []
            if "MANUAL" not in existing_tags:
                existing.doc_type = doc_type
                existing.tags = tags
            existing.previewable = previewable

            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise

        flash("File uploaded successfully", "success")
    except ValueError as e:
        db.session.rollback()
        if "too big" in str(e) or " " in str(e):
            return "File too large", 413
        flash(str(e), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("FM upload failed (case_id=%s)", case_id)
        flash("Upload Failed.   Retry .", "danger")

    return redirect(redirect_url)


@bp.route("/<case_id>/fm/folder", methods=["POST"])
@login_required
def create_fm_folder(case_id):
    """Create a new folder in the file manager."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    folder_name = (request.form.get("folder_name") or "").strip()
    parent_id = _normalize_parent_id(request.form.get("parent_id"))

    redirect_url = (
        url_for("case_work.case_detail", case_id=case_id, fm_folder_id=parent_id) + "#sec-files"
    )

    if not folder_name:
        flash("Folder Name Input.", "warning")
        return redirect(redirect_url)

    if not _is_valid_parent_folder(case_id=case_id, parent_id=parent_id):
        flash("target Folder   none.", "danger")
        return redirect(redirect_url)

    try:
        folder_id = uuid.uuid4().hex
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Create a virtual folder entry (no file_asset, just matter_file_asset with role='folder')
        db.session.execute(
            text(
                """
                INSERT INTO matter_file_asset(matter_file_id, matter_id, role, description, parent_id, created_at)
                VALUES(:mfid, :mid, 'folder', :name, :parent, :created)
            """
            ),
            {
                "mfid": folder_id,
                "mid": str(case_id),
                "name": folder_name,
                "parent": parent_id,
                "created": now,
            },
        )
        db.session.commit()

        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="fm.folder.create",
            actor_user_id=getattr(current_user, "id", None),
            old_value=None,
            new_value={"folder_id": folder_id, "folder_name": folder_name, "parent_id": parent_id},
        )
        db.session.commit()

        flash(f"Folder '{folder_name}' Create.", "success")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("FM folder creation failed (case_id=%s)", case_id)
        flash("Folder Create Failed.   Retry .", "danger")

    return redirect(redirect_url)


@bp.route("/<case_id>/fm/move", methods=["POST"])
@login_required
def move_fm_item(case_id):
    """Move a file or folder to a different parent."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    item_id = (request.form.get("item_id") or request.form.get("matter_file_id") or "").strip()
    target_role = (request.form.get("target_role") or "").strip().lower()
    new_parent_id = _normalize_parent_id(request.form.get("new_parent_id"))
    current_folder_id = _normalize_parent_id(request.form.get("current_folder_id"))
    redirect_url = (
        url_for(
            "case_work.case_detail",
            case_id=case_id,
            fm_folder_id=(current_folder_id if target_role else new_parent_id),
        )
        + "#sec-files"
    )

    if not item_id:
        flash("Go Item Select.", "warning")
        return redirect(redirect_url)

    if target_role and target_role not in {"internal", "submission"}:
        flash("target   .", "warning")
        return redirect(redirect_url)

    try:
        # Verify item belongs to matter
        row = db.session.execute(
            text(
                """
                SELECT role, parent_id
                FROM matter_file_asset
                WHERE matter_file_id = :id AND matter_id = :mid
                """
            ),
            {"id": item_id, "mid": case_id},
        ).fetchone()

        if not row:
            flash("Item   none.", "danger")
            return redirect(redirect_url)
        old_role, old_parent_id = row

        # Role switch (internal <-> submission), used by the two-column board and drag/drop.
        if target_role:
            if old_role == target_role:
                flash("   exists.", "info")
                return redirect(
                    url_for(
                        "case_work.case_detail",
                        case_id=case_id,
                        fm_folder_id=(current_folder_id or old_parent_id),
                    )
                    + "#sec-files"
                )

            db.session.execute(
                text("UPDATE matter_file_asset SET role = :role WHERE matter_file_id = :id"),
                {"role": target_role, "id": item_id},
            )

            record_case_audit(
                case_id=str(case_id),
                action="USER",
                field_name="fm.move",
                actor_user_id=getattr(current_user, "id", None),
                old_value={"item_id": item_id, "role": old_role, "parent_id": old_parent_id},
                new_value={"item_id": item_id, "role": target_role, "parent_id": old_parent_id},
            )
            db.session.commit()
            flash("item Go.", "success")
            return redirect(
                url_for(
                    "case_work.case_detail",
                    case_id=case_id,
                    fm_folder_id=(current_folder_id or old_parent_id),
                )
                + "#sec-files"
            )

        if new_parent_id and new_parent_id == item_id:
            flash(" Folder Go  none.", "warning")
            return redirect(redirect_url)

        if new_parent_id:
            if not _is_valid_parent_folder(case_id=case_id, parent_id=new_parent_id):
                flash("target Folder   none.", "danger")
                return redirect(redirect_url)
            if old_role == "folder" and _would_create_cycle(
                case_id=case_id, item_id=item_id, new_parent_id=new_parent_id
            ):
                flash(" Folder Go  none.", "warning")
                return redirect(redirect_url)

        # Update parent
        db.session.execute(
            text("UPDATE matter_file_asset SET parent_id = :parent WHERE matter_file_id = :id"),
            {"parent": new_parent_id, "id": item_id},
        )

        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="fm.move",
            actor_user_id=getattr(current_user, "id", None),
            old_value={"item_id": item_id, "role": old_role, "parent_id": old_parent_id},
            new_value={"item_id": item_id, "role": old_role, "parent_id": new_parent_id},
        )
        db.session.commit()

        flash("item Go.", "success")

    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "FM move failed (case_id=%s, item_id=%s, target_role=%s)",
            case_id,
            item_id,
            target_role or "",
        )
        flash("Go Failed.   Retry .", "danger")

    return redirect(redirect_url)


@bp.route("/<case_id>/fm/delete", methods=["POST"])
@login_required
def delete_fm_item(case_id):
    """Delete a file or folder from the file manager."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    item_id = (request.form.get("item_id") or request.form.get("matter_file_id") or "").strip()
    parent_id = _normalize_parent_id(
        request.form.get("parent_id") or request.form.get("current_folder_id")
    )

    redirect_url = (
        url_for("case_work.case_detail", case_id=case_id, fm_folder_id=parent_id) + "#sec-files"
    )

    if not item_id:
        flash("Delete Item Select.", "warning")
        return redirect(redirect_url)

    try:
        # Verify item belongs to matter
        row = db.session.execute(
            text(
                "SELECT role, file_asset_id, parent_id FROM matter_file_asset WHERE matter_file_id = :id AND matter_id = :mid"
            ),
            {"id": item_id, "mid": case_id},
        ).fetchone()

        if not row:
            flash("Item   none.", "danger")
            return redirect(redirect_url)

        role, file_asset_id, old_parent_id = row

        # If folder, check for children
        if role == "folder":
            children = db.session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM matter_file_asset
                    WHERE parent_id = :pid AND matter_id = :mid
                    """
                ),
                {"pid": item_id, "mid": case_id},
            ).scalar()

            if children and children > 0:
                flash(
                    "Folder  File  Delete  none.  File Delete.", "warning"
                )
                return redirect(redirect_url)

        # Delete the matter_file_asset link
        db.session.execute(
            text("DELETE FROM matter_file_asset WHERE matter_file_id = :id"),
            {"id": item_id},
        )

        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="fm.delete",
            actor_user_id=getattr(current_user, "id", None),
            old_value={
                "item_id": item_id,
                "role": role,
                "file_asset_id": file_asset_id,
                "parent_id": old_parent_id,
            },
            new_value={"deleted": True},
        )

        # Note: We don't delete the file_asset itself as it might be linked elsewhere
        # Physical file cleanup can be a separate maintenance job

        db.session.commit()
        flash("item Delete.", "success")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("FM delete failed (case_id=%s, item_id=%s)", case_id, item_id)
        flash("Delete Failed.   Retry .", "danger")

    return redirect(redirect_url)
