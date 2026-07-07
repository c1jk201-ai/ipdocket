"""Apply extracted parameter confirmation results.

This endpoint is shared by multiple upload flows that render
`case/upload_confirm_params.html`.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from werkzeug.exceptions import HTTPException

from app.blueprints.case import bp
from app.blueprints.case.views._common import validate_csrf_or_redirect
from app.extensions import db
from app.models.ip_records import FileAsset, Matter, MatterStaffAssignment
from app.models.user import User
from app.services.case.canonical_field_service import upsert_case_flat_index
from app.services.parameter_conflict.parameter_conflict_resolver import ParameterConflictResolver
from app.services.parameter_conflict.parameter_conflict_types import ConflictItem
from app.services.storage.file_asset_access import filter_accessible_file_assets
from app.utils.search import compact_search_text as to_compact_compact
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import require_matter_access
from app.utils.policy_sql import policy_text as text


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_staff_role_code(value: str | None) -> str | None:
    clean = (value or "").strip().lower()
    return clean or None


_ALLOWED_TABLES = {
    "matter",
    "matter_custom_field",
    "matter_identifier",
    "matter_event",
    "matter_party_role",
    "matter_staff_assignment",
}
_FIELD_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")


def _resolve_conflict_meta(
    *,
    field_name: str,
    table_name: str | None,
    field_key: str | None,
) -> tuple[str, str, str, int, bool] | None:
    if not field_name:
        return None

    defs = ParameterConflictResolver._load_conflict_definitions()
    if field_name in (defs or {}):
        field_def = defs.get(field_name) or {}
        table = (field_def.get("table") or "matter").strip()
        if table not in _ALLOWED_TABLES:
            return None
        return (
            table,
            field_name,
            field_def.get("label", field_name),
            int(field_def.get("priority", 2) or 2),
            bool(field_def.get("hidden")),
        )

    table = None
    if field_name.startswith("identifier_"):
        table = "matter_identifier"
    elif field_name.startswith("event_"):
        table = "matter_event"
    elif field_name.startswith("party_"):
        table = "matter_party_role"
    elif field_name.startswith("staff_"):
        table = "matter_staff_assignment"
    elif field_name.startswith("custom_"):
        table = "matter_custom_field"

    if not table:
        return None
    if table_name and table_name.strip() and table_name.strip() != table:
        return None

    key = (field_key or "").strip()
    if not key or not _FIELD_KEY_RE.match(key):
        return None

    if table == "matter":
        try:
            from app.services.parameter_conflict.parameter_conflict_updater import (
                _ALLOWED_MATTER_UPDATE_COLUMNS,
            )

            if key not in _ALLOWED_MATTER_UPDATE_COLUMNS:
                return None
        except Exception:
            return None

    return (table, key, field_name, 2, False)


def _link_file_assets_to_matter(
    *,
    matter_id: str,
    file_asset_ids: list[str],
    role: str | None,
) -> int:
    if not file_asset_ids:
        return 0

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    created = 0
    role_value = (role or "").strip() or None

    for fid in file_asset_ids:
        fid = (fid or "").strip()
        if not fid:
            continue

        result = db.session.execute(
            text(
                """
                INSERT INTO matter_file_asset(
                    matter_file_id, matter_id, file_asset_id, role, created_at
                )
                SELECT :mfid, :mid, :fid, :role, :created_at
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM matter_file_asset
                    WHERE matter_id = :mid
                      AND file_asset_id = :fid
                      AND role IS NOT DISTINCT FROM :role
                )
                """
            ),
            {
                "mfid": uuid.uuid4().hex,
                "mid": matter_id,
                "fid": fid,
                "role": role_value,
                "created_at": now_str,
            },
        )
        if result.rowcount:
            created += 1

    return created


def _build_upload_note(label: str, names: list[str]) -> str:
    clean = [n for n in (names or []) if (n or "").strip()]
    if not clean:
        return label
    if len(clean) == 1:
        return f"{label}: {clean[0]}"
    return f"{label}: {clean[0]}  {len(clean) - 1}items"


def _create_upload_history_entry(
    *,
    matter_id: str,
    file_asset_ids: list[str],
    label: str,
    owner_staff_party_id: str | None,
    comm_type: str | None = "R",
) -> int:
    if not file_asset_ids:
        return 0

    names = [
        fa.original_name
        for fa in FileAsset.query.filter(FileAsset.file_asset_id.in_(file_asset_ids)).all()
        if fa.original_name
    ]
    note = _build_upload_note(label, names)
    comm_id = uuid.uuid4().hex
    today = date.today().isoformat()

    normalized_comm_type = (comm_type or "R").strip().upper() or "R"
    if normalized_comm_type not in ("M", "R"):
        normalized_comm_type = "R"

    db.session.execute(
        text(
            """
            INSERT INTO communication(
                comm_id, matter_id, comm_type,
                received_date, note, search_compact,
                owner_staff_party_id, author_staff_party_id
            )
            VALUES(
                :comm_id, :matter_id, :comm_type,
                :received_date, :note, :search_compact,
                :owner_staff_party_id, :author_staff_party_id
            )
            """
        ),
        {
            "comm_id": comm_id,
            "matter_id": matter_id,
            "comm_type": normalized_comm_type,
            "received_date": today,
            "note": note,
            "search_compact": to_compact_compact(note) if (note or "").strip() else None,
            "owner_staff_party_id": owner_staff_party_id,
            "author_staff_party_id": owner_staff_party_id,
        },
    )

    for fid in file_asset_ids:
        db.session.execute(
            text(
                """
                INSERT INTO communication_file_asset(comm_file_id, comm_id, file_asset_id, role, description)
                VALUES(:comm_file_id, :comm_id, :file_asset_id, 'upload', :desc)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "comm_file_id": uuid.uuid4().hex,
                "comm_id": comm_id,
                "file_asset_id": fid,
                "desc": label,
            },
        )

    return 1


def _assign_staff(
    matter_id: str, attorney_user_id: str | None, manager_user_id: str | None
) -> None:
    """Assign staff to matter based on user selection."""
    if not (attorney_user_id or manager_user_id):
        return

    def _upsert(user_id, role_code):
        role_code = _normalize_staff_role_code(role_code)
        if not role_code:
            return
        if not user_id:
            return
        user_obj = User.query.get(user_id)
        if not (user_obj and user_obj.staff_party_id):
            return

        # Check existing assignments for this role
        # We want to AVOID duplicates but maybe allow multiple attorneysNew
        # For "Responsible Attorney" dropdown, usually we imply setting the MAIN one.
        # But here we just assume add-if-not-exists to be safe against deleting data.
        exists = (
            db.session.query(MatterStaffAssignment.msa_id)
            .filter(
                MatterStaffAssignment.matter_id == matter_id,
                MatterStaffAssignment.staff_party_id == user_obj.staff_party_id,
                func.lower(func.trim(MatterStaffAssignment.staff_role_code)) == role_code,
            )
            .first()
        )

        if not exists:
            db.session.execute(
                text(
                    """
                    INSERT INTO matter_staff_assignment(
                        msa_id, matter_id, staff_party_id, staff_role_code
                    )
                    VALUES (:msa_id, :mid, :pid, :role)
                    """
                ),
                {
                    "msa_id": uuid.uuid4().hex,
                    "mid": matter_id,
                    "pid": user_obj.staff_party_id,
                    "role": role_code,
                },
            )

    _upsert(attorney_user_id, "attorney")
    _upsert(manager_user_id, "manager")

    # Refresh index immediately so automation sees the new staff
    try:
        upsert_case_flat_index(matter_id)
    except Exception as exc:
        # Best-effort: index refresh should not block staff assignment flow.
        report_swallowed_exception(
            exc,
            context="case.extracted_params.refresh_case_flat_index",
            log_key="case.extracted_params.refresh_case_flat_index",
            log_window_seconds=300,
        )


@bp.route("/apply_extracted_params", methods=["POST"])
@login_required
def apply_extracted_params():
    """Apply user-confirmed extracted parameters to the target matter."""
    matter_id = (request.form.get("matter_id") or "").strip()
    popup = request.form.get("popup")
    file_asset_ids: list[str] = []
    back_url = (
        url_for("case_work.case_detail", case_id=matter_id)
        if matter_id
        else url_for("case_work.case_list")
    )

    redirect_endpoint = "case_work.case_detail" if matter_id else "case_work.case_list"
    redirect_kwargs = {"case_id": matter_id} if matter_id else {}
    csrf_error = validate_csrf_or_redirect(
        request.form.get("csrf_token"),
        redirect_endpoint,
        **redirect_kwargs,
    )
    if csrf_error:
        return csrf_error

    if not matter_id:
        flash("Matter ID none.", "danger")
        return redirect(url_for("case_work.case_list"))

    Matter.query.get_or_404(matter_id)
    require_matter_access(matter_id, action="edit_case")

    # 1) Build ConflictItem lists from posted form
    auto_apply: list[ConflictItem] = []
    user_selections: dict[str, str] = {}
    conflicts: list[ConflictItem] = []
    invalid_fields: list[str] = []

    # Auto-apply checkboxes
    for k, v in request.form.items():
        if not k.startswith("auto_"):
            continue
        if k.startswith("auto_field_") or k.startswith("auto_table_") or k.startswith("auto_key_"):
            continue
        if v != "1":
            continue

        field_name = k[len("auto_") :]
        new_value = request.form.get(f"auto_field_{field_name}")
        table_name = request.form.get(f"auto_table_{field_name}")
        field_key = request.form.get(f"auto_key_{field_name}")
        resolved = _resolve_conflict_meta(
            field_name=field_name,
            table_name=table_name,
            field_key=field_key,
        )
        if not resolved:
            invalid_fields.append(field_name)
            continue
        resolved_table, resolved_key, label, priority, hidden = resolved

        auto_apply.append(
            ConflictItem(
                field_name=field_name,
                field_label=label,
                current_value=None,
                new_value=new_value,
                table_name=resolved_table,
                field_key=resolved_key,
                priority=priority,
                hidden=hidden,
            )
        )

    # Conflicts (radio current/new)
    for k, v in request.form.items():
        if not k.startswith("conflict_"):
            continue
        field_name = k[len("conflict_") :]
        if not field_name:
            continue

        choice = (v or "").strip().lower()
        if choice not in {"current", "new"}:
            choice = "current"
        user_selections[field_name] = choice

        new_value = request.form.get(f"new_value_{field_name}")
        table_name = request.form.get(f"new_table_{field_name}")
        field_key = request.form.get(f"new_key_{field_name}")
        resolved = _resolve_conflict_meta(
            field_name=field_name,
            table_name=table_name,
            field_key=field_key,
        )
        if not resolved:
            invalid_fields.append(field_name)
            continue
        resolved_table, resolved_key, label, priority, hidden = resolved

        conflicts.append(
            ConflictItem(
                field_name=field_name,
                field_label=label,
                current_value=None,
                new_value=new_value,
                table_name=resolved_table,
                field_key=resolved_key,
                priority=priority,
                hidden=hidden,
            )
        )

    if invalid_fields:
        current_app.logger.warning(
            "Rejected apply_extracted_params fields (matter_id=%s): %s",
            matter_id,
            ", ".join(sorted(set(invalid_fields))),
        )
        abort(400, " .")

    resolver = ParameterConflictResolver(matter_id)
    stage = "apply_params"
    try:
        # 2) Apply parameter updates
        resolver.apply_parameters(
            auto_apply=auto_apply,
            user_selections=user_selections,
            conflicts=conflicts,
        )

        # 3) Optionally link uploaded file assets to the matter
        stage = "link_files"
        role = (request.form.get("file_role") or "").strip().lower()
        file_asset_ids = request.form.getlist("staged_file_ids")
        if not file_asset_ids:
            single = (request.form.get("staged_file_id") or "").strip()
            if single:
                file_asset_ids = [single]

        if file_asset_ids:
            # Keep stable ordering from the form while removing duplicates/empties.
            seen: set[str] = set()
            deduped: list[str] = []
            for fid in file_asset_ids:
                clean = str(fid or "").strip()
                if not clean or clean in seen:
                    continue
                seen.add(clean)
                deduped.append(clean)
            file_asset_ids = deduped

            try:
                file_asset_ids = filter_accessible_file_assets(
                    file_asset_ids,
                    user=current_user,
                )
            except PermissionError:
                current_app.logger.warning(
                    "Denied file_asset mapping (matter_id=%s, file_asset_ids=%s)",
                    matter_id,
                    file_asset_ids,
                )
                abort(403, "You do not have permission to apply extracted values.")
            except ValueError:
                current_app.logger.warning(
                    "Invalid file_asset mapping request (matter_id=%s, file_asset_ids=%s)",
                    matter_id,
                    file_asset_ids,
                )
                abort(400, " File .")

            _link_file_assets_to_matter(
                matter_id=matter_id,
                file_asset_ids=file_asset_ids,
                role=role,
            )
            if role in ("application", "response"):
                owner_staff_party_id = None
                try:
                    owner_staff_party_id = (
                        str(current_user.staff_party_id)
                        if getattr(current_user, "staff_party_id", None)
                        else None
                    )
                except Exception:
                    owner_staff_party_id = None

                doc_label = (request.form.get("doc_type") or "").strip()
                if role == "application":
                    label = doc_label or "Filing Upload"
                else:
                    label = doc_label or " Upload"

                _create_upload_history_entry(
                    matter_id=matter_id,
                    file_asset_ids=file_asset_ids,
                    label=label,
                    owner_staff_party_id=owner_staff_party_id,
                    comm_type="R",
                )

        db.session.commit()
    except HTTPException:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to apply extracted params at %s (matter_id=%s)",
            stage,
            matter_id,
        )
        flash("Apply In Progress Error .   Retry .", "danger")
        return redirect(back_url)

    flash("Select item Apply.", "success")
    if popup:
        from flask import render_template

        return render_template(
            "case/popup_done.html",
            title="File   Apply.",
            back_url=back_url,
            popup=True,
        )
    return redirect(back_url)
