from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from flask import abort, current_app, g, jsonify, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.blueprints.api import bp
from app.blueprints.api.money_utils import (
    _clamp_money,
    _money_to_float,
    _quantize_money,
    _safe_float,
    _to_decimal,
)
from app.blueprints.api.routes_helpers import normalize_date_text as _normalize_date_text
from app.blueprints.api.routes_helpers import parse_date as _parse_date
from app.blueprints.api.routes_helpers import parse_date_only as _parse_date_only
from app.blueprints.api.routes_helpers import safe_int as _safe_int
from app.blueprints.billing_invoices.db import DB_ERRORS
from app.blueprints.case.helpers import _build_case_list_extras, _update_basic_matter_info
from app.extensions import db
from app.models.case_audit_log import CaseAuditLog
from app.models.client import Client
from app.models.ip_records import (
    DocketItem,
    ExternalInvoiceCaseMap,
    Matter,
    MatterCustomField,
    MatterFileAsset,
    MatterMemo,
    MatterStaffAssignment,
    LegacyExpense,
    LegacyExpensePayment,
    VMatterOverview,
)
from app.models.user import User
from app.models.workflow import Workflow
from app.models.worklog import WorkLog
from app.services.audit.entity_audit import (
    diff_snapshots,
    record_entity_change_audit,
    snapshot_attrs,
)
from app.services.billing.case_invoice_service import fetch_case_invoices
from app.services.billing.invoice_bridge import (
    InvoiceBridgeError,
    fetch_external_invoice_links_for_case,
)
from app.services.billing.invoice_matter_link_usecase import InvoiceMatterLinkUseCase
from app.services.case.canonical_field_service import (
    get_namespace_for_matter,
    upsert_case_flat_index,
)
from app.services.case.case_kind import resolve_public_case_kind_for_matter
from app.services.case.case_numbering import NextOurRefError, generate_next_our_ref
from app.services.case.helpers_files import (
    _attach_image_file_asset,
    _is_allowed_image_upload,
    _load_linked_file_asset,
)
from app.services.case.status_task_cleanup import apply_case_status_side_effects
from app.services.client.client_party_sync import ensure_clients_synced_from_party
from app.services.core.config_service import ConfigService
from app.services.files.file_classification import (
    DOC_TYPE_LABELS,
    classify_doc_type,
    is_previewable,
)
from app.services.storage.file_asset_service import get_file_asset_service
from app.services.uspto.uspto_practice import analyze_uspto_document_text
from app.services.workflow.assignment_requests import (
    sync_assignment_requests_for_changed_roles,
    workflow_assignment_state,
)
from app.services.workflow.task_sync import persist_manual_workflow_assignment_override
from app.utils.annuity_deadline_routing import calendar_endpoint_for_docket
from app.utils.docket_dates import effective_due_for_work, effective_due_text_expr
from app.utils.docket_visibility import visible_on_or_before
from app.utils.error_logging import report_swallowed_exception
from app.utils.network_access import check_admin_or_internal_access
from app.utils.permissions import (
    can_access_matter,
    check_permission,
    matter_action,
    resolve_matter_action,
)
from app.utils.search import sqlalchemy_contains_query
from app.utils.status_red_visibility import is_non_action_status_red_label
from app.utils.task_assignment_rules import is_manager_only_notice
from app.utils.workflow_semantics import derive_workflow_category

_INVOICE_CONN_ERRORS = DB_ERRORS + (AttributeError, RuntimeError, TypeError)
_DOCKET_AUDIT_FIELDS = (
    "docket_id",
    "matter_id",
    "category",
    "name_ref",
    "name_free",
    "due_date",
    "extended_due_date",
    "visible_from_date",
    "done_date",
    "owner_staff_party_id",
    "memo",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "delete_reason",
)

# Audit list default limit is implemented in routes_audits via request.args.get("limit"), 5.


def _normalize_app_no(v: str | None) -> str | None:
    if not v:
        return None
    return "".join(ch for ch in v if ch.isdigit())


def _app_no_min_match_length() -> int:
    return int(ConfigService.get_int("APP_NO_MIN_MATCH_LENGTH", 8, min_value=1) or 8)


def _find_matter_by_app_no(app_no: str):
    app_no_norm = _normalize_app_no(app_no)
    if not app_no_norm:
        return None
    if len(app_no_norm) < _app_no_min_match_length():
        return None
    try:
        from app.services.matching.auto_match_service import find_case_by_app_no

        matter = find_case_by_app_no(app_no_norm)
        if matter:
            return matter
    except (
        AttributeError,
        ImportError,
        RuntimeError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as exc:
        report_swallowed_exception(
            exc,
            context="api.routes._find_matter_by_app_no",
            log_key="api.routes._find_matter_by_app_no",
            log_window_seconds=300,
        )
    return None


@bp.route("/cases", methods=["GET"])
@login_required
def api_cases_list():
    """Minimal cases list endpoint (used by tests and lightweight integrations)."""
    limit = _safe_int(request.args.get("limit"), 200) or 200
    limit = max(1, min(limit, 500))

    staff_pid = (getattr(current_user, "staff_party_id", "") or "").strip()

    if check_permission("manage_case"):
        q = Matter.query
    elif staff_pid:
        q = Matter.query.join(
            MatterStaffAssignment,
            MatterStaffAssignment.matter_id == Matter.matter_id,
        ).filter(MatterStaffAssignment.staff_party_id == staff_pid)
    else:
        return jsonify([])

    try:
        q = q.filter(or_(Matter.is_deleted.is_(None), Matter.is_deleted.is_(False)))
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.api_cases_list.filter_active",
            log_key="api.routes.api_cases_list.filter_active",
            log_window_seconds=300,
        )

    rows = q.limit(limit).all()
    return jsonify(
        [
            {
                "matter_id": str(m.matter_id),
                "our_ref": m.our_ref,
                "right_name": m.right_name,
                "status_red": m.status_red,
                "status_blue": m.status_blue,
            }
            for m in rows
        ]
    )


def _derive_notice_workflow_category(
    *,
    matter_id: str,
    doc_title: str,
    assignee_id: int | None,
    attorney_assignee_id: int | None,
    manager_assignee_id: int | None,
    source: str | None = None,
) -> str:
    if manager_assignee_id and is_manager_only_notice(
        name_ref=None,
        name_free=doc_title,
        category=None,
        source=source,
    ):
        return "MGMT"
    return derive_workflow_category(
        case_id=matter_id,
        handler_id=assignee_id,
        attorney_id=attorney_assignee_id,
        manager_id=manager_assignee_id,
        hint_name_free=doc_title,
        source=source,
    )


def _active_filter(model):
    return or_(model.is_deleted == False, model.is_deleted.is_(None))  # noqa: E712


def _report_db_operation_error(exc: Exception, *, context: str) -> None:
    report_swallowed_exception(
        exc,
        context=context,
        log_key=context,
        log_window_seconds=300,
    )


def _rollback_conn_safely(conn, *, context: str) -> None:
    try:
        conn.rollback()
    except _INVOICE_CONN_ERRORS as rollback_exc:
        _report_db_operation_error(rollback_exc, context=f"{context}.rollback")


def _close_conn_safely(conn, *, context: str) -> None:
    try:
        conn.close()
    except _INVOICE_CONN_ERRORS as close_exc:
        _report_db_operation_error(close_exc, context=f"{context}.close")


def _cleanup_created_invoice(*, get_db_fn, invoice_id: int) -> None:
    try:
        cleanup_conn = get_db_fn()
        try:
            cleanup_conn.execute("DELETE FROM invoices WHERE id=?", (int(invoice_id),))
            cleanup_conn.commit()
        except _INVOICE_CONN_ERRORS:
            _rollback_conn_safely(cleanup_conn, context="api.routes.case_invoice_create.cleanup")
            raise
        finally:
            _close_conn_safely(cleanup_conn, context="api.routes.case_invoice_create.cleanup")
    except _INVOICE_CONN_ERRORS as cleanup_exc:
        current_app.logger.exception(
            "Invoice cleanup failed (invoice_id=%s): %s",
            invoice_id,
            cleanup_exc,
        )


def _invoice_open_url(invoice_id: int | str) -> str:
    base_url = current_app.config.get(
        "INVOICE_MODULE_VIEW_BASE_URL",
        "/accounting/invoice-system/invoices",
    ).rstrip("/")
    return f"{base_url}/{invoice_id}"


def _recalc_expense_totals(expense: LegacyExpense) -> None:
    paid_total = (
        db.session.query(func.sum(LegacyExpensePayment.sent_amount))
        .filter(LegacyExpensePayment.expense_id == expense.expense_id)
        .filter(_active_filter(LegacyExpensePayment))
        .scalar()
    )
    paid_total_dec = _to_decimal(paid_total)
    requested_dec = _to_decimal(expense.requested_total)
    outstanding_dec = _clamp_money(_quantize_money(requested_dec - paid_total_dec))
    if outstanding_dec < 0:
        outstanding_dec = _to_decimal(0)

    expense.remit_total = _money_to_float(paid_total_dec)
    expense.outstanding_amount = _money_to_float(outstanding_dec)
    expense.status = "Done" if outstanding_dec <= 0 else ""


def _reserve_next_installment_no(expense_id: str) -> int:
    try:
        dialect = (db.session.get_bind().dialect.name or "").lower()
    except (AttributeError, RuntimeError, SQLAlchemyError):
        dialect = ""
    if dialect.startswith("postgres"):
        # Lock the parent expense row to prevent concurrent installment number allocation.
        (
            db.session.query(LegacyExpense.expense_id)
            .filter(LegacyExpense.expense_id == str(expense_id))
            .with_for_update()
            .first()
        )
    max_no = (
        db.session.query(func.max(LegacyExpensePayment.installment_no))
        .filter(LegacyExpensePayment.expense_id == str(expense_id))
        .scalar()
        or 0
    )
    next_no = int(max_no) + 1
    while (
        LegacyExpensePayment.query.filter_by(
            expense_id=str(expense_id), installment_no=next_no
        ).first()
        is not None
    ):
        next_no += 1
    return next_no


def _payable_status_label(status: str | None) -> str:
    if not status:
        return ""
    if status == "PAID":
        return "Done"
    if status == "UNPAID":
        return ""
    return status


def _ledger_status_match(item: dict, status_filter: str) -> bool:
    if not status_filter:
        return True
    sf = str(status_filter).strip().lower()
    if not sf:
        return True
    status = str(item.get("status") or "").strip().lower()
    item_type = str(item.get("type") or "").strip().upper()
    if item_type == "PAYABLE":
        if sf in ("", "unpaid"):
            return status in ("unpaid", "")
        if sf in ("Done", "paid"):
            return status in ("paid", "Done")
        return status == sf
    if sf in ("overdue", "overdue"):
        return status == "overdue"
    if sf in ("outstanding", "outstanding", "unpaid"):
        return status in ("sent", "partial")
    if sf in ("Done", "paid"):
        return status == "paid"
    return status == sf


def _case_custom_namespaces() -> list[str]:
    return [
        "domestic_patent",
        "domestic_design",
        "domestic_trademark",
        "incoming_patent",
        "incoming_design",
        "incoming_trademark",
        "outgoing_patent",
        "outgoing_design",
        "outgoing_trademark",
        "pct",
        "litigation",
    ]


def _update_matter_custom_fields(matter_id: str, updates: dict) -> None:
    """Update matter-type-specific custom fields. Only updates the first canonical namespace row."""
    namespaces = _case_custom_namespaces()
    rows = (
        MatterCustomField.query.filter(MatterCustomField.matter_id == matter_id)
        .filter(MatterCustomField.namespace.in_(namespaces))
        .all()
    )
    if rows:
        row = rows[0]
        data = dict(row.data or {})
        data.update(updates)
        row.data = data


def _update_case_custom_fields(case_id: str, updates: dict) -> None:
    """Legacy compatibility wrapper; new code should pass matter_id."""
    _update_matter_custom_fields(str(case_id), updates)


def _can_edit_matter(matter_id: str) -> bool:
    return can_access_matter(current_user, str(matter_id), action="edit_case")


def _can_edit_case(case_id: str) -> bool:
    """Legacy compatibility wrapper; new code should call _can_edit_matter."""
    return _can_edit_matter(str(case_id))


def _log_case_audit(
    *,
    case_id: str,
    field: str,
    old_value: Any,
    new_value: Any,
    action: str = "USER",
) -> None:
    """
    Best-effort CaseAuditLog writer.

    Many API endpoints treat audit logging as non-critical; keep this helper resilient so a
    logging failure doesn't break primary workflows.
    """
    try:
        from app.services.case.case_audit_service import record_case_audit

        record_case_audit(
            case_id=str(case_id),
            field_name=(field or "event").strip(),
            action=(action or "USER").strip().upper(),
            actor_user_id=getattr(current_user, "id", None),
            old_value=old_value,
            new_value=new_value,
            request_id=getattr(g, "request_id", None),
        )
    except (AttributeError, ImportError, RuntimeError, SQLAlchemyError) as exc:
        report_swallowed_exception(
            exc,
            context="api.routes._log_case_audit",
            log_key="api.routes._log_case_audit",
            log_window_seconds=300,
        )


def _docket_audit_snapshot(di: DocketItem) -> dict[str, Any]:
    return snapshot_attrs(di, _DOCKET_AUDIT_FIELDS)


def _docket_audit_meta(di: DocketItem, *, source: str) -> dict[str, Any]:
    return {
        "docket_id": str(getattr(di, "docket_id", "") or ""),
        "matter_id": str(getattr(di, "matter_id", "") or ""),
        "name": (di.name_free or di.name_ref or "").strip(),
        "source": source,
    }


@bp.route("/cases/<string:matter_id>/registry-image", methods=["POST"])
@login_required
def case_registry_image_update(matter_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403

    matter = Matter.query.get_or_404(matter_id)
    namespace = get_namespace_for_matter(matter)
    if namespace not in {
        "domestic_design",
        "incoming_design",
        "outgoing_design",
        "domestic_trademark",
        "incoming_trademark",
        "outgoing_trademark",
    }:
        return jsonify({"error": "unsupported_case_type"}), 400

    image_file = request.files.get("image_file")
    clear = str(request.form.get("clear") or "").strip().lower() in ("1", "true", "y", "yes")

    row = MatterCustomField.query.filter_by(matter_id=str(matter_id), namespace=namespace).first()
    if not row:
        row = MatterCustomField(matter_id=str(matter_id), namespace=namespace, data={})
        db.session.add(row)
    data = dict(row.data or {})

    if clear:
        old_img = (data.get("image") or "").strip()
        data["image"] = ""
        row.data = data
        _log_case_audit(
            case_id=matter_id,
            field="registry_image",
            old_value=old_img,
            new_value="",
            action="UPLOAD",
        )
        db.session.commit()
        return jsonify({"ok": True, "cleared": True, "image": ""})

    if not image_file or not (image_file.filename or "").strip():
        return jsonify({"error": "image_file required"}), 400
    if not _is_allowed_image_upload(image_file):
        return jsonify({"error": "invalid_image"}), 400

    try:
        old_img = (data.get("image") or "").strip()
        _attach_image_file_asset(matter_id=str(matter_id), file=image_file, data=data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    row.data = data
    new_img = (data.get("image") or "").strip()
    _log_case_audit(
        case_id=matter_id,
        field="registry_image",
        old_value=old_img,
        new_value=new_img,
        action="UPLOAD",
    )
    db.session.commit()

    file_asset_id = (data.get("image") or "").strip()
    asset = _load_linked_file_asset(matter_id=str(matter_id), file_asset_id=file_asset_id)
    preview_url = (
        url_for(
            "case_work.download_file_asset",
            case_id=matter_id,
            file_asset_id=file_asset_id,
            inline=1,
        )
        if file_asset_id
        else ""
    )
    download_url = (
        url_for(
            "case_work.download_file_asset",
            case_id=matter_id,
            file_asset_id=file_asset_id,
        )
        if file_asset_id
        else ""
    )

    return jsonify(
        {
            "ok": True,
            "matter_id": str(matter_id),
            "image": file_asset_id,
            "preview_url": preview_url,
            "download_url": download_url,
            "original_name": (asset or {}).get("original_name") if asset else "",
            "mime_type": (asset or {}).get("mime_type") if asset else "",
        }
    )


def _require_matter_access(matter_id: str, action: str | None = None) -> None:
    act = action
    if not act:
        try:
            view_fn = current_app.view_functions.get(request.endpoint)
            act = getattr(view_fn, "_matter_action", None)
        except (AttributeError, RuntimeError):
            act = None
    if not act:
        act = resolve_matter_action(request)
    if not can_access_matter(current_user, str(matter_id), action=act):
        abort(403, "forbidden")


@bp.route("/parse/uspto", methods=["POST"])
@login_required
def parse_uspto_document():
    payload = request.get_json(silent=True) or {}
    filename = (
        request.form.get("filename") or payload.get("filename") or ""
    ).strip()
    text_value = (request.form.get("text") or payload.get("text") or "").strip()

    if not text_value and "file" in request.files:
        upload = request.files["file"]
        filename = filename or (getattr(upload, "filename", "") or "")
        try:
            text_value = _read_uspto_upload_text(upload, filename=filename)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if not text_value:
        return jsonify({"error": "text or a PDF/TXT file is required"}), 400

    analysis = analyze_uspto_document_text(text_value, filename=filename)
    analysis_payload = analysis.to_dict()
    deadline = analysis_payload.get("deadline") or {}
    fields = analysis_payload.get("fields") or {}
    return jsonify(
        {
            "ok": True,
            "status": "analyzed",
            "doc_name": analysis.doc_type,
            "task_type": analysis.task_type,
            "app_no": fields.get("app_no") or "",
            "due_date": deadline.get("due_date") or "",
            "confidence": analysis.confidence,
            "warnings": list(analysis.warnings),
            "analysis": analysis_payload,
        }
    )


def _read_uspto_upload_text(upload, *, filename: str = "") -> str:
    ext = (Path(filename).suffix or "").lower()
    raw = upload.read()
    if not raw:
        raise ValueError("uploaded file is empty")
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("uploaded file is too large")

    if ext == ".pdf":
        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(raw), strict=False)
            chunks: list[str] = []
            for page in reader.pages[:8]:
                chunks.append(page.extract_text() or "")
            return "\n".join(chunks).strip()
        except Exception as exc:
            raise ValueError(f"PDF text extraction failed: {type(exc).__name__}") from exc

    if ext not in {".txt", ".text", ""}:
        raise ValueError("only USPTO PDF or text uploads are supported")

    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


@bp.route("/users/list")
@login_required
def users_list():
    allowed, reason = check_admin_or_internal_access()
    if not allowed:
        message = " ."
        if reason == "blocked_country":
            message = "Access from this country is blocked."
        return jsonify({"error": message}), 403
    try:
        from app.services.core.staff_options import build_staff_assignment_lists

        users = build_staff_assignment_lists().get("all_users") or []
    except (AttributeError, ImportError, RuntimeError, SQLAlchemyError):
        users = []
    return jsonify(
        [{"id": u.id, "username": u.username, "email": u.email, "role": u.role} for u in users]
    )


@bp.route("/cases/next_ref")
@login_required
def cases_next_ref():
    from app.models.system_config import SystemConfig
    from app.services.case.case_numbering import (
        NextOurRefError,
        _build_our_ref_scheme,
        _compute_max_our_ref_seq_from_refs,
    )
    from app.utils.api_response import api_response
    from app.utils.policy_sql import policy_text as text

    if not check_permission("manage_case"):
        return api_response(
            ok=False,
            error="forbidden",
            message="forbidden",
            status=403,
        )

    ctype = (request.args.get("type") or "").upper()
    division = (request.args.get("division") or "").upper()  # DOM/INC/OUT
    country = (request.args.get("country") or "US").upper()

    try:
        try:
            scheme = _build_our_ref_scheme(division=division, matter_type=ctype, country=country)
            legacy_key = f"case_ref_counter:{scheme.prefix}:{country or 'US'}"
            legacy_counter = SystemConfig.query.filter_by(key=legacy_key).first()
        except Exception:
            legacy_counter = None

        if legacy_counter is not None:
            refs = db.session.execute(
                text("SELECT ref_no FROM cases WHERE ref_no LIKE :prefix"),
                {"prefix": f"{scheme.prefix}%"},
            ).scalars()
            max_num = _compute_max_our_ref_seq_from_refs(
                refs,
                scheme.sequence_pattern or scheme.pattern,
            )
            next_ref = scheme.build(max_num + 1)
        else:
            next_ref = generate_next_our_ref(
                division=division,
                matter_type=ctype,
                country=country,
                reserve=False,
            )
    except NextOurRefError as exc:
        return api_response(
            ok=False,
            error=exc.code,
            message=exc.message,
            status=exc.status,
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.cases_next_ref",
            log_key="api.routes.cases_next_ref",
            log_window_seconds=300,
        )
        return api_response(
            ok=False,
            error="next_ref_failed",
            message="next_ref_failed",
            status=500,
        )

    return api_response(value={"next_ref": next_ref}, legacy={"next_ref": next_ref, "ok": True})


@bp.route("/clients/search")
@login_required
def clients_search():
    ensure_clients_synced_from_party()
    q = (request.args.get("q") or "").strip()
    query = Client.query.filter(_active_filter(Client))
    if q:
        query = query.filter(
            sqlalchemy_contains_query(Client.name, q)
            | sqlalchemy_contains_query(Client.email, q)
            | sqlalchemy_contains_query(Client.registration_number, q)
            | sqlalchemy_contains_query(Client.phone, q)
            | sqlalchemy_contains_query(Client.biz_reg_number, q)
            | sqlalchemy_contains_query(Client.biz_company_name, q)
            | sqlalchemy_contains_query(Client.biz_tax_invoice_email, q)
            | sqlalchemy_contains_query(Client.search_tags, q)
        )
    items = query.order_by(Client.name).limit(20).all()
    return jsonify(
        [
            {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
            }
            for c in items
        ]
    )


@bp.route("/cases/<string:matter_id>/external-invoice-links", methods=["GET"])
@matter_action("invoice")
@login_required
def case_external_invoice_links(matter_id: str):
    _require_matter_access(matter_id)
    links = fetch_external_invoice_links_for_case(matter_id=matter_id)
    return jsonify({"ok": True, "links": links})


@bp.route("/cases/<string:matter_id>/external-invoice-links", methods=["POST"])
@matter_action("invoice")
@login_required
def case_external_invoice_links_create(matter_id: str):
    payload = request.get_json(silent=True) or {}
    ext_ref = (
        payload.get("external_invoice_ref")
        or payload.get("external_invoice_id")
        or payload.get("external_invoice_number")
        or ""
    )
    ext_ref = str(ext_ref or "").strip()
    if not ext_ref:
        return jsonify({"error": "external_invoice_ref is required"}), 400

    _require_matter_access(matter_id)

    try:
        data = InvoiceMatterLinkUseCase.link(
            matter_id=matter_id,
            external_invoice_ref=ext_ref,
            actor_id=getattr(current_user, "id", None),
        )
        return jsonify({"ok": True, "invoice": data})
    except InvoiceBridgeError as exc:
        return jsonify({"error": str(exc)}), 400
    except SQLAlchemyError as exc:
        return jsonify({"error": f"link failed: {exc}"}), 500


@bp.route(
    "/cases/<string:matter_id>/external-invoice-links/<path:external_invoice_ref>",
    methods=["DELETE"],
)
@matter_action("invoice")
@login_required
def case_external_invoice_links_delete(matter_id: str, external_invoice_ref: str):
    _require_matter_access(matter_id)
    ext_ref = str(external_invoice_ref or "").strip()
    if not ext_ref:
        return jsonify({"error": "external_invoice_ref is required"}), 400
    try:
        InvoiceMatterLinkUseCase.unlink(
            matter_id=matter_id,
            external_invoice_ref=ext_ref,
            actor_id=getattr(current_user, "id", None),
        )
        return jsonify({"ok": True})
    except InvoiceBridgeError as exc:
        return jsonify({"error": str(exc)}), 400
    except SQLAlchemyError as exc:
        return jsonify({"error": f"unlink failed: {exc}"}), 500


@bp.route("/cases/<string:case_id>/summary")
@login_required
def case_summary(case_id: str):
    matter = Matter.query.get_or_404(case_id)
    mid = str(case_id)
    _require_matter_access(mid)
    overview = VMatterOverview.query.get(mid)
    public_division, public_type = resolve_public_case_kind_for_matter(matter, overview)

    custom_rows = (
        MatterCustomField.query.filter(MatterCustomField.matter_id == mid)
        .filter(MatterCustomField.namespace.in_(["basic", *_case_custom_namespaces()]))
        .all()
    )
    custom_data = {}
    basic_data = {}
    for row in custom_rows:
        if row.namespace == "basic" and isinstance(row.data, dict) and row.data and not basic_data:
            basic_data = row.data
            continue
        if row.data and not custom_data:
            custom_data = row.data
    if not custom_data and basic_data:
        custom_data = basic_data

    list_extras = {}
    if overview:
        try:
            list_extras = _build_case_list_extras([overview]).get(mid) or {}
        except (AttributeError, RuntimeError, SQLAlchemyError) as exc:
            report_swallowed_exception(
                exc,
                context="api.routes.case_summary.list_extras",
                log_key="api.routes.case_summary.list_extras",
                log_window_seconds=300,
            )

    title = (
        (list_extras.get("proposal_title") if isinstance(list_extras, dict) else None)
        or (custom_data.get("proposal_title") if isinstance(custom_data, dict) else None)
        or (matter.right_name or "")
        or (matter.our_ref or "")
    )
    client_name = (
        str((list_extras.get("client_name") if isinstance(list_extras, dict) else "") or "").strip()
        or str(
            (basic_data.get("client_name") if isinstance(basic_data, dict) else "") or ""
        ).strip()
        or str((getattr(overview, "clients", None) or "")).strip()
    )
    applicant_name = (
        str(
            (list_extras.get("applicant_name") if isinstance(list_extras, dict) else "") or ""
        ).strip()
        or str(
            (custom_data.get("applicant_name") if isinstance(custom_data, dict) else "") or ""
        ).strip()
        or str((getattr(overview, "applicants", None) or "")).strip()
    )
    client_id = _safe_int(
        (list_extras.get("client_id") if isinstance(list_extras, dict) else None)
        or (basic_data.get("client_id") if isinstance(basic_data, dict) else None)
    )
    applicant_client_id = _safe_int(
        (list_extras.get("applicant_client_id") if isinstance(list_extras, dict) else None)
        or (custom_data.get("applicant_client_id") if isinstance(custom_data, dict) else None)
    )
    application_no = str(
        (list_extras.get("application_no") if isinstance(list_extras, dict) else "")
        or (custom_data.get("application_no") if isinstance(custom_data, dict) else "")
        or ""
    ).strip()
    application_date = str(
        (list_extras.get("application_date") if isinstance(list_extras, dict) else "")
        or (custom_data.get("application_date") if isinstance(custom_data, dict) else "")
        or ""
    ).strip()
    attorney_name = (
        str((basic_data.get("attorney") if isinstance(basic_data, dict) else "") or "").strip()
        or str((custom_data.get("attorney") if isinstance(custom_data, dict) else "") or "").strip()
        or str((getattr(overview, "attorneys", None) or "")).strip()
    )
    handler_name = (
        str((basic_data.get("handler") if isinstance(basic_data, dict) else "") or "").strip()
        or str((custom_data.get("handler") if isinstance(custom_data, dict) else "") or "").strip()
    )
    manager_name = (
        str((basic_data.get("manager") if isinstance(basic_data, dict) else "") or "").strip()
        or str((custom_data.get("manager") if isinstance(custom_data, dict) else "") or "").strip()
    )
    list_display_red = str(
        (list_extras.get("display_red") if isinstance(list_extras, dict) else "") or ""
    ).strip()
    matter_display_red = str((matter.status_red or "")).strip()
    display_red = list_display_red
    if (
        not display_red
        and matter_display_red
        and not is_non_action_status_red_label(matter_display_red)
    ):
        display_red = matter_display_red
    display_blue = (
        str(
            (list_extras.get("display_blue") if isinstance(list_extras, dict) else "") or ""
        ).strip()
        or str((matter.status_blue or "")).strip()
    )
    focus_label = (
        display_red or display_blue or str((matter.inhouse_status or "")).strip() or "OpenIn Progress"
    )
    focus_tone = "alert" if display_red else ("info" if display_blue else "default")

    today = date.today()
    due_text = effective_due_text_expr(
        DocketItem, dialect_name=getattr(db.engine.dialect, "name", "")
    )
    base_open = (
        DocketItem.query.filter(DocketItem.matter_id == mid)
        .filter(or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""))
        .filter(visible_on_or_before(DocketItem, target_date=today))
    )
    base_due = base_open.filter(due_text.isnot(None)).filter(due_text >= today.isoformat())
    next_item = base_due.order_by(due_text.asc()).first()
    open_deadline_count = base_open.count()

    next_deadline = None
    if next_item:
        due_date = effective_due_for_work(
            getattr(next_item, "due_date", None),
            getattr(next_item, "extended_due_date", None),
        )
        d_day = None
        if due_date:
            # D-<n>: days remaining until due_date (0 means due today)
            d_day = (due_date - today).days
        due_ymd = due_date.isoformat() if due_date else None
        calendar_endpoint = calendar_endpoint_for_docket(
            name_ref=getattr(next_item, "name_ref", None),
            title=(getattr(next_item, "name_free", None) or getattr(next_item, "name_ref", None)),
        )
        next_deadline = {
            "date": due_ymd,
            "d_day": d_day,
            "label": (next_item.name_free or next_item.name_ref or "").strip() or None,
            "docket_id": next_item.docket_id,
            "url": url_for("deadlines.docket_detail", docket_id=next_item.docket_id),
            "calendar_url": (url_for(calendar_endpoint, date=due_ymd) if due_ymd else None),
        }

    invoice_payload = fetch_case_invoices(mid)
    invoice_summary = invoice_payload.get("summary") or {}

    counts = {
        "open_deadlines": open_deadline_count,
        "memos": 0,
        "files": 0,
        "workflows": 0,
        "active_workflows": 0,
    }
    try:
        counts["memos"] = int(
            db.session.query(func.count(MatterMemo.id)).filter(MatterMemo.matter_id == mid).scalar()
            or 0
        )
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.memo_count",
            log_key="api.routes.case_summary.memo_count",
            log_window_seconds=300,
        )
    try:
        counts["files"] = int(
            db.session.query(func.count(MatterFileAsset.matter_file_id))
            .filter(MatterFileAsset.matter_id == mid)
            .filter(
                or_(MatterFileAsset.is_deleted.is_(None), MatterFileAsset.is_deleted.is_(False))
            )
            .scalar()
            or 0
        )
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.file_count",
            log_key="api.routes.case_summary.file_count",
            log_window_seconds=300,
        )
    try:
        workflow_base = Workflow.query.filter(Workflow.case_id == mid)
        counts["workflows"] = int(workflow_base.count() or 0)
        status_expr = func.lower(func.coalesce(Workflow.status, ""))
        counts["active_workflows"] = int(
            workflow_base.filter(~status_expr.in_(("completed", "abandoned"))).count() or 0
        )
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.workflow_count",
            log_key="api.routes.case_summary.workflow_count",
            log_window_seconds=300,
        )

    can_invoice_case = False
    try:
        can_invoice_case = bool(can_access_matter(current_user, mid, action="invoice"))
    except (AttributeError, RuntimeError, ValueError) as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.invoice_access",
            log_key="api.routes.case_summary.invoice_access",
            log_window_seconds=300,
        )

    client_url = url_for("customers.client_view", client_id=client_id) if client_id else None
    client_search_url = url_for("customers.clients", q=client_name) if client_name else None
    applicant_url = (
        url_for("customers.client_view", client_id=applicant_client_id)
        if applicant_client_id
        else None
    )
    applicant_search_url = (
        url_for("customers.clients", q=applicant_name) if applicant_name else None
    )
    links = {
        "case": url_for("case_work.case_detail", case_id=mid),
        "deadlines": url_for("case_work.case_detail", case_id=mid, _anchor="sec-deadlines"),
        "history": url_for("case_work.case_detail", case_id=mid, _anchor="sec-history"),
        "memo": url_for("case_work.case_detail", case_id=mid, _anchor="sec-memo"),
        "files": url_for("case_work.case_detail", case_id=mid, _anchor="sec-files"),
        "workflow": url_for("case_work.case_detail", case_id=mid, _anchor="sec-workflow"),
        "family": url_for("case_work.case_detail", case_id=mid, _anchor="sec-family"),
        "finance": (
            url_for("case_work.case_detail", case_id=mid, _anchor="sec-cost")
            if can_invoice_case
            else None
        ),
        "client": client_url,
        "client_search": client_search_url,
        "applicant": applicant_url,
        "applicant_search": applicant_search_url,
        "section_deadlines": url_for(
            "case_work.case_detail_section_legacy",
            case_id=mid,
            section="deadlines",
        ),
        "section_history": url_for(
            "case_work.case_detail_section_legacy",
            case_id=mid,
            section="history",
        ),
        "section_memo": url_for(
            "case_work.case_detail_section_legacy", case_id=mid, section="memo"
        ),
        "section_files": url_for(
            "case_work.case_detail_section_legacy", case_id=mid, section="files"
        ),
    }

    activity_candidates = []
    try:
        memo_dt = (
            db.session.query(func.max(MatterMemo.created_at))
            .filter(MatterMemo.matter_id == mid)
            .scalar()
        )
        if memo_dt:
            activity_candidates.append(memo_dt)
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.memo_dt",
            log_key="api.routes.case_summary.memo_dt",
            log_window_seconds=300,
        )
    try:
        file_dt = (
            db.session.query(func.max(MatterFileAsset.created_at))
            .filter(MatterFileAsset.matter_id == mid)
            .scalar()
        )
        if file_dt:
            activity_candidates.append(file_dt)
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.file_dt",
            log_key="api.routes.case_summary.file_dt",
            log_window_seconds=300,
        )
    try:
        work_dt = (
            db.session.query(func.max(WorkLog.updated_at)).filter(WorkLog.matter_id == mid).scalar()
        )
        if work_dt:
            activity_candidates.append(work_dt)
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.work_dt",
            log_key="api.routes.case_summary.work_dt",
            log_window_seconds=300,
        )
    try:
        inv_dt = (
            db.session.query(func.max(ExternalInvoiceCaseMap.created_at))
            .filter(ExternalInvoiceCaseMap.matter_id == mid)
            .scalar()
        )
        if inv_dt:
            activity_candidates.append(inv_dt)
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="api.routes.case_summary.inv_dt",
            log_key="api.routes.case_summary.inv_dt",
            log_window_seconds=300,
        )

    def _to_utc(dt_raw):
        if dt_raw is None:
            return None
        if isinstance(dt_raw, datetime):
            cur_dt = dt_raw
        elif isinstance(dt_raw, date):
            cur_dt = datetime.combine(dt_raw, datetime.min.time())
        else:
            s = str(dt_raw).strip()
            if not s:
                return None
            if " " in s and "T" not in s:
                s = s.replace(" ", "T")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                cur_dt = datetime.fromisoformat(s)
            except ValueError:
                return None
        if cur_dt.tzinfo is None:
            return cur_dt.replace(tzinfo=timezone.utc)
        try:
            return cur_dt.astimezone(timezone.utc)
        except ValueError:
            # Fallback: treat as UTC if tzinfo is malformed.
            return cur_dt.replace(tzinfo=timezone.utc)

    last_activity_at = None
    last_activity_ts = None
    for raw in activity_candidates:
        cur = _to_utc(raw)
        if not cur:
            continue
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=timezone.utc)
        try:
            cur_ts = cur.timestamp()
        except (OSError, OverflowError, ValueError):
            continue
        if last_activity_ts is None or cur_ts > last_activity_ts:
            last_activity_ts = cur_ts
            last_activity_at = cur

    return jsonify(
        {
            "case_id": mid,
            "our_ref": matter.our_ref,
            "your_ref": matter.your_ref,
            "title": title,
            "division": (matter.right_group or "").strip() or None,
            "type": (matter.matter_type or "").strip() or None,
            "display_division": public_division or ((matter.right_group or "").strip() or None),
            "display_type": public_type or ((matter.matter_type or "").strip() or None),
            "status": (matter.inhouse_status or "").strip() or None,
            "status_red": display_red or None,
            "status_blue": display_blue or None,
            "focus": {
                "label": focus_label,
                "tone": focus_tone,
            },
            "people": {
                "client_name": client_name or None,
                "client_id": client_id,
                "applicant_name": applicant_name or None,
                "applicant_client_id": applicant_client_id,
                "attorney": attorney_name or None,
                "handler": handler_name or None,
                "manager": manager_name or None,
            },
            "application": {
                "number": application_no or None,
                "date": application_date or None,
            },
            "counts": counts,
            "next_deadline": next_deadline,
            "open_deadline_count": open_deadline_count,
            "invoice": {
                "outstanding": invoice_summary.get("outstanding") or 0,
                "currency": invoice_summary.get("currency") or "USD",
                "overdue_count": invoice_summary.get("overdue_count") or 0,
            },
            "links": links,
            "last_activity_at": (last_activity_at.isoformat() if last_activity_at else None),
        }
    )


@bp.route("/cases/<string:matter_id>/deadlines", methods=["POST"])
@login_required
def case_deadline_create(matter_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403
    matter = Matter.query.get_or_404(matter_id)
    payload = request.get_json(silent=True) or {}
    label = (payload.get("label") or "").strip()
    due_date = (payload.get("due_date") or "").strip() or None
    internal_due_date = (payload.get("internal_due_date") or "").strip() or None
    assignee_id = _safe_int(payload.get("assignee_id"))
    priority = (payload.get("priority") or "").strip() or None
    visible_from_date = (payload.get("visible_from_date") or "").strip() or None

    if not label:
        return jsonify({"error": "label is required"}), 400
    if not (due_date or internal_due_date):
        return jsonify({"error": "due_date or internal_due_date is required"}), 400
    if due_date and not _parse_date_only(due_date):
        return jsonify({"error": "invalid due_date"}), 400
    if internal_due_date and not _parse_date_only(internal_due_date):
        return jsonify({"error": "invalid internal_due_date"}), 400
    if visible_from_date and not _parse_date_only(visible_from_date):
        return jsonify({"error": "invalid visible_from_date"}), 400
    effective_due = internal_due_date or due_date
    if visible_from_date and effective_due and visible_from_date > effective_due:
        return jsonify({"error": "visible_from_date must be on or before effective due date"}), 400

    owner_staff_party_id = None
    if assignee_id:
        user = User.query.get(assignee_id)
        if user and user.staff_party_id:
            owner_staff_party_id = str(user.staff_party_id)

    try:
        from app.utils.task_classification import determine_category_by_staff_role

        category = determine_category_by_staff_role(str(matter.matter_id), assignee_id=assignee_id)
    except (AttributeError, ImportError, RuntimeError, SQLAlchemyError, ValueError):
        category = "WORK"

    di = DocketItem(
        matter_id=str(matter.matter_id),
        category=category,
        name_free=label,
        due_date=due_date,
        extended_due_date=internal_due_date,
        visible_from_date=visible_from_date,
        owner_staff_party_id=owner_staff_party_id,
        memo=priority,
    )
    db.session.add(di)
    try:
        from app.services.workflow.sync_requests import enqueue_docket_sync_for_item

        enqueue_docket_sync_for_item(docket_item=di, actor_id=getattr(current_user, "id", None))
    except (AttributeError, ImportError, RuntimeError, SQLAlchemyError, ValueError) as exc:
        # Best-effort: creation should succeed even if sync enqueue fails.
        report_swallowed_exception(
            exc,
            context="api.case_deadline_create.enqueue_docket_sync",
            log_key="api.case_deadline_create.enqueue_docket_sync",
            log_window_seconds=300,
        )

    _log_case_audit(
        case_id=str(matter_id),
        field="deadline.add",
        old_value=None,
        new_value={
            "label": label,
            "due_date": due_date,
            "internal_due_date": internal_due_date,
            "assignee_id": assignee_id,
            "priority": priority,
            "visible_from_date": visible_from_date,
        },
        action="DEADLINE",
    )
    db.session.flush()
    record_entity_change_audit(
        action="docket.create",
        target_type="docket_item",
        actor_id=getattr(current_user, "id", None),
        after=_docket_audit_snapshot(di),
        meta=_docket_audit_meta(di, source="api.case_deadline_create"),
        title=label,
        include_snapshots=True,
    )
    db.session.commit()

    return (
        jsonify(
            {
                "id": di.docket_id,
                "matter_id": str(matter.matter_id),
                "label": label,
                "due_date": due_date,
                "internal_due_date": internal_due_date,
                "assignee_id": assignee_id,
                "priority": priority,
                "visible_from_date": visible_from_date,
            }
        ),
        201,
    )


@bp.route("/cases/<string:matter_id>/deadlines/<string:docket_id>", methods=["PATCH", "DELETE"])
@login_required
def case_deadline_patch(matter_id: str, docket_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403

    di = DocketItem.query.get_or_404(str(docket_id))
    if str(getattr(di, "matter_id", "") or "") != str(matter_id):
        return jsonify({"error": "not_found"}), 404
    if hasattr(di, "is_deleted") and bool(getattr(di, "is_deleted", False)):
        return jsonify({"error": "not_found"}), 404

    if request.method == "DELETE":
        # Matter view User Deadline Delete  ( Deadline  to ).
        if (di.name_ref or "").strip():
            return jsonify({"error": "system_deadline_cannot_be_deleted"}), 400
        audit_before = _docket_audit_snapshot(di)

        before = {
            "label": (di.name_free or di.name_ref or "").strip(),
            "due_date": (di.due_date or "").strip(),
            "internal_due_date": (di.extended_due_date or "").strip(),
            "visible_from_date": (di.visible_from_date or "").strip(),
            "owner_staff_party_id": (di.owner_staff_party_id or "").strip(),
            "memo": di.memo,
        }

        deleted_at = datetime.utcnow()
        if hasattr(di, "is_deleted"):
            di.is_deleted = True
        else:
            db.session.delete(di)
        if hasattr(di, "deleted_at"):
            di.deleted_at = deleted_at
        if hasattr(di, "deleted_by"):
            di.deleted_by = getattr(current_user, "id", None)
        if hasattr(di, "delete_reason"):
            di.delete_reason = "case_user_deadline_delete"

        _log_case_audit(
            case_id=str(matter_id),
            field="deadline.delete",
            old_value=before,
            new_value={"id": str(di.docket_id), "deleted": True},
            action="DEADLINE",
        )

        try:
            from app.services.workflow.sync_requests import enqueue_docket_sync_for_item

            enqueue_docket_sync_for_item(docket_item=di, actor_id=getattr(current_user, "id", None))
        except (AttributeError, ImportError, RuntimeError, SQLAlchemyError, ValueError) as exc:
            report_swallowed_exception(
                exc,
                context="api.case_deadline_delete.enqueue_docket_sync",
                log_key="api.case_deadline_delete.enqueue_docket_sync",
                log_window_seconds=300,
            )

        audit_after = _docket_audit_snapshot(di)
        record_entity_change_audit(
            action="docket.delete",
            target_type="docket_item",
            actor_id=getattr(current_user, "id", None),
            before=audit_before,
            after=audit_after,
            meta=_docket_audit_meta(di, source="api.case_deadline_delete"),
            title=before["label"],
            include_snapshots=True,
        )
        db.session.commit()
        return jsonify({"success": True, "id": str(di.docket_id), "matter_id": str(matter_id)})

    payload = request.get_json(silent=True) or {}

    # Snapshot for audit
    audit_before = _docket_audit_snapshot(di)
    before = {
        "label": (di.name_free or di.name_ref or "").strip(),
        "due_date": (di.due_date or "").strip(),
        "internal_due_date": (di.extended_due_date or "").strip(),
        "visible_from_date": (di.visible_from_date or "").strip(),
        "owner_staff_party_id": (di.owner_staff_party_id or "").strip(),
        "memo": di.memo,
    }

    # Patch helpers
    def _as_bool(v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        s = str(v).strip().lower()
        return s in {"1", "true", "yes", "y", "on"}

    memo_json: dict | None = None
    try:
        parsed = json.loads(di.memo) if (di.memo or "").strip() else None
        if isinstance(parsed, dict):
            memo_json = parsed
    except (JSONDecodeError, TypeError):
        memo_json = None

    name_ref = (di.name_ref or "").strip()
    is_system = bool(name_ref)
    is_auto = bool(memo_json is not None and memo_json.get("auto"))
    locked_current = bool(memo_json is not None and memo_json.get("locked"))
    locked_after = locked_current

    if "locked" in payload and memo_json is not None and is_auto:
        locked_after = _as_bool(payload.get("locked"))

    # Auto-managed system deadlines: only allow overriding core fields when locked.
    allow_override = (not is_system) or (not is_auto) or locked_after

    # label
    if "label" in payload:
        new_label = str(payload.get("label") or "").strip()
        if new_label and allow_override:
            di.name_free = new_label

    # due_date
    if "due_date" in payload:
        raw = (payload.get("due_date") or "").strip()
        if raw and not _parse_date_only(raw):
            return jsonify({"error": "invalid due_date"}), 400
        if allow_override:
            di.due_date = raw or None

    # internal_due_date (extended_due_date)
    if "internal_due_date" in payload:
        raw = (payload.get("internal_due_date") or "").strip()
        if raw and not _parse_date_only(raw):
            return jsonify({"error": "invalid internal_due_date"}), 400
        if allow_override:
            di.extended_due_date = raw or None

    # visible_from_date
    if "visible_from_date" in payload:
        raw = (payload.get("visible_from_date") or "").strip()
        if raw:
            if not _parse_date_only(raw):
                return jsonify({"error": "invalid visible_from_date"}), 400
            di.visible_from_date = raw
        else:
            di.visible_from_date = None

    # assignee
    if "assignee_id" in payload:
        assignee_id = _safe_int(payload.get("assignee_id"))
        owner_staff_party_id = None
        if assignee_id:
            user = User.query.get(assignee_id)
            if user and user.staff_party_id:
                owner_staff_party_id = str(user.staff_party_id)
        di.owner_staff_party_id = owner_staff_party_id
        if is_system:
            prefix = f"DOCKET:{di.docket_id}"
            candidates = (
                Workflow.query.filter(Workflow.case_id == str(matter_id))
                .filter(Workflow.business_code.like(f"{prefix}%"))
                .order_by(Workflow.id.asc())
                .all()
            )
            linked_workflow = next(
                (
                    wf
                    for wf in candidates
                    if (getattr(wf, "business_code", None) or "").strip() == prefix
                ),
                candidates[0] if candidates else None,
            )
            if linked_workflow is not None:
                assignment_request_before = workflow_assignment_state(linked_workflow)
                linked_workflow.assignee_id = assignee_id
                linked_workflow.category = derive_workflow_category(
                    case_id=str(matter_id),
                    handler_id=linked_workflow.assignee_id,
                    attorney_id=getattr(linked_workflow, "attorney_assignee_id", None),
                    manager_id=getattr(linked_workflow, "inspector_id", None),
                    hint_category=getattr(linked_workflow, "category", None),
                    hint_name_ref=getattr(di, "name_ref", None),
                    hint_name_free=(di.name_free or di.name_ref or "").strip() or None,
                )
                persist_manual_workflow_assignment_override(
                    workflow=linked_workflow,
                    docket_item=di,
                    actor_id=getattr(current_user, "id", None),
                )
                sync_assignment_requests_for_changed_roles(
                    linked_workflow,
                    assignment_request_before,
                    requested_by_id=getattr(current_user, "id", None),
                    source="deadline_assignee_patch",
                )
                db.session.add(linked_workflow)
        if (not is_system) and assignee_id:
            try:
                from app.utils.task_classification import determine_category_by_staff_role

                di.category = determine_category_by_staff_role(
                    str(matter_id),
                    assignee_id=int(assignee_id),
                    staff_party_id=owner_staff_party_id,
                    category=getattr(di, "category", None),
                    name_ref=name_ref,
                    name_free=(di.name_free or di.name_ref or "").strip() or None,
                )
            except (AttributeError, ImportError, RuntimeError, SQLAlchemyError, ValueError) as exc:
                report_swallowed_exception(
                    exc,
                    context="api.case_deadline_patch.determine_category_by_staff_role",
                    log_key="api.case_deadline_patch.determine_category_by_staff_role",
                    log_window_seconds=300,
                )

    # priority (only for non-JSON memo rows; system rows store JSON metadata)
    if "priority" in payload:
        raw = str(payload.get("priority") or "").strip()
        if memo_json is None and not is_system:
            di.memo = raw or None

    # lock (only meaningful for JSON memo system rows)
    if "locked" in payload:
        locked = _as_bool(payload.get("locked"))
        if memo_json is not None and is_auto:
            memo_json["locked"] = locked
            try:
                di.memo = json.dumps(memo_json, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                # Keep old memo if serialization fails
                report_swallowed_exception(
                    exc,
                    context="api.case_deadline_patch.memo_json.dumps",
                    log_key="api.case_deadline_patch.memo_json.dumps",
                    log_window_seconds=300,
                )

    # Validate window: visible_from_date must not be after the effective due date.
    effective_due = (di.extended_due_date or "").strip() or (di.due_date or "").strip() or None
    visible_from = (di.visible_from_date or "").strip() or None
    if effective_due and visible_from and visible_from > effective_due:
        return (
            jsonify({"error": "visible_from_date must be on or before effective due date"}),
            400,
        )

    after = {
        "label": (di.name_free or di.name_ref or "").strip(),
        "due_date": (di.due_date or "").strip(),
        "internal_due_date": (di.extended_due_date or "").strip(),
        "visible_from_date": (di.visible_from_date or "").strip(),
        "owner_staff_party_id": (di.owner_staff_party_id or "").strip(),
        "memo": di.memo,
    }

    _log_case_audit(
        case_id=str(matter_id),
        field="deadline.update",
        old_value=before,
        new_value=after,
        action="DEADLINE",
    )

    try:
        from app.services.workflow.sync_requests import enqueue_docket_sync_for_item

        enqueue_docket_sync_for_item(docket_item=di, actor_id=getattr(current_user, "id", None))
    except (AttributeError, ImportError, RuntimeError, SQLAlchemyError, ValueError) as exc:
        report_swallowed_exception(
            exc,
            context="api.case_deadline_patch.enqueue_docket_sync",
            log_key="api.case_deadline_patch.enqueue_docket_sync",
            log_window_seconds=300,
        )

    audit_after = _docket_audit_snapshot(di)
    changes = diff_snapshots(audit_before, audit_after)
    if changes:
        record_entity_change_audit(
            action=(
                "docket.status_change"
                if set(changes.keys()) <= {"done_date", "status"}
                else "docket.update"
            ),
            target_type="docket_item",
            actor_id=getattr(current_user, "id", None),
            changes=changes,
            meta=_docket_audit_meta(di, source="api.case_deadline_patch"),
            title=after["label"],
        )
    db.session.commit()

    return jsonify(
        {
            "id": di.docket_id,
            "matter_id": str(matter_id),
            "label": after["label"],
            "due_date": after["due_date"] or None,
            "internal_due_date": after["internal_due_date"] or None,
            "visible_from_date": after["visible_from_date"] or None,
            "owner_staff_party_id": after["owner_staff_party_id"] or None,
        }
    )


@bp.route("/cases/<string:matter_id>/memos", methods=["POST"])
@login_required
def case_memo_create(matter_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403
    Matter.query.get_or_404(matter_id)
    payload = request.get_json(silent=True) or {}
    content = (payload.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400

    memo = MatterMemo(
        matter_id=str(matter_id),
        body=content,
        created_by_id=getattr(current_user, "id", None),
        created_by_name=(getattr(current_user, "username", None) or "User"),
    )
    db.session.add(memo)
    db.session.commit()

    # : Notes Add( All Save  Preview)
    try:
        preview = content[:120] + ("..." if len(content) > 120 else "")
        _log_case_audit(
            case_id=str(matter_id),
            field="memo.add",
            old_value=None,
            new_value={"memo_id": memo.id, "preview": preview, "len": len(content)},
            action="MEMO",
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()

    return (
        jsonify(
            {
                "id": memo.id,
                "matter_id": str(matter_id),
                "content": memo.body,
                "created_at": memo.created_at.isoformat() if memo.created_at else None,
                "created_by": memo.created_by_name,
                "attachments": [],
            }
        ),
        201,
    )


@bp.route("/cases/<string:matter_id>/files", methods=["POST"])
@login_required
def case_file_upload(matter_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403
    Matter.query.get_or_404(matter_id)
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "file is required"}), 400

    parent_id = request.form.get("parent_id")
    if not parent_id or str(parent_id).strip() in ("", "None", "none"):
        parent_id = None
    else:
        parent_id = str(parent_id).strip()

    # parent_id  " Matter folder"  ( Matter reference/  )
    if parent_id:
        folder = MatterFileAsset.query.filter_by(
            matter_id=str(matter_id),
            role="folder",
            matter_file_id=str(parent_id),
        ).first()
        if not folder:
            return jsonify({"error": "invalid parent_id"}), 400

    file_service = get_file_asset_service()
    now = datetime.now()
    subdir = f"fm/{now:%Y/%m}"

    try:
        staged = file_service.stage_upload(file, subdir=subdir)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 413
    doc_type, tags = classify_doc_type(staged.original_name)
    previewable = is_previewable(staged.original_name, staged.mime_type)

    mfa = MatterFileAsset(
        matter_file_id=uuid.uuid4().hex,
        matter_id=str(matter_id),
        file_asset_id=staged.file_asset_id,
        role="internal",
        parent_id=parent_id,
        doc_type=doc_type,
        tags=tags,
        previewable=previewable,
        created_at=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.session.add(mfa)
    try:
        db.session.commit()
    except IntegrityError:
        #  File(sha256 ) Upload to uq_matter_file_asset
        db.session.rollback()
        existing = MatterFileAsset.query.filter_by(
            matter_id=str(matter_id),
            file_asset_id=staged.file_asset_id,
            role="internal",
        ).first()
        if not existing:
            return jsonify({"error": "duplicate file link"}), 409

        # parent_id   (Folder Go UX )
        if parent_id and (existing.parent_id or "") != parent_id:
            existing.parent_id = parent_id

        # doc_type/tags MANUAL    Autovalueto (Manual category )
        existing_tags = existing.tags if isinstance(existing.tags, list) else []
        if "MANUAL" not in existing_tags:
            existing.doc_type = doc_type
            existing.tags = tags

        # previewable File   Updated
        existing.previewable = previewable

        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify({"error": "failed to recover duplicate upload"}), 500
        mfa = existing

    # : File Upload(Upload )
    try:
        _log_case_audit(
            case_id=str(matter_id),
            field="file.upload",
            old_value=None,
            new_value={
                "file_id": staged.file_asset_id,
                "filename": staged.original_name,
                "doc_type": doc_type,
                "tags": tags,
                "parent_id": parent_id,
            },
            action="UPLOAD",
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()

    return (
        jsonify(
            {
                "file_id": staged.file_asset_id,
                "matter_id": str(matter_id),
                "matter_file_id": mfa.matter_file_id,
                "filename": staged.original_name,
                "doc_type": doc_type,
                "tags": tags,
                "preview_url": f"/files/{staged.file_asset_id}/preview",
                "download_url": url_for(
                    "case_work.download_file_asset",
                    case_id=matter_id,
                    file_asset_id=staged.file_asset_id,
                ),
                "created_at": mfa.created_at,
            }
        ),
        201,
    )


@bp.route("/files/<string:file_id>", methods=["PATCH"])
@login_required
def update_file_meta(file_id: str):
    payload = request.get_json(silent=True) or {}
    doc_type = (payload.get("doc_type") or "").strip().upper()
    matter_id = (payload.get("matter_id") or payload.get("case_id") or "").strip()

    if matter_id and not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403
    if not matter_id and not check_permission("manage_case"):
        return jsonify({"error": "forbidden"}), 403

    if doc_type not in DOC_TYPE_LABELS:
        return jsonify({"error": "invalid doc_type"}), 400

    q = MatterFileAsset.query.filter_by(file_asset_id=file_id)
    if matter_id:
        q = q.filter(MatterFileAsset.matter_id == matter_id)
    rows = q.all()
    if not rows:
        return jsonify({"error": "file not found"}), 404

    # Change  doc_type Log()
    old_doc_type = rows[0].doc_type if rows else None

    updated_tags = None
    for row in rows:
        current_tags = row.tags if isinstance(row.tags, list) else []
        tags = [t for t in current_tags if t != "AUTO_CLASSIFIED"]
        if "MANUAL" not in tags:
            tags.append("MANUAL")
        row.doc_type = doc_type
        row.tags = tags
        updated_tags = tags

    db.session.commit()

    # : File (doc_type) Change
    try:
        _log_case_audit(
            case_id=str(matter_id) if matter_id else str(rows[0].matter_id),
            field="file.doc_type",
            old_value=old_doc_type,
            new_value=doc_type,
            action="FILE",
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
    return jsonify(
        {
            "ok": True,
            "matter_id": str(matter_id) if matter_id else str(rows[0].matter_id),
            "doc_type": doc_type,
            "tags": updated_tags,
        }
    )


@bp.route("/cases/<string:matter_id>", methods=["PATCH"])
@login_required
def case_patch(matter_id: str):
    if not _can_edit_matter(matter_id):
        return jsonify({"error": "forbidden"}), 403
    matter = Matter.query.get_or_404(matter_id)

    payload = request.get_json(silent=True) or {}
    updated_fields = []
    audit_ids = []
    old_status_for_calendar = ""
    new_status_for_calendar = ""

    def _add_audit(field: str, old_value, new_value):
        audit = CaseAuditLog(
            case_id=str(matter_id),
            actor_user_id=getattr(current_user, "id", None),
            action="PATCH",
            field_name=field,
            old_value=old_value,
            new_value=new_value,
            request_id=getattr(g, "request_id", None),
        )
        db.session.add(audit)
        db.session.flush()
        audit_ids.append(audit.id)

    custom_rows = (
        MatterCustomField.query.filter(MatterCustomField.matter_id == str(matter_id))
        .filter(MatterCustomField.namespace.in_(_case_custom_namespaces()))
        .all()
    )
    custom_data = {}
    for row in custom_rows:
        if row.data:
            custom_data = row.data
            break

    if "title" in payload:
        new_title = (payload.get("title") or "").strip()
        if not new_title:
            return jsonify({"error": "title is required"}), 400
        if len(new_title) > 200:
            return jsonify({"error": "title too long"}), 400
        old_title = (matter.right_name or "").strip()
        if new_title != old_title:
            matter.right_name = new_title
            _update_matter_custom_fields(str(matter_id), {"proposal_title": new_title})
            _add_audit("title", old_title, new_title)
            updated_fields.append("title")

    if "status" in payload:
        new_status = (payload.get("status") or "").strip()
        if not new_status:
            return jsonify({"error": "status is required"}), 400
        old_status = (matter.inhouse_status or "").strip()
        if new_status != old_status:
            matter.inhouse_status = new_status
            _add_audit("status", old_status, new_status)
            updated_fields.append("status")
            old_status_for_calendar = old_status
            new_status_for_calendar = new_status

    if "our_ref" in payload:
        new_ref = (payload.get("our_ref") or "").strip()
        if not new_ref:
            return jsonify({"error": "our_ref is required"}), 400
        existing = Matter.query.filter_by(our_ref=new_ref).first()
        if existing and str(existing.matter_id) != str(matter_id):
            return jsonify({"error": "our_ref already exists"}), 409
        old_ref = (matter.our_ref or "").strip()
        if new_ref != old_ref:
            matter.our_ref = new_ref
            _add_audit("our_ref", old_ref, new_ref)
            updated_fields.append("our_ref")

    if "your_ref" in payload:
        new_ref = (payload.get("your_ref") or "").strip()
        old_ref = (matter.your_ref or "").strip()
        if new_ref != old_ref:
            matter.your_ref = new_ref or None
            _add_audit("your_ref", old_ref, new_ref)
            updated_fields.append("your_ref")

    if "assignee_id" in payload:
        new_assignee_id = _safe_int(payload.get("assignee_id"))
        if new_assignee_id:
            # Prevent assigning deactivated/deleted users.
            # Keep this check aligned with staff pickers (User.is_active + PartyStaff.active when linked).
            try:
                from sqlalchemy import and_, or_

                from app.models.party import PartyStaff

                row = (
                    db.session.query(User.id, User.staff_party_id)
                    .filter(User.id == int(new_assignee_id))
                    .filter(User.is_active.is_(True))
                    .first()
                )
                if not row:
                    ok = False
                else:
                    staff_party_id = (row.staff_party_id or "").strip()
                    staff_dir_ok = and_(
                        PartyStaff.party_id.isnot(None),
                        or_(PartyStaff.active == 1, PartyStaff.active.is_(None)),
                    )
                    if staff_party_id:
                        ok = (
                            db.session.query(PartyStaff.party_id)
                            .filter(PartyStaff.party_id == staff_party_id)
                            .filter(or_(PartyStaff.active == 1, PartyStaff.active.is_(None)))
                            .first()
                            is not None
                        )
                    else:
                        # Unlinked users are only assignable when there are no directory-linked users.
                        has_linked_staff = (
                            db.session.query(User.id)
                            .join(PartyStaff, PartyStaff.party_id == User.staff_party_id)
                            .filter(User.is_active.is_(True))
                            .filter(staff_dir_ok)
                            .first()
                            is not None
                        )
                        ok = not has_linked_staff
            except SQLAlchemyError:
                ok = bool(User.query.filter_by(id=new_assignee_id, is_active=True).first())

            if not ok:
                return (
                    jsonify(
                        {
                            "error": "invalid_assignee",
                            "message": "disabled/Delete User Contact   none.",
                        }
                    ),
                    400,
                )
        current_assignee_id = None
        msa = MatterStaffAssignment.query.filter(
            MatterStaffAssignment.matter_id == str(matter_id),
            func.lower(func.trim(MatterStaffAssignment.staff_role_code)) == "attorney",
        ).first()
        if msa and msa.staff_party_id:
            user = User.query.filter_by(staff_party_id=msa.staff_party_id).first()
            if user:
                current_assignee_id = user.id
        if new_assignee_id != current_assignee_id:
            form_data = {"attorney_id": str(new_assignee_id or "")}
            _update_basic_matter_info(str(matter_id), form_data)
            _add_audit("assignee_id", current_assignee_id, new_assignee_id)
            updated_fields.append("assignee_id")

    if "client_id" in payload:
        new_client_id = _safe_int(payload.get("client_id"))
        if new_client_id:
            client = Client.query.get(new_client_id)
            if not client:
                return jsonify({"error": "invalid client_id"}), 400
            client_name = client.name
        else:
            client_name = ""
        old_client_id = _safe_int(custom_data.get("client_id"))
        if new_client_id != old_client_id:
            _update_matter_custom_fields(
                str(matter_id),
                {
                    "client_id": new_client_id or "",
                    "client_name": client_name or "",
                },
            )
            _add_audit("client_id", old_client_id, new_client_id)
            updated_fields.append("client_id")

    if not updated_fields:
        return jsonify({"ok": True, "updated_fields": [], "audit_ids": []})

    db.session.commit()
    if "status" in updated_fields:
        apply_case_status_side_effects(
            matter_id=str(matter_id),
            old_status=old_status_for_calendar,
            new_status=new_status_for_calendar,
            actor_id=getattr(current_user, "id", None),
            logger_override=current_app.logger,
        )
    try:
        upsert_case_flat_index(str(matter_id))
        db.session.commit()
    except (AttributeError, RuntimeError, SQLAlchemyError, ValueError) as exc:
        db.session.rollback()
        current_app.logger.warning(
            "case_update: flat index refresh failed for %s: %s", matter_id, exc
        )
    return jsonify(
        {
            "ok": True,
            "matter_id": str(matter_id),
            "updated_fields": updated_fields,
            "audit_ids": audit_ids,
            "case_updated_at": datetime.utcnow().isoformat(),
        }
    )
