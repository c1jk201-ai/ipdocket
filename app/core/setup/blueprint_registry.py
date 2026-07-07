from __future__ import annotations

from flask import Flask, abort, send_file, send_from_directory
from flask_login import current_user, login_required

from app.core.setup.logging_setup import _log_swallowed
from app.extensions import db
from app.utils.mime_headers import normalize_uploaded_filename
from app.utils.policy_sql import policy_text as text


def register_blueprints(app: Flask) -> None:
    # Register Blueprints
    from app.blueprints.main import bp as main_bp

    app.register_blueprint(main_bp)

    from app.blueprints.auth import bp as auth_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")

    from app.blueprints.case import bp as case_bp

    app.register_blueprint(case_bp, url_prefix="/case")

    from app.blueprints.doc import bp as doc_bp

    app.register_blueprint(doc_bp, url_prefix="/doc")

    from app.blueprints.workflow import bp as workflow_bp

    app.register_blueprint(workflow_bp, url_prefix="/workflow")

    from app.blueprints.api import bp as api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    # New: CRM (clients) and Accounting
    from app.blueprints.crm import bp as crm_bp

    app.register_blueprint(crm_bp, url_prefix="/crm")

    from app.blueprints.accounting.routes import bp as accounting_bp

    app.register_blueprint(accounting_bp, url_prefix="/accounting")

    from app.blueprints.billing_invoices import bp as billing_invoices_bp

    app.register_blueprint(billing_invoices_bp, url_prefix="/accounting/invoice-system")

    # New system modules
    from app.blueprints.document import bp as document_bp

    app.register_blueprint(document_bp, url_prefix="/document")

    from app.blueprints.statistics import bp as statistics_bp

    app.register_blueprint(statistics_bp, url_prefix="/statistics")

    from app.blueprints.deadline import bp as deadline_bp

    app.register_blueprint(deadline_bp, url_prefix="/deadline")

    from app.blueprints.renewal import bp as renewal_bp

    app.register_blueprint(renewal_bp, url_prefix="/renewal")

    # /Operations info modules
    from app.blueprints.dashboard import bp as dashboard_bp

    app.register_blueprint(dashboard_bp, url_prefix="/business/dashboard")

    from app.blueprints.business import bp as business_bp

    app.register_blueprint(business_bp, url_prefix="/business")

    from app.blueprints.mgmt_info import bp as mgmt_info_bp

    app.register_blueprint(mgmt_info_bp, url_prefix="/mgmt")

    from app.blueprints.settings import bp as settings_bp

    app.register_blueprint(settings_bp, url_prefix="/settings")

    from app.blueprints.help import bp as help_bp

    app.register_blueprint(help_bp, url_prefix="/help")

    from app.blueprints.admin import bp as admin_bp

    app.register_blueprint(admin_bp, url_prefix="/admin")

    try:
        from app.blueprints.admin.security_health import bp as admin_security_health_bp

        app.register_blueprint(admin_security_health_bp)
    except Exception:
        app.logger.exception("Failed to register admin_security_health blueprint")

    # Ops Admin
    from app.ops.admin import ops_admin_bp

    app.register_blueprint(ops_admin_bp)

    from app.blueprints.worklog import bp as worklog_bp

    app.register_blueprint(worklog_bp, url_prefix="/worklog")

    # ------------------------------------------------------------------
    # Productivity (P1): task/notice, document deadlines, Ctrl+K search, undo.
    # NOTE: Existing try-guarded blueprint registration.
    # ------------------------------------------------------------------
    try:
        from app.blueprints.productivity.routes import bp as productivity_bp

        app.register_blueprint(productivity_bp)
    except Exception as e:  # pragma: no cover
        try:
            app.logger.warning("productivity blueprint not loaded: %s", e)
        except Exception as exc:
            _log_swallowed("register_blueprints.productivity", exc)

    # Saved views (user-level list filters)
    try:
        from app.blueprints.views import bp as views_bp

        app.register_blueprint(views_bp)
    except Exception as e:  # pragma: no cover
        try:
            app.logger.warning("views blueprint not loaded: %s", e)
        except Exception as exc:
            _log_swallowed("register_blueprints.views", exc)

    register_file_routes(app)


def _linked_matter_ids_for_file_asset(file_asset_id: str) -> list[str]:
    if not (file_asset_id or "").strip():
        return []
    rows = (
        db.session.execute(
            text(
                """
            SELECT DISTINCT matter_id FROM matter_file_asset WHERE file_asset_id = :fid
              AND COALESCE(is_deleted, FALSE) = FALSE
            UNION
            SELECT DISTINCT c.matter_id
            FROM communication c
            JOIN communication_file_asset cfa ON c.comm_id = cfa.comm_id
            WHERE cfa.file_asset_id = :fid
            UNION
            SELECT DISTINCT oa.matter_id
            FROM office_action oa
            JOIN office_action_file_asset oafa ON oa.oa_id = oafa.oa_id
            WHERE oafa.file_asset_id = :fid
            """
            ),
            {"fid": str(file_asset_id)},
        )
        .scalars()
        .all()
    )
    return [str(r) for r in rows if r]


def _user_can_access_matter_ids(matter_ids: list[str]) -> bool:
    from app.utils.permissions import can_access_matter

    trimmed = [str(mid) for mid in matter_ids if mid]
    if not trimmed:
        return False
    return all(can_access_matter(current_user, mid, action="view") for mid in trimmed)


def _abort_unclean_file_asset(status: str | None) -> None:
    from app.services.storage.file_asset_scan_service import (
        SCAN_STATUS_ERROR,
        SCAN_STATUS_INFECTED,
        normalize_file_asset_scan_status,
    )

    normalized = normalize_file_asset_scan_status(status)
    if normalized == SCAN_STATUS_INFECTED:
        abort(409, " from  File.")
    if normalized == SCAN_STATUS_ERROR:
        abort(409, "File   Failed  column  none.")
    abort(409, "File    Done .")


def register_file_routes(app: Flask) -> None:
    def _download_name(value: str | None, *, fallback: str) -> str:
        normalized = normalize_uploaded_filename(value, default=fallback)
        normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1].strip()
        return normalized or fallback

    @app.route("/uploads/<path:filename>")
    @login_required
    def uploaded_file(filename):
        from app.models.assets import FileAsset
        from app.services.storage.file_asset_scan_service import file_asset_scan_allows_read
        from app.services.storage.file_asset_service import get_file_asset_service
        from app.utils.permissions import is_admin

        file_service = get_file_asset_service()
        try:
            rel_path = file_service.normalize_rel_path(filename)
        except Exception:
            abort(404)

        row = (
            db.session.query(
                FileAsset.file_asset_id,
                FileAsset.file_path,
                FileAsset.storage_type,
                FileAsset.original_name,
                FileAsset.mime_type,
                FileAsset.virus_scan_status,
            )
            .filter(
                (FileAsset.file_path == rel_path)
                | (FileAsset.file_path == f"data/uploads/{rel_path}")
            )
            .limit(1)
            .first()
        )

        if row:
            (
                file_asset_id,
                stored_path,
                storage_type,
                original_name,
                mime_type,
                virus_scan_status,
            ) = row
            matter_ids = _linked_matter_ids_for_file_asset(file_asset_id)
            # If link data is missing (orphaned file_asset), allow only admins so
            # accounting roles cannot browse unscoped uploads.
            if not matter_ids:
                if not is_admin(current_user):
                    abort(404)
            else:
                if not _user_can_access_matter_ids(matter_ids):
                    abort(403, "You do not have permission to access this file.")
            if not file_asset_scan_allows_read(virus_scan_status):
                _abort_unclean_file_asset(virus_scan_status)
            rel_path = file_service.normalize_rel_path(stored_path)
            if str(storage_type or "local").strip().lower() == "s3":
                return send_file(
                    file_service.open_stream(file_asset_id),
                    mimetype=mime_type or "application/octet-stream",
                    as_attachment=False,
                    download_name=_download_name(
                        original_name,
                        fallback=f"file_{file_asset_id}",
                    ),
                )
            return send_from_directory(str(file_service.upload_root), rel_path)

        if not is_admin(current_user):
            abort(403, "You do not have permission to access this file.")
        return send_from_directory(str(file_service.upload_root), rel_path)

    @app.route("/files/<string:file_asset_id>/preview")
    @login_required
    def preview_file_asset(file_asset_id: str):
        from app.models.assets import FileAsset
        from app.services.files.file_classification import is_previewable
        from app.services.storage.file_asset_scan_service import file_asset_scan_allows_read
        from app.services.storage.file_asset_service import get_file_asset_service
        from app.utils.permissions import is_admin

        matter_ids = _linked_matter_ids_for_file_asset(file_asset_id)
        # If orphaned, allow only admins so unscoped previews stay restricted.
        if not matter_ids:
            if not is_admin(current_user):
                abort(404)
        else:
            if not _user_can_access_matter_ids(matter_ids):
                abort(403, "You do not have permission to preview this file.")

        asset = db.session.get(FileAsset, file_asset_id)
        if not asset or bool(getattr(asset, "is_deleted", False)):
            abort(404)

        if not file_asset_scan_allows_read(asset.virus_scan_status):
            _abort_unclean_file_asset(asset.virus_scan_status)

        if not is_previewable(asset.original_name, asset.mime_type):
            abort(415, "Preview   .")

        file_service = get_file_asset_service()
        if str(asset.storage_type or "local").strip().lower() == "s3":
            return send_file(
                file_service.open_stream(file_asset_id),
                mimetype=asset.mime_type or "application/octet-stream",
                as_attachment=False,
                download_name=_download_name(
                    asset.original_name,
                    fallback=f"file_{file_asset_id}",
                ),
            )

        path = file_service.get_abs_path(file_asset_id)
        return send_from_directory(
            path.parent,
            path.name,
            mimetype=asset.mime_type or "application/octet-stream",
            as_attachment=False,
            download_name=asset.original_name or f"file_{file_asset_id}",
        )
