from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from werkzeug.utils import secure_filename

from app.blueprints.admin import bp
from app.blueprints.billing_invoices.db import get_db as get_invoice_db
from app.blueprints.billing_invoices.db import row_to_dict
from app.extensions import db
from app.models.case import Case
from app.models.codes import Code, CodeGroup
from app.models.deadline import Deadline, RenewalFee
from app.models.deletion_log import DeletionLog
from app.models.error_report import ErrorReport
from app.models.party import Party, PartyStaff
from app.models.ip_records import (
    AnnuityItem,
    DocketItem,
    Matter,
    MatterStaffAssignment,
)
from app.models.system_config import SystemConfig
from app.models.user import User
from app.models.user_access_log import UserAccessLog
from app.models.workflow import Workflow
from app.services.admin.data_quality_service import get_data_quality_metrics
from app.services.admin.error_reporting_service import get_error_report_metrics
from app.services.admin.usage_log_service import get_usage_logs_metrics
from app.services.annuity.annuity_service import revive_soft_deleted_annuity_item
from app.services.audit.entity_audit import diff_snapshots, record_entity_change_audit
from app.services.case.case_numbering import (
    CASE_OUR_REF_NUMBERING_CONFIG_KEY,
    default_our_ref_numbering_config_json,
    validate_our_ref_numbering_config_payload,
)
from app.services.case.case_menu_config import (
    CASE_MENU_CONFIG_KEY,
    case_menu_config_json_for_editor,
    default_case_menu_config_json,
    preview_case_menu_config,
    validate_case_menu_config_payload,
)
from app.services.core.config_aliases import (
    apply_config_read_aliases,
    expand_config_delete_aliases,
    expand_config_update_aliases,
)
from app.services.core.config_service import ConfigService
from app.services.core.staff_options import clear_staff_assignment_cache
from app.services.deletion_manager import DeletionService
from app.services.deletion_manager import deletion_entity_label as service_deletion_entity_label
from app.services.deletion_manager import (
    infer_matter_id_for_deletion_log as service_infer_matter_id_for_deletion_log,
)
from app.services.ops.error_report_monitor import summarize_error_reports_days
from app.services.rules.rule_registry import (
    RULE_REGISTRY_KEY,
    preview_rule_registry,
    validate_rule_registry_payload,
)
from app.services.workflow.sync_requests import enqueue_annuity_sync_for_item
from app.utils.error_logging import report_swallowed_exception
from app.utils.permissions import can_access_matter, pick_primary_role_name, role_required
from app.utils.policy_sql import policy_text as text

_MASKED_CONFIG_VALUE = "***"
_SENSITIVE_CONFIG_MARKERS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "WEBHOOK",
    "API_KEY",
    "CLIENT_SECRET",
    "DATABASE_URL",
    "SQLALCHEMY_DATABASE_URI",
)

_BRAND_ASSET_CONFIG_KEYS = {
    "logo": "BRAND_LOGO_PATH",
    "favicon": "BRAND_FAVICON_PATH",
}
_BRAND_ASSET_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "ico"}
_BRAND_ASSET_URL_PREFIX = "branding/"


def _is_sensitive_config_key(key: str) -> bool:
    upper = str(key or "").upper()
    if not upper:
        return False
    return any(marker in upper for marker in _SENSITIVE_CONFIG_MARKERS)


def _mask_config_value(key: str, value: str | None) -> str:
    if not _is_sensitive_config_key(key):
        return value or ""
    return _MASKED_CONFIG_VALUE if str(value or "").strip() else ""


def _audit_config_value(key: str, value: Any, *, incoming: bool = False) -> str:
    if _is_sensitive_config_key(key):
        if incoming and str(value or "").strip():
            return "[Changed]"
        return "[Settings]" if str(value or "").strip() else "[]"
    return "" if value is None else str(value)


def _record_config_audit(
    *,
    action: str,
    key: str,
    old_value: Any,
    new_value: Any,
    source: str,
    incoming_sensitive_value: bool = False,
) -> None:
    before = {"key": key, "value": _audit_config_value(key, old_value)}
    after = {
        "key": key,
        "value": _audit_config_value(key, new_value, incoming=incoming_sensitive_value),
    }
    changes = diff_snapshots(before, after)
    if not changes:
        return
    record_entity_change_audit(
        action=action,
        target_type="system_config",
        actor_id=getattr(current_user, "id", None),
        changes=changes,
        meta={"key": key, "source": source},
        title=key,
    )


def _brand_asset_max_bytes() -> int:
    configured = ConfigService.get_int(
        "BRAND_ASSET_MAX_BYTES",
        1024 * 1024,
        min_value=1,
        max_value=10 * 1024 * 1024,
    )
    return int(configured or 1024 * 1024)


def _brand_asset_extension(filename: str) -> str:
    safe = secure_filename(filename or "")
    if "." not in safe:
        return ""
    ext = safe.rsplit(".", 1)[1].lower()
    return ext if ext in _BRAND_ASSET_ALLOWED_EXTENSIONS else ""


def _brand_asset_magic_ok(head: bytes, ext: str) -> bool:
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in {"jpg", "jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if ext == "ico":
        return head.startswith(b"\x00\x00\x01\x00")
    return False


def _brand_static_dir() -> str:
    static_root = current_app.static_folder or os.path.join(current_app.root_path, "static")
    dest_dir = os.path.join(static_root, "branding")
    os.makedirs(dest_dir, exist_ok=True)
    return dest_dir


def _delete_old_brand_asset(path_value: str | None) -> None:
    raw = str(path_value or "").strip().replace("\\", "/")
    if raw.startswith("/static/"):
        raw = raw[len("/static/") :]
    if not raw.startswith(_BRAND_ASSET_URL_PREFIX):
        return
    filename = secure_filename(raw[len(_BRAND_ASSET_URL_PREFIX) :])
    if not filename:
        return
    dest_dir = os.path.abspath(_brand_static_dir())
    target = os.path.abspath(os.path.join(dest_dir, filename))
    if not target.startswith(dest_dir + os.sep):
        return
    try:
        if os.path.isfile(target):
            os.remove(target)
    except OSError:
        current_app.logger.warning("Old branding asset cleanup skipped", exc_info=True)


def _save_brand_asset_stream(upload, dest_path: str, *, max_bytes: int) -> int:
    total = 0
    with open(dest_path, "wb") as fh:
        while True:
            chunk = upload.stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("file_too_large")
            fh.write(chunk)
    return total


def _role_names_for_user(user: User) -> list[str]:
    try:
        role_names = sorted(
            str(role.name) for role in (user.roles or []) if getattr(role, "name", None)
        )
    except (AttributeError, TypeError):
        role_names = []
    if role_names:
        return role_names
    return [str(user.role or "user")]


def _user_audit_snapshot(user: User) -> dict[str, Any]:
    return {
        "username": user.username,
        "email": user.email,
        "display_name": getattr(user, "display_name", None),
        "department": getattr(user, "department", None),
        "position": getattr(user, "position", None),
        "staff_party_id": getattr(user, "staff_party_id", None),
        "role": getattr(user, "role", None),
        "roles": _role_names_for_user(user),
        "is_active": getattr(user, "is_active", None),
    }


def _staff_primary_email(party_id: str) -> str | None:
    try:
        row = db.session.execute(
            text(
                """
                SELECT value
                  FROM party_contact
                 WHERE party_id = :party_id
                   AND contact_type = 'email'
                 ORDER BY contact_id ASC
                 LIMIT 1
            """
            ),
            {"party_id": party_id},
        ).first()
        return str(row[0]).strip() if row and row[0] else None
    except SQLAlchemyError:
        return None


def _staff_audit_snapshot(ps: PartyStaff | None, party: Party | None) -> dict[str, Any]:
    party_id = str(getattr(ps, "party_id", None) or getattr(party, "party_id", "") or "")
    return {
        "party_id": party_id,
        "staff_code": getattr(ps, "staff_code", None) if ps else None,
        "dept": getattr(ps, "dept", None) if ps else None,
        "active": getattr(ps, "active", None) if ps else None,
        "name_display": getattr(party, "name_display", None) if party else None,
        "email": _staff_primary_email(party_id) if party_id else None,
    }


def _safe_parse_int(
    raw: str | None,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    try:
        value = int(raw or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _case_menu_field_options() -> list[dict[str, str]]:
    try:
        from app.services.case_fields import FieldRegistry

        registry = FieldRegistry.instance()
        registry.initialize()
        options = []
        for key, field in registry.all_fields().items():
            options.append(
                {
                    "key": str(key or ""),
                    "label": str(getattr(field, "label", "") or key or ""),
                    "input_type": str(getattr(field, "input_type", "") or "text"),
                    "help_text": str(getattr(field, "help_text", "") or ""),
                }
            )
        return sorted(options, key=lambda item: (item["label"].lower(), item["key"].lower()))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="admin.routes.case_menu_field_options",
            log_key="admin.routes.case_menu_field_options",
            log_window_seconds=300,
        )
        return []


def _is_blank_text(col):
    return or_(col.is_(None), func.trim(func.coalesce(col, "")) == "")


def _is_missing_schema_object(exc: Exception, object_name: str) -> bool:
    msg = str(exc).lower()
    name = (object_name or "").strip().lower()
    if name and name not in msg:
        return False
    return any(
        marker in msg
        for marker in (
            "no such table",
            "does not exist",
            "undefinedtable",
            "no such column",
            "undefinedcolumn",
        )
    )


@bp.route("/upload-runs")
@login_required
@role_required("admin")
def upload_runs_page():
    return _redirect_to_automation_queue(default_status="ALL")


@bp.route("/automation/inbox")
@login_required
@role_required("admin")
def automation_inbox_page():
    return _redirect_to_automation_queue(default_status="REVIEW,READY")


def _redirect_to_automation_queue(*, default_status: str):
    params = request.args.to_dict(flat=False)
    status_values = [str(v or "").strip() for v in params.get("status", [])]
    if not any(status_values):
        params["status"] = [default_status]
    query = urlencode(params, doseq=True)
    target = "/doc/automation-queue"
    if query:
        target += f"?{query}"
    return redirect(target)


@bp.route("/data-quality")
@login_required
@role_required("admin")
def data_quality_page():
    sample_limit = _safe_parse_int(
        request.args.get("sample"),
        default=20,
        min_value=5,
        max_value=100,
    )
    parse_days = _safe_parse_int(
        request.args.get("parse_days"),
        default=7,
        min_value=1,
        max_value=90,
    )

    metrics = get_data_quality_metrics(sample_limit=sample_limit, parse_days=parse_days)

    return render_template(
        "admin/data_quality.html",
        active_page="data_quality",
        sample_limit=sample_limit,
        parse_days=parse_days,
        **metrics,
    )


@bp.route("/", strict_slashes=False)
@login_required
@role_required("admin")
def index():
    return render_template("admin/index.html", active_page="index")


@bp.route("/config")
@login_required
@role_required("admin")
def config_page():
    from app.models.role import Role

    all_config = {c.key: _mask_config_value(c.key, c.value) for c in SystemConfig.query.all()}
    available_roles = [r.name for r in Role.query.order_by(Role.name.asc()).all()]
    return render_template(
        "admin/config.html",
        active_page="config",
        config=current_app.config,
        all_config=all_config,
        available_roles=available_roles,
        our_ref_numbering_config_key=CASE_OUR_REF_NUMBERING_CONFIG_KEY,
        default_our_ref_numbering_config=default_our_ref_numbering_config_json(),
        case_menu_value=case_menu_config_json_for_editor(),
        case_menu_config_key=CASE_MENU_CONFIG_KEY,
        default_case_menu_config=case_menu_config_json_for_editor(default_case_menu_config_json()),
        case_menu_field_options=_case_menu_field_options(),
    )


@bp.route("/api/branding-asset", methods=["POST"])
@login_required
@role_required("admin")
def api_branding_asset():
    kind = str(request.form.get("kind") or "").strip().lower()
    config_key = _BRAND_ASSET_CONFIG_KEYS.get(kind)
    if not config_key:
        return jsonify({"success": False, "error": "invalid_brand_asset_kind"}), 400

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"success": False, "error": "file_required"}), 400

    ext = _brand_asset_extension(upload.filename)
    if not ext:
        return jsonify({"success": False, "error": "unsupported_image_type"}), 400

    try:
        content_len = int(getattr(upload, "content_length", 0) or 0)
    except (TypeError, ValueError):
        content_len = 0
    max_bytes = _brand_asset_max_bytes()
    if content_len and content_len > max_bytes:
        return jsonify({"success": False, "error": "file_too_large"}), 413

    head = upload.stream.read(16)
    upload.stream.seek(0)
    if not _brand_asset_magic_ok(head, ext):
        return jsonify({"success": False, "error": "invalid_image"}), 400

    dest_dir = _brand_static_dir()
    filename = f"{kind}-{uuid.uuid4().hex[:12]}.{ext}"
    dest_path = os.path.abspath(os.path.join(dest_dir, filename))
    if not dest_path.startswith(os.path.abspath(dest_dir) + os.sep):
        return jsonify({"success": False, "error": "invalid_destination"}), 400

    old_value = SystemConfig.get_config(config_key, "")
    try:
        _save_brand_asset_stream(upload, dest_path, max_bytes=max_bytes)
    except ValueError:
        try:
            if os.path.isfile(dest_path):
                os.remove(dest_path)
        except OSError:
            pass
        return jsonify({"success": False, "error": "file_too_large"}), 413
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="admin.routes.api_branding_asset.save",
            log_key="admin.routes.api_branding_asset.save",
            log_window_seconds=300,
        )
        return jsonify({"success": False, "error": "save_failed"}), 500

    path_value = f"{_BRAND_ASSET_URL_PREFIX}{filename}"
    SystemConfig.set_config(config_key, path_value)
    _record_config_audit(
        action="admin.branding_asset.update",
        key=config_key,
        old_value=old_value,
        new_value=path_value,
        source="admin.api_branding_asset",
    )
    db.session.commit()
    try:
        ConfigService.clear_cache()
    except Exception:
        current_app.logger.warning("ConfigService cache clear skipped", exc_info=True)
    _delete_old_brand_asset(old_value)

    return jsonify(
        {
            "success": True,
            "key": config_key,
            "path": path_value,
            "url": url_for("static", filename=path_value),
        }
    )


@bp.route("/codes")
@login_required
@role_required("admin")
def codes_page():
    return render_template("admin/codes.html", active_page="codes")


@bp.route("/users")
@login_required
@role_required("admin")
def users_page():
    from app.models.role import Role

    available_roles = [r.name for r in Role.query.all()]
    return render_template("admin/users.html", active_page="users", available_roles=available_roles)


@bp.route("/deadlines")
@login_required
@role_required("admin")
def deadlines_page():
    return render_template("admin/deadlines.html", active_page="deadlines")


@bp.route("/errors")
@login_required
@role_required("admin")
def error_reports_page():
    window_minutes = request.args.get("window", default=60, type=int)
    summary_limit = request.args.get("limit", default=20, type=int)
    recent_limit = request.args.get("recent", default=50, type=int)
    source_filter = request.args.get("source")
    type_filter = (request.args.get("type") or "").strip()
    text_filter = (request.args.get("q") or "").strip()
    status_filter = request.args.get("status", default=None, type=int)

    metrics = get_error_report_metrics(
        window_minutes=window_minutes,
        summary_limit=summary_limit,
        recent_limit=recent_limit,
        source_filter_raw=source_filter,
        type_filter=type_filter,
        text_filter=text_filter,
        status_filter=status_filter,
    )

    return render_template(
        "admin/error_reports.html",
        **metrics,
    )


@bp.route("/errors/reset", methods=["POST"])
@login_required
@role_required("admin")
def error_reports_reset():
    db.session.query(ErrorReport).delete(synchronize_session=False)
    db.session.commit()
    return redirect(url_for("admin.error_reports_page"))


@bp.route("/observability")
@login_required
@role_required("admin")
def observability_page():
    error_top_7 = summarize_error_reports_days(window_days=7, limit=15, min_count=1)
    error_top_30 = summarize_error_reports_days(window_days=30, limit=15, min_count=1)
    swallowed_top_7 = summarize_error_reports_days(
        window_days=7, limit=15, min_count=1, swallowed_only=True
    )
    swallowed_top_30 = summarize_error_reports_days(
        window_days=30, limit=15, min_count=1, swallowed_only=True
    )

    return render_template(
        "admin/observability.html",
        active_page="observability",
        error_top_7=error_top_7,
        error_top_30=error_top_30,
        swallowed_top_7=swallowed_top_7,
        swallowed_top_30=swallowed_top_30,
    )


@bp.route("/usage_logs")
@login_required
@role_required("admin")
def usage_logs_page():
    page = _safe_parse_int(request.args.get("page"), default=1, min_value=1, max_value=1_000_000)
    per_page = 100

    user_filter = (request.args.get("user_id") or "").strip()
    method_filter = (request.args.get("method") or "").strip().upper()
    status_filter = (request.args.get("status") or "").strip().lower()
    path_filter = (request.args.get("path") or "").strip()
    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw = (request.args.get("date_to") or "").strip()

    metrics = get_usage_logs_metrics(
        page=page,
        per_page=per_page,
        user_filter=user_filter,
        method_filter=method_filter,
        status_filter=status_filter,
        path_filter=path_filter,
        date_from_raw=date_from_raw,
        date_to_raw=date_to_raw,
    )

    return render_template(
        "admin/usage_logs.html",
        active_page="usage_logs",
        **metrics,
    )


@bp.route("/usage_logs/purge", methods=["POST"])
@login_required
@role_required("admin")
def usage_logs_purge():
    days_raw = (request.form.get("days") or "").strip()
    user_raw = (request.form.get("user_id") or "").strip()

    days = _safe_parse_int(days_raw, default=90, min_value=1, max_value=3650)

    cutoff = datetime.utcnow() - timedelta(days=days)

    q = db.session.query(UserAccessLog).filter(UserAccessLog.created_at < cutoff)
    if user_raw.isdigit():
        q = q.filter(UserAccessLog.user_id == int(user_raw))

    deleted = 0
    try:
        deleted = int(q.delete(synchronize_session=False) or 0)
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500

    try:
        from app.blueprints.billing_invoices.auth import log_audit

        log_audit(
            "user_access_log.purge",
            "user_access_log",
            None,
            json.dumps(
                {
                    "days": days,
                    "cutoff_utc": cutoff.isoformat(),
                    "user_id": int(user_raw) if user_raw.isdigit() else None,
                    "deleted": deleted,
                },
                ensure_ascii=False,
            ),
        )
    except (ImportError, RuntimeError, SQLAlchemyError, TypeError, ValueError) as exc:
        # Best-effort only.
        report_swallowed_exception(
            exc,
            context="admin.usage_logs_purge.log_audit",
            log_key="admin.usage_logs_purge.log_audit",
            log_window_seconds=300,
        )

    return redirect(url_for("admin.usage_logs_page"))


def _as_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_payload_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return _as_bool(None if raw is None else str(raw))


def _coerce_int(raw: Any) -> int | None:
    try:
        if raw is None:
            return None
        text_val = str(raw).strip()
        if not text_val:
            return None
        return int(text_val)
    except (TypeError, ValueError):
        return None


def _json_text_value(column, key: str):
    dialect = (db.engine.dialect.name or "").lower()
    if dialect.startswith("postgres"):
        return column.op("->>")(key)
    return func.json_extract(column, f"$.{key}")


def _resolve_user_lookup(token: str | None) -> User | None:
    lookup = (token or "").strip()
    if not lookup:
        return None

    if lookup.isdigit():
        user = db.session.get(User, int(lookup))
        if user:
            return user

    lowered = lookup.lower()
    user = User.query.filter(
        or_(func.lower(User.email) == lowered, func.lower(User.username) == lowered)
    ).first()
    if user:
        return user

    return User.query.filter(func.lower(func.coalesce(User.display_name, "")) == lowered).first()


def _resolve_matter_lookup(token: str | None) -> Matter | None:
    lookup = (token or "").strip()
    if not lookup:
        return None

    matter = Matter.query.filter(
        or_(
            Matter.matter_id == lookup,
            Matter.our_ref == lookup,
            Matter.old_our_ref == lookup,
            Matter.your_ref == lookup,
        )
    ).first()
    if matter:
        return matter

    legacy_case_id = _coerce_int(lookup)
    if legacy_case_id is None:
        return None

    legacy_case = db.session.get(Case, legacy_case_id)
    if not legacy_case:
        return None

    case_ref = (getattr(legacy_case, "ref_no", None) or "").strip()
    if not case_ref:
        return None

    return Matter.query.filter(
        or_(Matter.our_ref == case_ref, Matter.old_our_ref == case_ref, Matter.your_ref == case_ref)
    ).first()


# Compatibility shims for the admin routes. The implementation lives in
# app.services.deletion_manager so recycle-bin behavior stays reusable outside
# the admin blueprint.
def _deletion_entity_label(entity_type: str | None) -> str:
    return service_deletion_entity_label(entity_type)


def _infer_matter_id_for_deletion_log(log: DeletionLog) -> str | None:
    return service_infer_matter_id_for_deletion_log(log)


def _build_restore_preview(log: DeletionLog) -> dict[str, Any]:
    return DeletionService().preview_log(log)


def _restore_deletion_log_entry(log: DeletionLog, *, actor_user_id: int | None) -> dict[str, Any]:
    return DeletionService().restore_log(log, actor_user_id=actor_user_id)


def _deletion_log_query_from_filters(
    *,
    entity_type: str = "",
    include_restored: bool = False,
    search: str = "",
    date_from: str = "",
    date_to: str = "",
):
    q = DeletionLog.query
    if entity_type:
        q = q.filter_by(entity_type=entity_type)
    if not include_restored:
        q = q.filter(DeletionLog.restored_at.is_(None))
    if search:
        search_pattern = f"%{search}%"
        q = q.filter(
            db.or_(
                DeletionLog.title.ilike(search_pattern),
                DeletionLog.entity_key.ilike(search_pattern),
                DeletionLog.search_vector.ilike(search_pattern),
                db.cast(DeletionLog.entity_id, db.String).ilike(search_pattern),
                db.cast(DeletionLog.payload, db.String).ilike(search_pattern),
            )
        )

    if date_from:
        try:
            from_dt = datetime.fromisoformat(date_from)
            q = q.filter(DeletionLog.deleted_at >= from_dt)
        except ValueError as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.deletions.date_from",
                log_key="admin.routes.deletions.date_from",
                log_window_seconds=300,
            )
    if date_to:
        try:
            to_dt = datetime.fromisoformat(date_to + " 23:59:59")
            q = q.filter(DeletionLog.deleted_at <= to_dt)
        except ValueError as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.deletions.date_to",
                log_key="admin.routes.deletions.date_to",
                log_window_seconds=300,
            )
    return q


def _apply_deletion_log_matter_filter(query, matter_filter: str):
    resolved_matter = _resolve_matter_lookup(matter_filter)
    target_matter_id = str(resolved_matter.matter_id) if resolved_matter else matter_filter.strip()
    if not target_matter_id:
        return query.filter(DeletionLog.id < 0), None

    target_value = str(target_matter_id)
    payload_matter_id = func.trim(
        func.coalesce(_json_text_value(DeletionLog.payload, "matter_id"), "")
    )
    payload_case_id = func.trim(func.coalesce(_json_text_value(DeletionLog.payload, "case_id"), ""))
    entity_type_expr = func.lower(func.trim(func.coalesce(DeletionLog.entity_type, "")))

    return (
        query.filter(
            or_(
                and_(
                    func.lower(func.trim(func.coalesce(DeletionLog.parent_type, ""))) == "matter",
                    func.trim(func.coalesce(DeletionLog.parent_id, "")) == target_value,
                ),
                and_(entity_type_expr == "workflow", payload_case_id == target_value),
                and_(
                    entity_type_expr != "workflow",
                    or_(payload_matter_id == target_value, payload_case_id == target_value),
                ),
            )
        ),
        target_matter_id,
    )


def _deletion_log_matches_matter(row: DeletionLog, target_matter_id: str | None) -> bool:
    if not target_matter_id:
        return False
    inferred_mid = _infer_matter_id_for_deletion_log(row)
    return bool(inferred_mid and str(inferred_mid) == str(target_matter_id))


def _build_matter_assignment_snapshot(matter_id: str) -> list[dict[str, Any]]:
    rows = (
        db.session.query(MatterStaffAssignment, Party, PartyStaff)
        .outerjoin(Party, Party.party_id == MatterStaffAssignment.staff_party_id)
        .outerjoin(PartyStaff, PartyStaff.party_id == MatterStaffAssignment.staff_party_id)
        .filter(MatterStaffAssignment.matter_id == str(matter_id))
        .order_by(
            func.lower(func.coalesce(MatterStaffAssignment.staff_role_code, "")),
            func.coalesce(Party.name_display, ""),
        )
        .all()
    )

    party_ids = {
        str(msa.staff_party_id)
        for msa, _party, _staff in rows
        if getattr(msa, "staff_party_id", None) is not None
    }
    users_by_party: dict[str, User] = {}
    if party_ids:
        linked_users = (
            User.query.filter(User.staff_party_id.in_(party_ids))
            .order_by(User.is_active.desc(), User.username.asc())
            .all()
        )
        for user in linked_users:
            party_id = str(user.staff_party_id or "")
            if party_id and party_id not in users_by_party:
                users_by_party[party_id] = user

    out: list[dict[str, Any]] = []
    for msa, party, pstaff in rows:
        staff_party_id = str(msa.staff_party_id or "")
        linked_user = users_by_party.get(staff_party_id)
        out.append(
            {
                "staff_role_code": (msa.staff_role_code or "").strip().lower() or "-",
                "staff_party_id": staff_party_id or None,
                "staff_name": (getattr(party, "name_display", None) or "").strip() or "-",
                "staff_dept": (getattr(pstaff, "dept", None) or "").strip() or None,
                "linked_user_id": getattr(linked_user, "id", None),
                "linked_username": getattr(linked_user, "username", None),
                "linked_user_role": getattr(linked_user, "role", None),
            }
        )
    return out


def _build_access_reason_tree(user: User, *, matter: Matter, action: str) -> dict[str, Any]:
    from app.utils import permissions as permission_utils

    role = (getattr(user, "role", None) or "").strip().lower()
    staff_party_id = (getattr(user, "staff_party_id", None) or "").strip() or None
    department = (getattr(user, "department", None) or "").strip() or None
    matter_id = str(getattr(matter, "matter_id", ""))
    normalized_action = (action or "view").strip().lower()
    if normalized_action not in {"view", "edit_case", "assign_staff", "delete_case", "invoice"}:
        normalized_action = "view"

    direct_assigned = permission_utils._has_direct_assignment(
        staff_party_id=staff_party_id,
        matter_id=matter_id,
    )
    team_assigned = permission_utils._has_team_assignment(
        department=department,
        matter_id=matter_id,
    )
    allowed = can_access_matter(user, matter_id, action=normalized_action)

    def _node(
        *,
        label: str,
        passed: bool,
        detail: str,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "label": label,
            "passed": bool(passed),
            "detail": detail,
            "children": children or [],
        }

    root_nodes: list[dict[str, Any]] = []
    root_nodes.append(
        _node(
            label="Account Status",
            passed=bool(getattr(user, "is_authenticated", False))
            and bool(getattr(user, "is_active", True)),
            detail=(
                f"authenticated={bool(getattr(user, 'is_authenticated', False))}, "
                f"is_active={bool(getattr(user, 'is_active', True))}"
            ),
            children=[
                _node(
                    label="Login ",
                    passed=bool(getattr(user, "is_authenticated", False)),
                    detail="Login User   target.",
                ),
                _node(
                    label="active Account ",
                    passed=bool(getattr(user, "is_active", True)),
                    detail="active Account Search .",
                ),
            ],
        )
    )

    root_nodes.append(
        _node(
            label="Matter Status",
            passed=not bool(getattr(matter, "is_deleted", False)),
            detail=(
                f"matter_id={matter_id}, our_ref={(getattr(matter, 'our_ref', None) or '-')}, "
                f"is_deleted={bool(getattr(matter, 'is_deleted', False))}"
            ),
            children=[
                _node(
                    label="Deleted status",
                    passed=not bool(getattr(matter, "is_deleted", False)),
                    detail=(
                        "Deleted status Matter Data  operating policy Search /    exists."
                    ),
                )
            ],
        )
    )

    decision_children: list[dict[str, Any]] = []
    decision_children.append(
        _node(
            label=" Responsible(assignment) Confirm",
            passed=direct_assigned,
            detail=(
                f"staff_party_id={staff_party_id or '-'}  Matter view  Role"
                f"(CASE_SELF_ASSIGNED_ROLE_CODES={','.join(permission_utils.get_self_assigned_role_codes())})"
                "   "
            ),
        )
    )
    decision_children.append(
        _node(
            label=" Responsible(team assignment) Confirm",
            passed=team_assigned,
            detail=f"department={department or '-'}  Matter Contact Department match ",
        )
    )

    if normalized_action == "view":
        global_allowed = role in permission_utils._ROLE_CASE_VIEW_GLOBAL
        team_role = role in permission_utils._ROLE_CASE_TEAM
        decision_children.insert(
            0,
            _node(
                label=" Search Role",
                passed=global_allowed,
                detail=f"role={role or '-'} in _ROLE_CASE_VIEW_GLOBAL",
            ),
        )
        decision_children.append(
            _node(
                label=" Search Role",
                passed=team_role,
                detail=f"role={role or '-'} in _ROLE_CASE_TEAM",
            )
        )
        decision_detail = " items: Search Role   Responsible (Search Role + Responsible match)."
    elif normalized_action == "edit_case":
        global_allowed = role in permission_utils._ROLE_CASE_GLOBAL
        team_lead_role = role in permission_utils._ROLE_CASE_TEAM_LEAD
        readonly_role = role in permission_utils._ROLE_READ_ONLY
        decision_children.insert(
            0,
            _node(
                label=" Edit Role",
                passed=global_allowed,
                detail=f"role={role or '-'} in _ROLE_CASE_GLOBAL",
            ),
        )
        decision_children.append(
            _node(
                label=" Role",
                passed=not readonly_role,
                detail=f"role={role or '-'} in _ROLE_READ_ONLY => {readonly_role}",
            )
        )
        decision_children.append(
            _node(
                label=" Role( Edit )",
                passed=team_lead_role,
                detail=f"role={role or '-'} in _ROLE_CASE_TEAM_LEAD",
            )
        )
        decision_detail = " items: Edit Role  Responsible ( Role + Responsible match)."
    elif normalized_action == "assign_staff":
        global_allowed = role in permission_utils._ROLE_CASE_GLOBAL
        assign_role = role in permission_utils._ROLE_CASE_ASSIGN
        decision_children.insert(
            0,
            _node(
                label=" Responsible Role",
                passed=global_allowed,
                detail=f"role={role or '-'} in _ROLE_CASE_GLOBAL",
            ),
        )
        decision_children.append(
            _node(
                label="Responsible Permissions Role",
                passed=assign_role,
                detail=f"role={role or '-'} in _ROLE_CASE_ASSIGN",
            )
        )
        decision_detail = " items:  Role (Responsible Permissions Role + Responsible match)."
    elif normalized_action == "delete_case":
        delete_role = role in permission_utils._ROLE_CASE_DELETE
        decision_children.insert(
            0,
            _node(
                label="Delete Permissions Role",
                passed=delete_role,
                detail=f"role={role or '-'} in _ROLE_CASE_DELETE",
            ),
        )
        decision_detail = " items: _ROLE_CASE_DELETE  Role."
    else:
        business_super = role in permission_utils._ROLE_BUSINESS_SUPER
        invoice_roles = set(permission_utils.get_invoice_roles())
        if "manager" in invoice_roles:
            invoice_roles.update(permission_utils.get_management_roles())
        invoice_role_ok = role == permission_utils.ROLE_ADMIN or role in invoice_roles
        decision_children.insert(
            0,
            _node(
                label="  Role",
                passed=business_super,
                detail=f"role={role or '-'} in _ROLE_BUSINESS_SUPER",
            ),
        )
        decision_children.append(
            _node(
                label="Invoice Role  ",
                passed=invoice_role_ok,
                detail=f"role={role or '-'} in STAFF_INVOICE_ROLES Extend ",
            )
        )
        decision_detail = " items: super Role (invoice Role + Search items )."

    root_nodes.append(
        _node(
            label=f"  ({normalized_action})",
            passed=allowed,
            detail=decision_detail,
            children=decision_children,
        )
    )

    return {
        "allowed": bool(allowed),
        "action": normalized_action,
        "facts": {
            "role": role or None,
            "staff_party_id": staff_party_id,
            "department": department,
            "direct_assigned": direct_assigned,
            "team_assigned": team_assigned,
            "matter_deleted": bool(getattr(matter, "is_deleted", False)),
        },
        "policy_ref": "app/utils/permissions.py:can_access_matter",
        "tree": root_nodes,
    }


@bp.route("/access-debug")
@login_required
@role_required("admin")
def access_debug_page():
    return render_template("admin/access_debug.html", active_page="access_debug")


@bp.route("/api/access-debug", methods=["GET"])
@login_required
@role_required("admin")
def api_access_debug():
    user_lookup = (
        request.args.get("user")
        or request.args.get("user_id")
        or request.args.get("username")
        or request.args.get("email")
    )
    matter_lookup = (
        request.args.get("matter")
        or request.args.get("matter_id")
        or request.args.get("our_ref")
        or request.args.get("case_ref")
    )
    action = (request.args.get("action") or "view").strip().lower() or "view"

    user = _resolve_user_lookup(user_lookup)
    if not user:
        return jsonify({"ok": False, "error": "user_not_found"}), 404

    team_reassign_url = url_for("admin.users_page")
    if getattr(user, "staff_party_id", None):
        team_reassign_url = url_for(
            "admin.users_page",
            focus_party_id=str(user.staff_party_id),
            auto_reassign="1",
        )

    shortcuts = {
        "case_detail_url": None,
        "matter_edit_url": None,
        "team_reassign_url": team_reassign_url,
        "users_admin_url": url_for("admin.users_page"),
    }
    user_payload = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": bool(getattr(user, "is_active", True)),
        "department": user.department,
        "staff_party_id": user.staff_party_id,
    }

    matter_lookup_text = (matter_lookup or "").strip()
    if not matter_lookup_text:
        return jsonify(
            {
                "ok": True,
                "user": user_payload,
                "matter": None,
                "evaluation": None,
                "assignments": [],
                "shortcuts": shortcuts,
            }
        )

    matter = _resolve_matter_lookup(matter_lookup_text)
    if not matter:
        return jsonify({"ok": False, "error": "matter_not_found"}), 404

    assignment_rows = _build_matter_assignment_snapshot(str(matter.matter_id))
    evaluation = _build_access_reason_tree(user, matter=matter, action=action)

    shortcuts["case_detail_url"] = url_for("case_work.case_detail", case_id=str(matter.matter_id))
    shortcuts["matter_edit_url"] = url_for("case_work.edit_matter", matter_id=str(matter.matter_id))

    return jsonify(
        {
            "ok": True,
            "user": user_payload,
            "matter": {
                "matter_id": str(matter.matter_id),
                "our_ref": matter.our_ref,
                "old_our_ref": matter.old_our_ref,
                "your_ref": matter.your_ref,
                "right_name": matter.right_name,
                "is_deleted": bool(getattr(matter, "is_deleted", False)),
            },
            "evaluation": evaluation,
            "assignments": assignment_rows,
            "shortcuts": shortcuts,
        }
    )


@bp.route("/deletions")
@login_required
@role_required("admin")
def deletions_page():
    active_tab = (request.args.get("tab") or "audit").strip().lower()
    if active_tab not in ("audit", "deletions"):
        active_tab = "audit"

    page = _safe_parse_int(request.args.get("page"), default=1, min_value=1, max_value=1_000_000)
    per_page = 50

    action_filter = request.args.get("action", "").strip()
    user_filter = request.args.get("user_id", "").strip()
    target_type_filter = request.args.get("target_type", "").strip()

    logs = []
    total_count = 0
    action_types = []
    target_types = []
    restore_ok = {}

    conn = get_invoice_db()
    try:
        has_action = bool(action_filter)
        action_like = f"%{action_filter}%" if has_action else None

        user_id = int(user_filter) if (user_filter and user_filter.isdigit()) else None
        has_user = user_id is not None

        has_type = bool(target_type_filter)
        target_type = target_type_filter if has_type else None

        if has_action and has_user and has_type:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.action LIKE ? AND a.user_id = ? AND a.target_type = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = (
                "SELECT COUNT(*) "
                "FROM audit_log a "
                "WHERE a.action LIKE ? AND a.user_id = ? AND a.target_type = ?"
            )
            params = [action_like, user_id, target_type]
        elif has_action and has_user:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.action LIKE ? AND a.user_id = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a WHERE a.action LIKE ? AND a.user_id = ?"
            params = [action_like, user_id]
        elif has_action and has_type:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.action LIKE ? AND a.target_type = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = (
                "SELECT COUNT(*) FROM audit_log a WHERE a.action LIKE ? AND a.target_type = ?"
            )
            params = [action_like, target_type]
        elif has_user and has_type:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.user_id = ? AND a.target_type = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a WHERE a.user_id = ? AND a.target_type = ?"
            params = [user_id, target_type]
        elif has_action:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.action LIKE ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a WHERE a.action LIKE ?"
            params = [action_like]
        elif has_user:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.user_id = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a WHERE a.user_id = ?"
            params = [user_id]
        elif has_type:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "WHERE a.target_type = ? "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a WHERE a.target_type = ?"
            params = [target_type]
        else:
            logs_sql = (
                "SELECT a.*, u.username "
                "FROM audit_log a "
                "LEFT JOIN users u ON u.id = a.user_id "
                "ORDER BY a.created_at DESC "
                "LIMIT ? OFFSET ?"
            )
            count_sql = "SELECT COUNT(*) FROM audit_log a"
            params = []

        total_count = conn.execute(count_sql, params).fetchone()[0]
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        logs = conn.execute(logs_sql, params + [per_page, offset]).fetchall()

        action_types = conn.execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action"
        ).fetchall()

        target_types = conn.execute(
            "SELECT DISTINCT target_type FROM audit_log WHERE target_type IS NOT NULL ORDER BY target_type"
        ).fetchall()

        try:
            backup_dir = current_app.config.get("BACKUP_DIR")
            if backup_dir:
                os.makedirs(backup_dir, exist_ok=True)
            backup_times = []
            if backup_dir and os.path.isdir(backup_dir):
                for name in os.listdir(backup_dir):
                    base, ext = os.path.splitext(name)
                    if not name.startswith("backup-") or ext not in (".db", ".sql", ".dump"):
                        continue
                    ts_str = base[len("backup-") :]
                    try:
                        dt = datetime.strptime(ts_str, "%Y%m%d%H%M%S")
                        backup_times.append(dt)
                    except ValueError as exc:
                        # Best-effort: ignore unexpected backup filenames.
                        report_swallowed_exception(
                            exc,
                            context="admin.routes.deletions.parse_backup_timestamp",
                            log_key="admin.routes.deletions.parse_backup_timestamp",
                            log_window_seconds=300,
                        )
            backup_times.sort()
            for log in logs:
                try:
                    created = str(log["created_at"])
                except (KeyError, TypeError):
                    created = ""
                try:
                    at = datetime.fromisoformat(created.replace("T", " ").split(".")[0])
                except ValueError:
                    at = None
                ok = False
                if at and backup_times:
                    for b in backup_times:
                        if b <= at:
                            ok = True
                        else:
                            break
                restore_ok[log["id"]] = ok
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.deletions.restore_ok",
                log_key="admin.routes.deletions.restore_ok",
                log_window_seconds=300,
            )
            try:
                for log in logs:
                    restore_ok[log["id"]] = False
            except (KeyError, TypeError) as inner_exc:
                report_swallowed_exception(
                    inner_exc,
                    context="admin.routes.deletions.restore_ok.fallback",
                    log_key="admin.routes.deletions.restore_ok.fallback",
                    log_window_seconds=300,
                )
    finally:
        conn.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)

    users = [{"id": u.id, "username": u.username} for u in User.query.order_by(User.username).all()]

    from app.blueprints.billing_invoices.maintenance import (
        get_maintenance_info,
        is_maintenance_mode,
    )

    maintenance_active = is_maintenance_mode()
    maintenance_info = get_maintenance_info() if maintenance_active else None

    return render_template(
        "admin/deletions.html",
        active_tab=active_tab,
        logs=logs,
        action_types=action_types,
        target_types=target_types,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        action_filter=action_filter,
        user_filter=user_filter,
        target_type_filter=target_type_filter,
        restore_ok=restore_ok,
        users=users,
        maintenance_active=maintenance_active,
        maintenance_info=maintenance_info,
        active_page="deletions",
    )


@bp.route("/api/deletions", methods=["GET"])
@login_required
@role_required("admin")
def api_deletions():
    entity_type = (request.args.get("entity_type") or "").strip()
    include_restored = _as_bool(request.args.get("include_restored"))
    search = (request.args.get("search") or "").strip()
    matter_filter = (request.args.get("matter") or request.args.get("matter_id") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    include_preview = _as_bool(request.args.get("include_preview") or "1")
    timeline_mode = _as_bool(request.args.get("timeline"))

    page = _safe_parse_int(request.args.get("page"), default=1, min_value=1, max_value=1_000_000)
    per_page = _safe_parse_int(
        request.args.get("per_page"),
        default=50,
        min_value=1,
        max_value=200,
    )

    def _offset_for_total(total_rows: int) -> int:
        nonlocal page
        pages = max(1, (int(total_rows or 0) + per_page - 1) // per_page)
        if page > pages:
            page = pages
        return (page - 1) * per_page

    q = DeletionLog.query
    if entity_type:
        q = q.filter_by(entity_type=entity_type)
    if not include_restored:
        q = q.filter(DeletionLog.restored_at.is_(None))
    if search:
        search_pattern = f"%{search}%"
        q = q.filter(
            db.or_(
                DeletionLog.title.ilike(search_pattern),
                DeletionLog.entity_key.ilike(search_pattern),
                DeletionLog.search_vector.ilike(search_pattern),
                db.cast(DeletionLog.entity_id, db.String).ilike(search_pattern),
                db.cast(DeletionLog.payload, db.String).ilike(search_pattern),
            )
        )

    if date_from:
        try:
            from_dt = datetime.fromisoformat(date_from)
            q = q.filter(DeletionLog.deleted_at >= from_dt)
        except ValueError as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.deletions.date_from",
                log_key="admin.routes.deletions.date_from",
                log_window_seconds=300,
            )
    if date_to:
        try:
            to_dt = datetime.fromisoformat(date_to + " 23:59:59")
            q = q.filter(DeletionLog.deleted_at <= to_dt)
        except ValueError as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.deletions.date_to",
                log_key="admin.routes.deletions.date_to",
                log_window_seconds=300,
            )

    ordered_q = q.order_by(DeletionLog.deleted_at.desc(), DeletionLog.id.desc())
    all_rows_for_timeline: list[DeletionLog] = []

    if matter_filter:
        strict_q, target_matter_id = _apply_deletion_log_matter_filter(ordered_q, matter_filter)
        if not target_matter_id:
            total = 0
            rows = []
            all_rows_for_timeline = []
        else:
            try:
                total = strict_q.count()
                rows = strict_q.offset(_offset_for_total(total)).limit(per_page).all()
                all_rows_for_timeline = strict_q.limit(500).all() if timeline_mode else rows
            except SQLAlchemyError as exc:
                report_swallowed_exception(
                    exc,
                    context="admin.routes.api_deletions.matter_filter_sql",
                    log_key="admin.routes.api_deletions.matter_filter_sql",
                    log_window_seconds=300,
                )
                candidate_rows = ordered_q.all()
                strict_rows: list[DeletionLog] = []
                for row in candidate_rows:
                    if _deletion_log_matches_matter(row, target_matter_id):
                        strict_rows.append(row)

                total = len(strict_rows)
                offset = _offset_for_total(total)
                rows = strict_rows[offset : offset + per_page]
                all_rows_for_timeline = strict_rows
    else:
        total = ordered_q.count()
        rows = ordered_q.offset(_offset_for_total(total)).limit(per_page).all()
        all_rows_for_timeline = rows

    user_ids = set()
    matter_ids = set()
    for row in rows:
        if row.deleted_by:
            user_ids.add(row.deleted_by)
        if row.restored_by:
            user_ids.add(row.restored_by)
        inferred_mid = _infer_matter_id_for_deletion_log(row)
        if inferred_mid:
            matter_ids.add(str(inferred_mid))
    if timeline_mode:
        for row in all_rows_for_timeline[:500]:
            inferred_mid = _infer_matter_id_for_deletion_log(row)
            if inferred_mid:
                matter_ids.add(str(inferred_mid))

    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    matters = (
        {str(m.matter_id): m for m in Matter.query.filter(Matter.matter_id.in_(matter_ids)).all()}
        if matter_ids
        else {}
    )

    def _dt(v):
        return v.isoformat() if v else None

    def _user_name(uid):
        if not uid:
            return ""
        u = users.get(uid)
        if not u:
            return str(uid)
        return u.display_name or u.username or u.email or str(uid)

    items: list[dict[str, Any]] = []
    for row in rows:
        inferred_mid = _infer_matter_id_for_deletion_log(row)
        matter = matters.get(str(inferred_mid)) if inferred_mid else None
        preview = _build_restore_preview(row) if include_preview else None
        items.append(
            {
                "id": row.id,
                "entity_type": row.entity_type,
                "entity_type_label": _deletion_entity_label(row.entity_type),
                "entity_id": row.entity_id,
                "entity_key": row.entity_key,
                "title": row.title,
                "payload": row.payload,
                "parent_type": row.parent_type,
                "parent_id": row.parent_id,
                "tags": row.tags,
                "matter_id": str(inferred_mid) if inferred_mid else None,
                "matter_ref": getattr(matter, "our_ref", None) if matter else None,
                "deleted_by": row.deleted_by,
                "deleted_by_name": _user_name(row.deleted_by),
                "deleted_at": _dt(row.deleted_at),
                "restored_entity_id": row.restored_entity_id,
                "restored_entity_key": row.restored_entity_key,
                "restored_by": row.restored_by,
                "restored_by_name": _user_name(row.restored_by),
                "restored_at": _dt(row.restored_at),
                "can_restore": (
                    False if row.restored_at else (preview.get("can_restore") if preview else None)
                ),
                "preview_warning_count": len((preview or {}).get("warnings") or []),
                "preview_blocker_count": len((preview or {}).get("blockers") or []),
            }
        )

    timeline: list[dict[str, Any]] = []
    if timeline_mode:
        source_rows = all_rows_for_timeline[:500] if matter_filter else all_rows_for_timeline
        timeline_items: list[dict[str, Any]] = []
        for row in source_rows:
            inferred_mid = _infer_matter_id_for_deletion_log(row)
            matter = matters.get(str(inferred_mid)) if inferred_mid else None
            timeline_items.append(
                {
                    "id": row.id,
                    "entity_type": row.entity_type,
                    "entity_type_label": _deletion_entity_label(row.entity_type),
                    "title": row.title,
                    "matter_id": str(inferred_mid) if inferred_mid else None,
                    "matter_ref": getattr(matter, "our_ref", None) if matter else None,
                    "deleted_at": _dt(row.deleted_at),
                    "restored_at": _dt(row.restored_at),
                }
            )

        timeline_items.sort(key=lambda x: x.get("deleted_at") or "")
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in timeline_items:
            key = (item.get("deleted_at") or "")[:10] or "-"
            buckets.setdefault(key, []).append(item)
        timeline = [{"date": k, "items": buckets[k]} for k in sorted(buckets.keys())]

    return jsonify(
        {
            "items": items,
            "timeline": timeline,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page) if per_page > 0 else 1,
        }
    )


@bp.route("/api/deletions/<int:log_id>/preview", methods=["GET"])
@login_required
@role_required("admin")
def api_deletion_preview(log_id: int):
    log = db.session.get(DeletionLog, log_id)
    if not log:
        return jsonify({"error": "not found"}), 404
    preview = _build_restore_preview(log)
    return jsonify(
        {
            "id": log.id,
            "entity_type": log.entity_type,
            "entity_type_label": _deletion_entity_label(log.entity_type),
            "title": log.title,
            "entity_id": log.entity_id,
            "entity_key": log.entity_key,
            "preview": preview,
        }
    )


@bp.route("/api/deletions/bulk-restore", methods=["POST"])
@login_required
@role_required("admin")
def api_bulk_restore_deletions():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "ids(list) is required"}), 400

    log_ids: list[int] = []
    seen = set()
    for raw_id in raw_ids:
        lid = _coerce_int(raw_id)
        if lid is None or lid <= 0 or lid in seen:
            continue
        seen.add(lid)
        log_ids.append(lid)
    if not log_ids:
        return jsonify({"error": "no valid ids"}), 400

    restored: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    actor_id = getattr(current_user, "id", None)

    for log_id in log_ids:
        log = db.session.get(DeletionLog, log_id)
        if not log:
            failed.append({"id": log_id, "reason": "not_found"})
            continue
        if log.restored_at:
            skipped.append({"id": log_id, "reason": "already_restored"})
            continue
        try:
            result = _restore_deletion_log_entry(log, actor_user_id=actor_id)
            restored.append({"id": log_id, "result": result})
        except ValueError as exc:
            db.session.rollback()
            code = str(exc)
            payload = {"id": log_id, "reason": code}
            preview = getattr(exc, "preview", None)
            if preview:
                payload["preview"] = preview
            if code in {"already_restored", "dependency_blocked"}:
                skipped.append(payload)
            else:
                failed.append(payload)
        except SQLAlchemyError as exc:
            db.session.rollback()
            failed.append({"id": log_id, "reason": str(exc)})

    return jsonify(
        {
            "success": len(failed) == 0,
            "requested": len(log_ids),
            "restored": len(restored),
            "skipped": skipped,
            "failed": failed,
        }
    )


@bp.route("/api/deletions/bulk-delete", methods=["POST"])
@login_required
@role_required("admin")
def api_bulk_delete_deletions():
    data = request.get_json(silent=True) or {}
    delete_all_matching = _as_payload_bool(data.get("delete_all_matching")) or (
        str(data.get("mode") or "").strip().lower() in {"filtered", "all_matching"}
    )
    if delete_all_matching:
        confirm = str(data.get("confirm") or "").strip().upper()
        if confirm != "DELETE":
            return jsonify({"error": "confirm DELETE is required"}), 400

        filters = data.get("filters") or {}
        if not isinstance(filters, dict):
            return jsonify({"error": "filters(object) is required"}), 400

        entity_type = str(filters.get("entity_type") or "").strip()
        include_restored = _as_payload_bool(filters.get("include_restored"))
        search = str(filters.get("search") or "").strip()
        matter_filter = str(filters.get("matter") or filters.get("matter_id") or "").strip()
        date_from = str(filters.get("date_from") or "").strip()
        date_to = str(filters.get("date_to") or "").strip()

        filtered_q = _deletion_log_query_from_filters(
            entity_type=entity_type,
            include_restored=include_restored,
            search=search,
            date_from=date_from,
            date_to=date_to,
        ).order_by(DeletionLog.deleted_at.desc(), DeletionLog.id.desc())

        try:
            if matter_filter:
                filtered_q, target_matter_id = _apply_deletion_log_matter_filter(
                    filtered_q,
                    matter_filter,
                )
                if not target_matter_id:
                    rows: list[DeletionLog] = []
                else:
                    rows = filtered_q.all()
            else:
                rows = filtered_q.all()
        except SQLAlchemyError as exc:
            db.session.rollback()
            if not matter_filter:
                return jsonify({"error": str(exc)}), 500
            report_swallowed_exception(
                exc,
                context="admin.routes.api_bulk_delete_deletions.matter_filter_sql",
                log_key="admin.routes.api_bulk_delete_deletions.matter_filter_sql",
                log_window_seconds=300,
            )
            base_q = _deletion_log_query_from_filters(
                entity_type=entity_type,
                include_restored=include_restored,
                search=search,
                date_from=date_from,
                date_to=date_to,
            ).order_by(DeletionLog.deleted_at.desc(), DeletionLog.id.desc())
            resolved_matter = _resolve_matter_lookup(matter_filter)
            target_matter_id = (
                str(resolved_matter.matter_id) if resolved_matter else matter_filter.strip()
            )
            rows = [
                row
                for row in base_q.all()
                if _deletion_log_matches_matter(row, target_matter_id)
            ]

        deleted_ids = [int(row.id) for row in rows]
        for row in rows:
            db.session.delete(row)
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "mode": "all_matching",
                "requested": len(deleted_ids),
                "deleted": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "filters": {
                    "entity_type": entity_type,
                    "include_restored": include_restored,
                    "matter": matter_filter,
                    "search": search,
                    "date_from": date_from,
                    "date_to": date_to,
                },
            }
        )

    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "ids(list) is required"}), 400

    log_ids: list[int] = []
    seen = set()
    for raw_id in raw_ids:
        lid = _coerce_int(raw_id)
        if lid is None or lid <= 0 or lid in seen:
            continue
        seen.add(lid)
        log_ids.append(lid)
    if not log_ids:
        return jsonify({"error": "no valid ids"}), 400

    rows = DeletionLog.query.filter(DeletionLog.id.in_(log_ids)).all()
    existing_ids = {row.id for row in rows}
    missing_ids = [lid for lid in log_ids if lid not in existing_ids]

    for row in rows:
        db.session.delete(row)
    db.session.commit()

    return jsonify(
        {
            "success": True,
            "requested": len(log_ids),
            "deleted": len(rows),
            "missing": missing_ids,
        }
    )


@bp.route("/api/deletions/<int:log_id>/restore", methods=["POST"])
@login_required
@role_required("admin")
def api_restore_deletion(log_id: int):
    log = db.session.get(DeletionLog, log_id)
    if not log:
        return jsonify({"error": "not found"}), 404

    try:
        result = _restore_deletion_log_entry(log, actor_user_id=getattr(current_user, "id", None))
        return jsonify(result)
    except ValueError as exc:
        db.session.rollback()
        code = str(exc)
        if code == "already_restored":
            return jsonify({"error": "already restored"}), 400
        if code == "not_found":
            return jsonify({"error": "not found"}), 404
        if code == "dependency_blocked":
            return (
                jsonify({"error": "dependency blocked", "preview": getattr(exc, "preview", {})}),
                400,
            )
        if code == "missing_due_date":
            return jsonify({"error": "missing due_date"}), 400
        if code == "unsupported_entity_type":
            return jsonify({"error": "unsupported entity_type"}), 400
        return jsonify({"error": code}), 400
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 500


# --- Admin APIs ---


@bp.route("/api/codes/groups", methods=["GET", "POST"])
@login_required
@role_required("admin")
def api_code_groups():
    if request.method == "POST":
        return _legacy_code_registry_disabled_response()

    rows = CodeGroup.query.all()
    return jsonify([{"code": r.code, "name": r.name} for r in rows])


def _legacy_code_registry_disabled_response():
    return (
        jsonify(
            {
                "success": False,
                "error": "legacy_code_registry_disabled",
                "details": "The legacy admin code registry is not connected to runtime behavior. Use the dedicated admin screens for roles, deadlines, billing categories, and case-kind code changes.",
            }
        ),
        410,
    )


@bp.route("/api/codes", methods=["GET", "POST", "PUT", "DELETE"])
@login_required
@role_required("admin")
def api_codes():
    if request.method == "POST":
        return _legacy_code_registry_disabled_response()

    if request.method == "PUT":
        return _legacy_code_registry_disabled_response()

    if request.method == "DELETE":
        return _legacy_code_registry_disabled_response()

    gid = request.args.get("group_id")
    q = Code.query
    if gid:
        q = q.filter_by(group_code=gid)
    rows = q.order_by(Code.sort).all()
    return jsonify(
        [
            {
                "group_id": r.group_code,
                "code": r.code,
                "name": r.name,
                "sort_order": r.sort or 0,
                "active": bool(r.active),
            }
            for r in rows
        ]
    )


@bp.route("/api/users", methods=["GET", "POST", "PATCH"])
@login_required
@role_required("admin")
def api_users():
    def _normalize_role_names(raw_roles: Any) -> list[str]:
        if not isinstance(raw_roles, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_roles:
            name = str(item or "").strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def _resolve_roles(role_names: list[str]):
        from app.models.role import Role

        if not role_names:
            return []
        return Role.query.filter(Role.name.in_(role_names)).all()

    def _apply_roles_to_user(user: User, role_names: list[str]) -> None:
        roles = _resolve_roles(role_names)
        user.roles = roles
        if roles:
            user.role = pick_primary_role_name([r.name for r in roles], default="user")
        else:
            user.role = pick_primary_role_name(role_names, default="user")

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        email = (data.get("email") or "").strip()
        display_name = (data.get("display_name") or "").strip()
        department = (data.get("department") or "").strip()
        position = (data.get("position") or "").strip()
        role_names = _normalize_role_names(data.get("roles"))
        if not role_names and data.get("role"):
            role_names = _normalize_role_names([data.get("role")])

        if not username or not password:
            return (
                jsonify({"success": False, "error": "username and password are required"}),
                400,
            )
        if User.query.filter_by(username=username).first():
            return jsonify({"success": False, "error": "username already exists"}), 400
        u = User(
            username=username,
            email=email or None,
            display_name=display_name or None,
            department=department or None,
            position=position or None,
            is_active=True,
            role="user",
        )
        _apply_roles_to_user(u, role_names)
        if not u.roles:
            _apply_roles_to_user(u, ["user"])
        u.set_password(password)
        db.session.add(u)
        db.session.flush()
        record_entity_change_audit(
            action="admin.user.create",
            target_type="admin_user",
            target_id=u.id,
            actor_id=getattr(current_user, "id", None),
            after=_user_audit_snapshot(u),
            meta={"username": u.username, "source": "admin.api_users"},
            title=u.username,
            include_snapshots=True,
        )
        db.session.commit()
        clear_staff_assignment_cache()
        return jsonify({"success": True, "id": u.id})
    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        uid = data.get("id")
        u = db.session.get(User, uid)
        if u:
            before = _user_audit_snapshot(u)
            if "roles" in data:
                role_names = _normalize_role_names(data.get("roles"))
                _apply_roles_to_user(u, role_names)
            elif "role" in data:
                # legacy single role
                role_name = str(data.get("role") or "").strip().lower()
                if role_name:
                    _apply_roles_to_user(u, [role_name])

            if "is_active" in data:
                u.is_active = data["is_active"]
            if "display_name" in data:
                u.display_name = (data.get("display_name") or "").strip() or None
            if "staff_party_id" in data:
                u.staff_party_id = (data.get("staff_party_id") or "").strip() or None
            if "department" in data:
                u.department = (data.get("department") or "").strip() or None
            after = _user_audit_snapshot(u)
            changes = diff_snapshots(before, after)
            if changes:
                record_entity_change_audit(
                    action="admin.user.update",
                    target_type="admin_user",
                    target_id=u.id,
                    actor_id=getattr(current_user, "id", None),
                    changes=changes,
                    meta={"username": u.username, "source": "admin.api_users"},
                    title=u.username,
                )
            db.session.commit()
            clear_staff_assignment_cache()
        return jsonify({"success": True})
    rows = User.query.all()
    return jsonify(
        [
            {
                "id": r.id,
                "username": r.username,
                "email": r.email,
                "display_name": getattr(r, "display_name", None),
                "staff_party_id": getattr(r, "staff_party_id", None),
                "role": r.role,
                "roles": [role_obj.name for role_obj in (r.roles or [])],
                "is_active": r.is_active,
            }
            for r in rows
        ]
    )


@bp.route("/api/users/provision", methods=["POST"])
@login_required
@role_required("admin")
def api_user_provision():
    data = request.get_json(silent=True) or {}
    staff_code = (data.get("staff_code") or "").strip()
    if not staff_code:
        return jsonify({"success": False, "error": "staff_code is required"}), 400

    staff_party_id = (data.get("staff_party_id") or "").strip() or None
    is_active = bool(data.get("is_active", True))

    # Resolve staff_party_id from directory if missing.
    ps = None
    if staff_party_id:
        ps = db.session.get(PartyStaff, staff_party_id)
        if ps and (ps.staff_code or "").strip() and (ps.staff_code or "").strip() != staff_code:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "staff_code does not match staff_party_id",
                    }
                ),
                400,
            )
    else:
        ps = PartyStaff.query.filter(PartyStaff.staff_code == staff_code).first()
        if ps:
            staff_party_id = ps.party_id

    # Create or update a local account. Passwords can be set separately.
    u = User.query.filter_by(username=staff_code).first()
    created = False
    before = _user_audit_snapshot(u) if u else None
    if not u:
        u = User(username=staff_code)
        db.session.add(u)
        created = True

    if "email" in data:
        email = (data.get("email") or "").strip() or None
        if email:
            other = User.query.filter(func.lower(User.email) == email.lower()).first()
            if other and other.id != u.id:
                return jsonify({"success": False, "error": "email already exists"}), 400
        u.email = email

    if "display_name" in data:
        u.display_name = (data.get("display_name") or "").strip() or None

    role_names = data.get("roles")
    if role_names is None and "role" in data:
        role_names = [data.get("role")]
    normalized_role_names = []
    if isinstance(role_names, list):
        seen = set()
        for item in role_names:
            name = str(item or "").strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized_role_names.append(name)

    if normalized_role_names:
        from app.models.role import Role

        roles_objs = Role.query.filter(Role.name.in_(normalized_role_names)).all()
        u.roles = roles_objs
        if roles_objs:
            u.role = pick_primary_role_name([r.name for r in roles_objs], default="user")
        else:
            u.role = pick_primary_role_name(normalized_role_names, default="user")
    else:
        # Fallback if no roles submitted but we want a default
        if not u.roles:
            from app.models.role import Role

            default_role = Role.query.filter_by(name="user").first()
            if default_role:
                u.roles = [default_role]
                u.role = "user"
        elif u.roles:
            u.role = pick_primary_role_name([r.name for r in u.roles], default="user")

    u.is_active = is_active
    u.staff_party_id = staff_party_id

    db.session.flush()
    after = _user_audit_snapshot(u)
    changes = diff_snapshots(before or {}, after)
    if created or changes:
        record_entity_change_audit(
            action="admin.user.provision",
            target_type="admin_user",
            target_id=u.id,
            actor_id=getattr(current_user, "id", None),
            changes=changes if not created else None,
            after=after if created else None,
            meta={
                "username": u.username,
                "staff_code": staff_code,
                "staff_party_id": staff_party_id,
                "created": created,
                "source": "admin.api_user_provision",
            },
            title=u.username,
            include_snapshots=created,
        )
    db.session.commit()
    clear_staff_assignment_cache()
    return jsonify({"success": True, "id": u.id, "username": u.username})


@bp.route("/api/config", methods=["GET", "POST", "DELETE"])
@login_required
@role_required("admin")
def api_config():
    if request.method == "POST":
        raw_data = request.get_json(silent=True) or {}
        data: dict[str, object] = {}
        for k, v in raw_data.items():
            key = str(k or "").strip()
            if not key:
                continue
            data[key] = v
        data = expand_config_update_aliases(data)

        # Public ERP builds use local credentials; do not allow disabling the only
        # built-in login method.
        try:
            if "ALLOW_PASSWORD_LOGIN" in data:
                v = str(data.get("ALLOW_PASSWORD_LOGIN") or "").strip().lower()
                disabling = v in ("0", "false", "no", "off", "")
                if disabling:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "Password login cannot be disabled while public password access is required.",
                            }
                        ),
                        400,
                    )
        except (OSError, RuntimeError, TypeError) as exc:
            report_swallowed_exception(
                exc,
                context="admin.routes.system_config.guard_password_login",
                log_key="admin.routes.system_config.guard_password_login",
                log_window_seconds=300,
            )

        if RULE_REGISTRY_KEY in data:
            validation = validate_rule_registry_payload(data.get(RULE_REGISTRY_KEY))
            if not validation.get("valid"):
                return (
                    jsonify({"success": False, "error": "invalid_rule_registry", **validation}),
                    400,
                )

        if CASE_OUR_REF_NUMBERING_CONFIG_KEY in data:
            validation = validate_our_ref_numbering_config_payload(
                data.get(CASE_OUR_REF_NUMBERING_CONFIG_KEY)
            )
            if not validation.get("valid"):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "invalid_our_ref_numbering_config",
                            **validation,
                        }
                    ),
                    400,
                )

        if CASE_MENU_CONFIG_KEY in data:
            validation = validate_case_menu_config_payload(data.get(CASE_MENU_CONFIG_KEY))
            if not validation.get("valid"):
                return (
                    jsonify({"success": False, "error": "invalid_case_menu_config", **validation}),
                    400,
                )

        for k, v in data.items():
            key = str(k or "").strip()
            if not key:
                continue
            raw_value = "" if v is None else str(v)

            if _is_sensitive_config_key(key) and raw_value.strip() in ("", _MASKED_CONFIG_VALUE):
                # Avoid overwriting secrets with placeholders/blanks; use DELETE for clearing.
                continue

            conf = SystemConfig.query.filter_by(key=key).first()
            old_value = conf.value if conf else None
            if conf:
                conf.value = raw_value
            else:
                db.session.add(SystemConfig(key=key, value=raw_value))
            _record_config_audit(
                action="admin.config.update",
                key=key,
                old_value=old_value,
                new_value=raw_value,
                source="admin.api_config",
                incoming_sensitive_value=_is_sensitive_config_key(key),
            )
        db.session.commit()
        try:
            ConfigService.clear_cache()
        except (RuntimeError, ValueError):
            current_app.logger.warning("ConfigService cache clear skipped", exc_info=True)
        if CASE_MENU_CONFIG_KEY in data or "UNIFIED_FIELD_REGISTRY_JSON" in data:
            try:
                from app.services.case.case_parameter_service import CaseParameterService

                CaseParameterService.reload_if_changed()
            except (ImportError, RuntimeError, ValueError):
                current_app.logger.warning("Case field registry reload skipped", exc_info=True)
        clear_staff_assignment_cache()
        return jsonify({"success": True})

    if request.method == "DELETE":
        key = str(request.args.get("key") or "").strip()
        if not key:
            return jsonify({"error": "key is required"}), 400
        deleted_keys: set[str] = set()
        for target_key in expand_config_delete_aliases(key):
            conf = SystemConfig.query.filter_by(key=target_key).first()
            if conf:
                old_value = conf.value
                db.session.delete(conf)
                deleted_keys.add(target_key)
                _record_config_audit(
                    action="admin.config.delete",
                    key=target_key,
                    old_value=old_value,
                    new_value=None,
                    source="admin.api_config",
                )
        db.session.commit()
        try:
            ConfigService.clear_cache()
        except (RuntimeError, ValueError):
            current_app.logger.warning("ConfigService cache clear skipped", exc_info=True)
        if CASE_MENU_CONFIG_KEY in deleted_keys or "UNIFIED_FIELD_REGISTRY_JSON" in deleted_keys:
            try:
                from app.services.case.case_parameter_service import CaseParameterService

                CaseParameterService.reload_if_changed()
            except (ImportError, RuntimeError, ValueError):
                current_app.logger.warning("Case field registry reload skipped", exc_info=True)
        clear_staff_assignment_cache()
        return jsonify({"success": True})

    rows = SystemConfig.query.all()
    data = {}
    for r in rows:
        key = str(r.key or "")
        value = "" if r.value is None else r.value
        if _is_sensitive_config_key(key):
            data[key] = _MASKED_CONFIG_VALUE if str(value).strip() else ""
        else:
            data[key] = value

    apply_config_read_aliases(data)
    return jsonify(data)


@bp.route("/api/rule-registry/validate", methods=["POST"])
@login_required
@role_required("admin")
def api_rule_registry_validate():
    payload = request.get_json(silent=True)
    value = payload.get("value") if isinstance(payload, dict) and "value" in payload else payload
    validation = validate_rule_registry_payload(value)
    return jsonify({"success": bool(validation.get("valid")), **validation})


@bp.route("/api/rule-registry/preview", methods=["GET"])
@login_required
@role_required("admin")
def api_rule_registry_preview():
    return jsonify({"success": True, "preview": preview_rule_registry()})


@bp.route("/api/case-menu/validate", methods=["POST"])
@login_required
@role_required("admin")
def api_case_menu_validate():
    payload = request.get_json(silent=True)
    value = payload.get("value") if isinstance(payload, dict) and "value" in payload else payload
    validation = validate_case_menu_config_payload(value)
    return jsonify({"success": bool(validation.get("valid")), **validation})


@bp.route("/api/case-menu/preview", methods=["GET"])
@login_required
@role_required("admin")
def api_case_menu_preview():
    return jsonify({"success": True, "preview": preview_case_menu_config()})


# Default deadline settings
DEADLINE_SETTINGS_DEFAULTS = {
    "DEADLINE_REMINDER_DAYS": "30,14,7,1",  # Notice Send  (Deadline N )
    "DEADLINE_ANNUITY_REMINDER_DAYS": "60,30,14",  # Annuity Fee Notice
    "DEADLINE_EMAIL_ENABLED": "true",  # Email Notice active  ( )
    "DEADLINE_NOTIFICATION_ENABLED": "true",  # Notice active
    "DEADLINE_CALENDAR_SYNC_ENABLED": "false",
    "DEADLINE_AUTO_CLEANUP_ENABLED": "true",  # Auto  active
    # Annuity-related toggles (non-DEADLINE_* but managed in the same editor)
    "ANNUITY_ALLOW_FOREIGN": "false",  # OUT/INC Matter Renewal AutoCreate
    "ANNUITY_ALLOW_REG_FEE_PAID_FALLBACK": "false",  # Registration date   RegistrationPayment ()
    "ANNUITY_VISIBLE_CYCLE_COUNT": "2",
    "DEADLINE_DEFAULT_ANNUITY_START_YEAR": "4",  # Renewal Start (Domestic: Registration 1-3 Process  4 )
    "DEADLINE_ANNUITY_FULL_TERM": "true",  #  All Renewal Create
    "DEADLINE_ANNUITY_AUTOGEN_FUTURE_YEARS": "3",  # All Create OFF : Current Renewal + N Create
    "DEADLINE_ANNUITY_RENEWAL_NOTICE_DAYS": "60",  # Internal Notice Deadline(D-)
    "DEADLINE_ANNUITY_RENEWAL_OPEN_DAYS": "30",  # Internal Updated (D-)
    "DEADLINE_DEFAULT_PATENT_TERM": "20",  # Patent  Renewal
    "DEADLINE_DEFAULT_UTILITY_TERM": "10",  # Utility model  Renewal
    "DEADLINE_DEFAULT_DESIGN_TERM": "20",  # Design  Renewal
    "DEADLINE_TRADEMARK_REGISTRATION_PAYMENT_TERM_DEFAULT": "10",  # Trademark Registration Default Payment()
    "DEADLINE_TRADEMARK_SPLIT_PAYMENT_NOTICE_DAYS": "90",  # Trademark 5 Payment 2 Notice Deadline(D-)
    "DEADLINE_TRADEMARK_SPLIT_PAYMENT_OPEN_DAYS": "180",  # Trademark 5 Payment 2 (D-)
}


@bp.route("/api/deadline-settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def api_deadline_settings():
    """
    Deadline/Notification settings API.

    GET: Returns current settings with defaults
    POST: Updates settings
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        for key, value in data.items():
            if key.startswith(("DEADLINE_", "ANNUITY_")):
                conf = SystemConfig.query.filter_by(key=key).first()
                old_value = conf.value if conf else None
                new_value = str(value)
                SystemConfig.set_config(key, new_value)
                _record_config_audit(
                    action="admin.deadline_settings.update",
                    key=key,
                    old_value=old_value,
                    new_value=new_value,
                    source="admin.api_deadline_settings",
                )
        db.session.commit()
        return jsonify({"success": True})

    # GET - return settings with defaults
    settings = {}
    for key, default in DEADLINE_SETTINGS_DEFAULTS.items():
        settings[key] = SystemConfig.get_config(key, default)

    return jsonify(settings)


@bp.route("/api/staff", methods=["GET", "POST"])
@login_required
@role_required("admin")
def api_staff():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        staff_code = (data.get("staff_code") or "").strip()
        name_display = (data.get("name_display") or "").strip()
        dept = (data.get("dept") or "").strip()
        email = (data.get("email") or "").strip()
        active = 1 if bool(data.get("active", True)) else 0

        if not staff_code or not name_display:
            return (
                jsonify({"success": False, "error": "staff_code and name_display are required"}),
                400,
            )

        existing = PartyStaff.query.filter(PartyStaff.staff_code == staff_code).first()
        if existing:
            return jsonify({"success": False, "error": "staff_code already exists"}), 400

        # Create party + staff
        party_id = uuid.uuid4().hex
        p = Party(
            party_id=party_id,
            name_display=name_display,
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
        ps = PartyStaff(party_id=party_id, staff_code=staff_code, dept=dept or None, active=active)
        db.session.add(p)
        db.session.add(ps)

        # Best-effort: add email to party_contact if table exists.
        if email:
            try:
                db.session.execute(
                    text(
                        """
                        INSERT INTO party_contact(contact_id, party_id, contact_type, label, value)
                        VALUES(:contact_id, :party_id, 'email', 'work', :email)
                        """
                    ),
                    {"contact_id": uuid.uuid4().hex, "party_id": party_id, "email": email},
                )
            except SQLAlchemyError as exc:
                # Best-effort: contact sync should not block staff creation.
                report_swallowed_exception(
                    exc,
                    context="admin.routes.api_staff.insert_email_contact",
                    log_key="admin.routes.api_staff.insert_email_contact",
                    log_window_seconds=300,
                )

        # Link to local user if username matches.
        u = User.query.filter_by(username=staff_code).first()
        user_before = _user_audit_snapshot(u) if u else None
        if u and not getattr(u, "staff_party_id", None):
            u.staff_party_id = party_id
            if not (getattr(u, "display_name", None) or "").strip():
                u.display_name = name_display
            if email and not (u.email or "").strip():
                u.email = email
            u.department = dept or None

        db.session.flush()
        record_entity_change_audit(
            action="admin.staff.create",
            target_type="staff",
            actor_id=getattr(current_user, "id", None),
            after=_staff_audit_snapshot(ps, p),
            meta={"party_id": party_id, "staff_code": staff_code, "source": "admin.api_staff"},
            title=name_display,
            include_snapshots=True,
        )
        if u and user_before is not None:
            user_changes = diff_snapshots(user_before, _user_audit_snapshot(u))
            if user_changes:
                record_entity_change_audit(
                    action="admin.user.update",
                    target_type="admin_user",
                    target_id=u.id,
                    actor_id=getattr(current_user, "id", None),
                    changes=user_changes,
                    meta={
                        "username": u.username,
                        "staff_party_id": party_id,
                        "source": "admin.api_staff.link_user",
                    },
                    title=u.username,
                )
        db.session.commit()
        return jsonify({"success": True, "party_id": party_id})

    # GET
    # Include primary email if present.
    rows = (
        db.session.execute(
            text(
                """
                SELECT
                  ps.party_id AS party_id,
                  COALESCE(ps.staff_code, '') AS staff_code,
                  COALESCE(ps.dept, '') AS dept,
                  COALESCE(ps.active, 1) AS active,
                  COALESCE(p.name_display, '') AS name_display,
                  (
                    SELECT pc.value
                    FROM party_contact pc
                    WHERE pc.party_id = ps.party_id
                      AND pc.contact_type = 'email'
                    ORDER BY pc.contact_id ASC
                    LIMIT 1
                  ) AS email
                FROM party_staff ps
                JOIN party p ON p.party_id = ps.party_id
                ORDER BY p.name_display ASC, ps.staff_code ASC
                """
            )
        )
        .mappings()
        .all()
    )
    return jsonify([row_to_dict(r) for r in rows])


@bp.route("/api/staff/<party_id>", methods=["PATCH"])
@login_required
@role_required("admin")
def api_staff_update(party_id: str):
    data = request.get_json(silent=True) or {}
    name_display = (data.get("name_display") or "").strip()
    dept = (data.get("dept") or "").strip()
    email = (data.get("email") or "").strip()
    active = 1 if bool(data.get("active", True)) else 0

    ps = db.session.get(PartyStaff, party_id)
    p = db.session.get(Party, party_id)
    if not ps or not p:
        return jsonify({"success": False, "error": "not found"}), 404
    staff_before = _staff_audit_snapshot(ps, p)
    before = _staff_audit_snapshot(ps, p)

    if name_display:
        p.name_display = name_display
    ps.dept = dept or None
    ps.active = active

    if email:
        try:
            db.session.execute(
                text(
                    """
                    INSERT INTO party_contact(contact_id, party_id, contact_type, label, value)
                    SELECT :contact_id, :party_id, 'email', 'work', :email
                    WHERE NOT EXISTS (
                      SELECT 1 FROM party_contact pc
                      WHERE pc.party_id = :party_id
                        AND pc.contact_type = 'email'
                        AND lower(pc.value) = lower(:email)
                    )
                    """
                ),
                {"contact_id": uuid.uuid4().hex, "party_id": party_id, "email": email},
            )
        except SQLAlchemyError as exc:
            # Best-effort: contact sync should not block staff updates.
            report_swallowed_exception(
                exc,
                context="admin.routes.api_staff_update.insert_email_contact",
                log_key="admin.routes.api_staff_update.insert_email_contact",
                log_window_seconds=300,
            )

    # Sync linked user if any.
    if ps.staff_code:
        u = User.query.filter_by(username=ps.staff_code).first()
        if u:
            user_before = _user_audit_snapshot(u)
            u.staff_party_id = party_id
            if name_display:
                u.display_name = u.display_name or name_display
            if email and not (u.email or "").strip():
                u.email = email
            u.department = dept or None
            user_changes = diff_snapshots(user_before, _user_audit_snapshot(u))
            if user_changes:
                record_entity_change_audit(
                    action="admin.user.update",
                    target_type="admin_user",
                    target_id=u.id,
                    actor_id=getattr(current_user, "id", None),
                    changes=user_changes,
                    meta={
                        "username": u.username,
                        "staff_party_id": party_id,
                        "source": "admin.api_staff_update.link_user",
                    },
                    title=u.username,
                )

    after = _staff_audit_snapshot(ps, p)
    changes = diff_snapshots(before, after)
    if changes:
        record_entity_change_audit(
            action="admin.staff.update",
            target_type="staff",
            actor_id=getattr(current_user, "id", None),
            changes=changes,
            meta={
                "party_id": party_id,
                "staff_code": ps.staff_code,
                "source": "admin.api_staff_update",
            },
            title=p.name_display,
        )
    db.session.commit()
    return jsonify({"success": True})


@bp.route("/api/staff/<party_id>/work-summary", methods=["GET"])
@login_required
@role_required("admin")
def api_staff_work_summary(party_id: str):
    """
    Get summary of all work assigned to a staff member.
    Returns counts of pending docket items, worklogs, case assignments, and communications.
    """
    ps = db.session.get(PartyStaff, party_id)
    if not ps:
        return jsonify({"success": False, "error": "not found"}), 404

    # Count pending docket_items (open = NULL/blank done_date)
    pending_dockets = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM docket_item
            WHERE owner_staff_party_id = :party_id
            AND (done_date IS NULL OR TRIM(done_date) = '')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    # Count active worklogs (pending or in_progress)
    active_worklogs = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM work_logs
            WHERE owner_staff_party_id = :party_id
            AND status IN ('pending', 'in_progress')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    # Count matter_staff_assignments
    try:
        assigned_cases = (
            db.session.execute(
                text(
                    """
                SELECT COUNT(*) as cnt FROM matter_staff_assignment
                WHERE staff_party_id = :party_id
            """
                ),
                {"party_id": party_id},
            ).scalar()
            or 0
        )
    except (ProgrammingError, OperationalError):
        assigned_cases = 0

    # Count communications (owner or author)
    communications = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM communication
            WHERE (owner_staff_party_id = :party_id OR author_staff_party_id = :party_id)
            AND (done_date IS NULL OR TRIM(done_date) = '')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    total = pending_dockets + active_worklogs + assigned_cases + communications
    can_delete = total == 0

    return jsonify(
        {
            "success": True,
            "party_id": party_id,
            "pending_dockets": pending_dockets,
            "active_worklogs": active_worklogs,
            "assigned_cases": assigned_cases,
            "communications": communications,
            "total": total,
            "can_delete": can_delete,
        }
    )


@bp.route("/api/staff/<party_id>/reassign", methods=["POST"])
@login_required
@role_required("admin")
def api_staff_reassign(party_id: str):
    """
    Reassign all work from one staff member to another.
    Updates docket_item, work_logs, matter_staff_assignment, and communication tables.
    """
    data = request.get_json(silent=True) or {}
    target_party_id = (data.get("target_party_id") or "").strip()

    if not target_party_id:
        return jsonify({"success": False, "error": "target_party_id is required"}), 400

    # Verify source staff exists
    source_ps = db.session.get(PartyStaff, party_id)
    if not source_ps:
        return jsonify({"success": False, "error": "source staff not found"}), 404

    # Verify target staff exists
    target_ps = db.session.get(PartyStaff, target_party_id)
    if not target_ps:
        return jsonify({"success": False, "error": "target staff not found"}), 404

    if party_id == target_party_id:
        return jsonify({"success": False, "error": "source and target cannot be the same"}), 400

    counts = {}

    try:
        docket_rows = (
            db.session.execute(
                text(
                    """
                    SELECT docket_id, matter_id, COALESCE(name_free, name_ref, '') AS name
                      FROM docket_item
                     WHERE owner_staff_party_id = :source_id
                       AND (done_date IS NULL OR TRIM(done_date) = '')
                       AND COALESCE(is_deleted, false) = false
                """
                ),
                {"source_id": party_id},
            )
            .mappings()
            .all()
        )
        # 1. Update docket_item.owner_staff_party_id (pending items only)
        result = db.session.execute(
            text(
                """
                UPDATE docket_item
                SET owner_staff_party_id = :target_id
                WHERE owner_staff_party_id = :source_id
                AND (done_date IS NULL OR TRIM(done_date) = '')
            """
            ),
            {"source_id": party_id, "target_id": target_party_id},
        )
        counts["docket_items"] = result.rowcount

        # 2. Update work_logs.owner_staff_party_id (pending/in_progress only)
        result = db.session.execute(
            text(
                """
                UPDATE work_logs
                SET owner_staff_party_id = :target_id
                WHERE owner_staff_party_id = :source_id
                AND status IN ('pending', 'in_progress')
            """
            ),
            {"source_id": party_id, "target_id": target_party_id},
        )
        counts["work_logs"] = result.rowcount

        # 3. Update matter_staff_assignment.staff_party_id
        try:
            result = db.session.execute(
                text(
                    """
                    UPDATE matter_staff_assignment
                    SET staff_party_id = :target_id
                    WHERE staff_party_id = :source_id
                """
                ),
                {"source_id": party_id, "target_id": target_party_id},
            )
            counts["case_assignments"] = result.rowcount
        except (ProgrammingError, OperationalError):
            counts["case_assignments"] = 0

        # 4. Update communication.owner_staff_party_id (pending only)
        result = db.session.execute(
            text(
                """
                UPDATE communication
                SET owner_staff_party_id = :target_id
                WHERE owner_staff_party_id = :source_id
                AND (done_date IS NULL OR TRIM(done_date) = '')
            """
            ),
            {"source_id": party_id, "target_id": target_party_id},
        )
        counts["communications_owner"] = result.rowcount

        # 5. Update communication.author_staff_party_id (pending only)
        result = db.session.execute(
            text(
                """
                UPDATE communication
                SET author_staff_party_id = :target_id
                WHERE author_staff_party_id = :source_id
                AND (done_date IS NULL OR TRIM(done_date) = '')
            """
            ),
            {"source_id": party_id, "target_id": target_party_id},
        )
        counts["communications_author"] = result.rowcount

        source_party = db.session.get(Party, party_id)
        target_party = db.session.get(Party, target_party_id)
        total_reassigned = sum(counts.values())
        record_entity_change_audit(
            action="admin.staff.reassign",
            target_type="staff",
            actor_id=getattr(current_user, "id", None),
            changes={
                "owner_staff_party_id": {
                    "from": party_id,
                    "to": target_party_id,
                }
            },
            meta={
                "source_party_id": party_id,
                "target_party_id": target_party_id,
                "source_staff_code": getattr(source_ps, "staff_code", None),
                "target_staff_code": getattr(target_ps, "staff_code", None),
                "source_name": getattr(source_party, "name_display", None),
                "target_name": getattr(target_party, "name_display", None),
                "counts": counts,
                "total_reassigned": total_reassigned,
                "source": "admin.api_staff_reassign",
            },
            title="Staff task reassignment",
        )
        for row in docket_rows:
            docket_id = str(row.get("docket_id") or "")
            record_entity_change_audit(
                action="docket.update",
                target_type="docket_item",
                actor_id=getattr(current_user, "id", None),
                changes={"owner_staff_party_id": {"from": party_id, "to": target_party_id}},
                meta={
                    "docket_id": docket_id,
                    "matter_id": str(row.get("matter_id") or ""),
                    "name": str(row.get("name") or ""),
                    "source": "admin.api_staff_reassign",
                    "source_party_id": party_id,
                    "target_party_id": target_party_id,
                },
                title=str(row.get("name") or "Deadline"),
            )
        db.session.commit()

        return jsonify(
            {
                "success": True,
                "message": f"{total_reassigned} task(s) reassigned.",
                "counts": counts,
                "total_reassigned": total_reassigned,
            }
        )

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


def _automation_job_change_set_id(job) -> str:
    payload = getattr(job, "payload", None) or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        return ""
    meta = payload.get("_meta") or {}
    if isinstance(meta, dict):
        change_set_id = str(meta.get("change_set_id") or "").strip()
        if change_set_id:
            return change_set_id
    return str(payload.get("change_set_id") or "").strip()


def _json_datetime(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _automation_job_row(job, *, now: datetime) -> dict[str, Any]:
    from app.ops.durable_queue import durable_job_retry_diagnostics

    diagnostics = durable_job_retry_diagnostics(job, now=now)
    return {
        "id": job.id,
        "queue": job.queue,
        "task": job.task,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_at": _json_datetime(job.run_at),
        "updated_at": _json_datetime(job.updated_at),
        "last_error": job.last_error,
        "retry_state": diagnostics.get("retry_state") or "",
        "retry_state_label": diagnostics.get("retry_state_label") or "",
        "retry_cause": diagnostics.get("retry_cause") or "",
        "next_retry_at": _json_datetime(diagnostics.get("next_retry_at")),
        "retries_remaining": diagnostics.get("retries_remaining"),
    }


@bp.route("/api/automation/changeset/<change_set_id>/sync_status")
@login_required
@role_required("admin")
def automation_changeset_sync_status(change_set_id: str):
    from app.models.email_automation import AutomationChangeSet
    from app.ops.models import DurableJob

    normalized_id = str(change_set_id or "").strip()
    change_set = AutomationChangeSet.query.get(normalized_id)
    if change_set is None:
        return jsonify({"success": False, "error": "not_found"}), 404

    candidates = (
        DurableJob.query.filter(DurableJob.task == "deferred.sync")
        .order_by(DurableJob.created_at.desc(), DurableJob.id.desc())
        .limit(500)
        .all()
    )
    jobs = [job for job in candidates if _automation_job_change_set_id(job) == normalized_id]
    open_statuses = {"queued", "running", "failed"}
    has_open_jobs = any(str(job.status or "").strip().lower() in open_statuses for job in jobs)
    status = "pending"
    if bool(change_set.applied) and not has_open_jobs:
        status = "completed"

    now = datetime.utcnow()
    return jsonify(
        {
            "success": True,
            "change_set_id": normalized_id,
            "status": status,
            "applied": bool(change_set.applied),
            "jobs": [_automation_job_row(job, now=now) for job in jobs],
        }
    )


@bp.route("/api/staff/<party_id>", methods=["DELETE"])
@login_required
@role_required("admin")
def api_staff_delete(party_id: str):
    """
    Delete a staff member.
    Only allowed if all work has been reassigned (total pending work = 0).
    """
    ps = db.session.get(PartyStaff, party_id)
    p = db.session.get(Party, party_id)
    if not ps or not p:
        return jsonify({"success": False, "error": "not found"}), 404

    # Check if there's any remaining work
    pending_dockets = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM docket_item
            WHERE owner_staff_party_id = :party_id
            AND (done_date IS NULL OR TRIM(done_date) = '')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    active_worklogs = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM work_logs
            WHERE owner_staff_party_id = :party_id
            AND status IN ('pending', 'in_progress')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    try:
        assigned_cases = (
            db.session.execute(
                text(
                    """
                SELECT COUNT(*) as cnt FROM matter_staff_assignment
                WHERE staff_party_id = :party_id
            """
                ),
                {"party_id": party_id},
            ).scalar()
            or 0
        )
    except (ProgrammingError, OperationalError):
        assigned_cases = 0

    communications = (
        db.session.execute(
            text(
                """
            SELECT COUNT(*) as cnt FROM communication
            WHERE (owner_staff_party_id = :party_id OR author_staff_party_id = :party_id)
            AND (done_date IS NULL OR TRIM(done_date) = '')
        """
            ),
            {"party_id": party_id},
        ).scalar()
        or 0
    )

    total = pending_dockets + active_worklogs + assigned_cases + communications

    if total > 0:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"{total} task(s) still exist. Resolve those tasks first.",
                    "remaining": {
                        "pending_dockets": pending_dockets,
                        "active_worklogs": active_worklogs,
                        "assigned_cases": assigned_cases,
                        "communications": communications,
                    },
                }
            ),
            400,
        )

    staff_before = _staff_audit_snapshot(ps, p)

    try:
        # Unlink user accounts associated with this staff
        users = User.query.filter_by(staff_party_id=party_id).all()
        user_before_by_id = {u.id: _user_audit_snapshot(u) for u in users}
        for u in users:
            u.staff_party_id = None
            u.is_active = False
            user_changes = diff_snapshots(user_before_by_id.get(u.id, {}), _user_audit_snapshot(u))
            if user_changes:
                record_entity_change_audit(
                    action="admin.user.update",
                    target_type="admin_user",
                    target_id=u.id,
                    actor_id=getattr(current_user, "id", None),
                    changes=user_changes,
                    meta={
                        "username": u.username,
                        "staff_party_id": party_id,
                        "source": "admin.api_staff_delete.unlink_user",
                    },
                    title=u.username,
                )

        # Delete party_contact entries
        from app.models.party import PartyContact

        PartyContact.query.filter(PartyContact.party_id == party_id).delete(
            synchronize_session=False
        )

        # Delete party_staff
        db.session.delete(ps)

        # Delete party
        db.session.delete(p)

        staff_after = dict(staff_before)
        staff_after["is_deleted"] = True
        record_entity_change_audit(
            action="admin.staff.delete",
            target_type="staff",
            actor_id=getattr(current_user, "id", None),
            before=staff_before,
            after=staff_after,
            meta={
                "party_id": party_id,
                "staff_code": staff_before.get("staff_code"),
                "source": "admin.api_staff_delete",
                "disabled_user_ids": [u.id for u in users],
            },
            title=str(staff_before.get("name_display") or party_id),
            include_snapshots=True,
        )
        db.session.commit()
        clear_staff_assignment_cache()

        return jsonify({"success": True, "message": "Staff Delete."})

    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
