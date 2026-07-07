from datetime import date, datetime
from pathlib import Path

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.blueprints.case import bp
from app.blueprints.case.helpers import (
    _sync_matter_events_from_out_design,
    _sync_matter_events_from_out_trademark,
    _sync_matter_events_from_pct,
    _sync_matter_identifiers_from_out_design,
    _sync_matter_identifiers_from_out_trademark,
    _sync_matter_identifiers_from_pct,
)
from app.blueprints.case.services.detail_context import (
    _build_base,
    _build_family_section,
    build_case_detail_context,
)
from app.extensions import db
from app.models.ip_records import (
    Family,
    Matter,
    MatterCustomField,
    MatterFamily,
    MatterMemo,
    MatterMemoFileAsset,
    MatterStaffAssignment,
    VMatterOverview,
)
from app.services.case.canonical_field_service import upsert_case_flat_index
from app.services.case.case_audit_service import record_case_audit
from app.services.case.case_kind import _infer_case_kind, resolve_public_case_kind_for_matter
from app.services.case.case_parameter_service import CaseParameterService
from app.services.matter.matter_status_cache import apply_auto_status_cache_to_matter
from app.services.matter.pct_related_application import apply_related_application_suggestion
from app.services.storage.file_asset_service import get_file_asset_service
from app.utils.permissions import require_matter_access, resolve_matter_id_for_case_ref
from app.utils.policy_sql import policy_text as text

_MEMO_PREFILL_FIELDS = {"body"}
_COPY_PEOPLE_KEYS = {
    "client_name",
    "client_id",
    "client_contact",
    "applicant_name",
    "applicant_registrant",
    "manager",
    "attorney",
    "handler",
    "drawing_handler",
    "assignee1",
    "assignee2",
    "claimant_name",
    "claimant_name_id",
    "claimant_agent",
    "respondent_name",
    "respondent_name_id",
    "respondent_agent",
}
_COPY_ALLOWED_ID_KEYS = {
    "client_id",
    "claimant_name_id",
    "respondent_name_id",
}
_COPY_ALLOWED_EXTRA_KEYS = {
    "proposal_title",
    "title",
    "title_en",
    "case_name",
    "right_type",
    "filing_type",
    "filing_kind",
    "application_country",
    "application_language",
    "app_route",
}
_COPY_BASIC_KEYS = {
    "client_name",
    "client_id",
    "client_contact",
    "manager",
    "manager_id",
    "attorney",
    "attorney_id",
    "handler",
    "handler_id",
    "drawing_handler",
    "drawing_handler_id",
    "department",
    "assignee1",
    "assignee2",
    "claimant_name",
    "claimant_name_id",
    "claimant_agent",
    "respondent_name",
    "respondent_name_id",
    "respondent_agent",
}
_COPY_EXACT_EXCLUDE_KEYS = {
    "image",
    "our_ref",
    "old_our_ref",
    "your_ref",
    "inhouse_status",
    "memo",
    "memo2",
    "misc",
}
_COPY_EXCLUDE_SUBSTRINGS = (
    "match",
    "matching",
    "communication",
    "office_action",
    "workflow",
    "docket",
    "annuity",
    "invoice",
    "expense",
    "payment",
    "family",
    "file",
    "attachment",
    "history",
    "event",
    "notification",
)
_FAMILY_PREF_NAMESPACE = "family"
_FAMILY_EXCLUDE_KEY = "excluded_related_matter_ids"
_CASE_DETAIL_SECTION_TEMPLATES = {
    "files": "case/matter_view/partials/_sec_files_content.html",
    "history": "case/matter_view/partials/_sec_history_content.html",
    "deadlines": "case/matter_view/partials/_sec_deadlines_content.html",
    "memo": "case/matter_view/partials/_sec_memo_content.html",
    "cost": "case/matter_view/partials/_sec_cost_content.html",
    "annuity": "case/matter_view/partials/_sec_annuity_content.html",
    "alarm": "case/matter_view/partials/_sec_alarm_content.html",
}
_CASE_DETAIL_SECTION_HASH_TARGETS = {
    "files": "sec-files",
    "history": "sec-history",
    "deadlines": "sec-deadlines",
    "memo": "sec-memo",
    "cost": "sec-cost",
    "annuity": "sec-annuity",
    "alarm": "alarm",
}


def _is_copyable_registry_key(key: str) -> bool:
    normalized = (key or "").strip().lower()
    if not normalized:
        return False
    if normalized in _COPY_PEOPLE_KEYS:
        return True
    if normalized in _COPY_EXACT_EXCLUDE_KEYS:
        return False
    if normalized.endswith("_id") and normalized not in _COPY_ALLOWED_ID_KEYS:
        return False
    if normalized.endswith(("_date", "_deadline", "_no", "_number")):
        return False
    if "status" in normalized or "result" in normalized:
        return False
    return not any(token in normalized for token in _COPY_EXCLUDE_SUBSTRINGS)


def _copyable_registry_data(data: dict, allowed_keys: set[str]) -> dict:
    out: dict = {}
    for key, value in (data or {}).items():
        if allowed_keys and key not in allowed_keys and key not in _COPY_ALLOWED_EXTRA_KEYS:
            continue
        if not _is_copyable_registry_key(key):
            continue
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        out[key] = value
    return out


def _copyable_basic_data(data: dict) -> dict:
    out: dict = {}
    for key, value in (data or {}).items():
        if key not in _COPY_BASIC_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        out[key] = value
    return out


def _generate_copy_our_ref(source: Matter) -> str:
    base = (source.our_ref or "").strip()
    if not base:
        base = f"COPY{date.today().strftime('%y%m%d')}"
    for seq in range(1, 1000):
        candidate = f"{base}({seq})"
        if not Matter.query.filter_by(our_ref=candidate).first():
            return candidate
    raise RuntimeError("copy_ref_exhausted")


def _extract_prefill_params(args) -> dict:
    prefill = {}
    for key in _MEMO_PREFILL_FIELDS:
        value = (args.get(key) or "").strip()
        if value:
            prefill[key] = value
    return prefill


def _load_family_excluded_related_ids(data: dict | None, *, self_matter_id: str = "") -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    payload = data if isinstance(data, dict) else {}
    raw_values = payload.get(_FAMILY_EXCLUDE_KEY)
    if not isinstance(raw_values, list):
        return out
    for raw in raw_values:
        rel_mid = str(raw or "").strip()
        if not rel_mid or rel_mid == self_matter_id or rel_mid in seen:
            continue
        seen.add(rel_mid)
        out.append(rel_mid)
    return out


def _upsert_auto_related_exclusion(matter_id: str, target_matter_id: str) -> bool:
    matter_id = (matter_id or "").strip()
    target_matter_id = (target_matter_id or "").strip()
    if not matter_id or not target_matter_id or matter_id == target_matter_id:
        return False

    row = MatterCustomField.query.filter_by(
        matter_id=matter_id, namespace=_FAMILY_PREF_NAMESPACE
    ).first()
    if not row:
        row = MatterCustomField(matter_id=matter_id, namespace=_FAMILY_PREF_NAMESPACE, data={})
        db.session.add(row)

    data = dict(row.data or {}) if isinstance(row.data, dict) else {}
    excluded = _load_family_excluded_related_ids(data, self_matter_id=matter_id)
    if target_matter_id in excluded:
        return False
    excluded.append(target_matter_id)
    data[_FAMILY_EXCLUDE_KEY] = excluded
    row.data = data
    return True


def _remove_auto_related_exclusion(matter_id: str, target_matter_id: str) -> bool:
    matter_id = (matter_id or "").strip()
    target_matter_id = (target_matter_id or "").strip()
    if not matter_id or not target_matter_id or matter_id == target_matter_id:
        return False

    row = MatterCustomField.query.filter_by(
        matter_id=matter_id, namespace=_FAMILY_PREF_NAMESPACE
    ).first()
    if not row or not isinstance(row.data, dict):
        return False

    data = dict(row.data or {})
    excluded = _load_family_excluded_related_ids(data, self_matter_id=matter_id)
    if target_matter_id not in excluded:
        return False
    data[_FAMILY_EXCLUDE_KEY] = [mid for mid in excluded if mid != target_matter_id]
    if not data[_FAMILY_EXCLUDE_KEY]:
        data.pop(_FAMILY_EXCLUDE_KEY, None)
    row.data = data
    return True


def _cleanup_degenerate_families(family_ids: set[str]) -> int:
    if not family_ids:
        return 0

    counts: dict[str, int] = {}
    rows = (
        db.session.query(MatterFamily.family_id, func.count(MatterFamily.mf_id))
        .filter(MatterFamily.family_id.in_(family_ids))
        .group_by(MatterFamily.family_id)
        .all()
    )
    for fam_id, cnt in rows or []:
        key = (fam_id or "").strip()
        if key:
            try:
                counts[key] = int(cnt or 0)
            except Exception:
                counts[key] = 0

    cleanup_ids = {fam_id for fam_id in family_ids if fam_id and counts.get(fam_id, 0) < 2}
    if not cleanup_ids:
        return 0
    MatterFamily.query.filter(MatterFamily.family_id.in_(cleanup_ids)).delete(
        synchronize_session=False
    )
    return (
        Family.query.filter(Family.family_id.in_(cleanup_ids)).delete(synchronize_session=False)
        or 0
    )


def _collect_connected_family_component(
    *, seed_matter_ids: set[str] | None = None, seed_family_ids: set[str] | None = None
) -> tuple[set[str], set[str]]:
    known_mids = {(m or "").strip() for m in (seed_matter_ids or set()) if (m or "").strip()}
    known_fams = {(f or "").strip() for f in (seed_family_ids or set()) if (f or "").strip()}
    if not known_mids and not known_fams:
        return set(), set()

    for _ in range(64):
        changed = False
        if known_mids:
            fam_rows = (
                db.session.query(MatterFamily.family_id)
                .filter(MatterFamily.matter_id.in_(sorted(known_mids)))
                .distinct()
                .all()
            )
            for (fam_id,) in fam_rows or []:
                fid = (fam_id or "").strip()
                if fid and fid not in known_fams:
                    known_fams.add(fid)
                    changed = True
        if known_fams:
            mid_rows = (
                db.session.query(MatterFamily.matter_id)
                .filter(MatterFamily.family_id.in_(sorted(known_fams)))
                .distinct()
                .all()
            )
            for (matter_id,) in mid_rows or []:
                mid = (matter_id or "").strip()
                if mid and mid not in known_mids:
                    known_mids.add(mid)
                    changed = True
        if not changed:
            break
    return known_fams, known_mids


@bp.route("/<case_id>")
@login_required
def case_detail(case_id):
    ctx = build_case_detail_context(case_id, request.args, current_user)
    prefill = _extract_prefill_params(request.args)
    if prefill:
        ctx["prefill"] = prefill
    return render_template("case/matter_view.html", **ctx)


@bp.route("/<case_id>/api/notice-send-semi-close/ack", methods=["POST"])
@login_required
def notice_send_semi_close_ack(case_id: str):
    require_matter_access(str(case_id), action="edit_case")

    payload = request.get_json(silent=True) or {}
    docket_id = str(payload.get("docket_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()

    if not docket_id:
        return jsonify({"error": "docket_id_required"}), 400
    if decision not in {"yes", "no"}:
        return jsonify({"error": "invalid_decision"}), 400

    try:
        from app.services.deadlines.notice_send_semi_close import ack_notice_send_prompt

        ok = ack_notice_send_prompt(
            matter_id=str(case_id),
            docket_id=docket_id,
            decision=decision,
            actor_user_id=getattr(current_user, "id", None),
        )
        if not ok:
            db.session.rollback()
            return jsonify({"error": "task_not_found"}), 404

        db.session.commit()
        return jsonify({"success": True})
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "notice_send_semi_close_ack failed (case_id=%s, docket_id=%s, decision=%s): %s",
            case_id,
            docket_id,
            decision,
            exc,
        )
        return jsonify({"error": "ack_failed"}), 500


@bp.route("/<case_id>/section/<section>")
@login_required
def case_detail_section(case_id: str, section: str):
    section_key = (section or "").strip().lower()
    template_name = _CASE_DETAIL_SECTION_TEMPLATES.get(section_key)
    if not template_name:
        abort(404)

    if (request.headers.get("HX-Request") or "").lower() != "true":
        anchor = _CASE_DETAIL_SECTION_HASH_TARGETS.get(section_key, "")
        target = url_for("case_work.case_detail", case_id=case_id)
        if anchor:
            target = f"{target}#{anchor}"
        return redirect(target)

    view_mode = "page"
    if section_key == "history":
        view_mode = "history_panel"
    elif section_key == "files":
        view_mode = "files_panel"
    elif section_key == "deadlines":
        view_mode = "deadlines_panel"
    elif section_key == "memo":
        view_mode = "memo_panel"
    elif section_key == "cost":
        view_mode = "cost_panel"
    elif section_key == "annuity":
        view_mode = "annuity_panel"
    elif section_key == "alarm":
        view_mode = "alarm_panel"

    ctx = build_case_detail_context(case_id, request.args, current_user, view_mode=view_mode)
    prefill = _extract_prefill_params(request.args)
    if prefill:
        ctx["prefill"] = prefill
    return render_template(template_name, **ctx)


@bp.route("/matter/<case_id>/section/<section>", endpoint="case_detail_section_legacy")
@login_required
def case_detail_section_legacy(case_id: str, section: str):
    # Keep the historical `/case/matter/.../section/...` links working for API clients.
    return case_detail_section(case_id, section)


@bp.route("/<case_id>/copy", methods=["POST"])
@login_required
def copy_case(case_id: str):
    source = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    source_overview = VMatterOverview.query.get(case_id)
    source_div, source_type = _infer_case_kind(source, source_overview)
    source_storage_div, source_storage_type = resolve_public_case_kind_for_matter(
        source,
        source_overview,
    )
    if not source_type:
        flash("Matter Type    Matter Create  none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    namespace = None
    allowed_keys: set[str] = set()
    try:
        profile = CaseParameterService.get_case_profile(source_div, source_type)
        namespace = profile.namespace
        allowed_keys = set(profile.allowed_keys or [])
    except Exception:
        current_app.logger.exception(
            "copy_case: failed to resolve profile (matter_id=%s, division=%s, type=%s)",
            source.matter_id,
            source_div,
            source_type,
        )

    source_registry = {}
    if namespace:
        source_registry_row = MatterCustomField.query.filter_by(
            matter_id=str(source.matter_id), namespace=namespace
        ).first()
        source_registry = dict(source_registry_row.data or {}) if source_registry_row else {}
    source_basic_row = MatterCustomField.query.filter_by(
        matter_id=str(source.matter_id), namespace="basic"
    ).first()
    source_basic = dict(source_basic_row.data or {}) if source_basic_row else {}

    try:
        new_ref = _generate_copy_our_ref(source)
        today_iso = date.today().isoformat()

        copied_registry = _copyable_registry_data(source_registry, allowed_keys)
        copied_basic = _copyable_basic_data(source_basic)
        for person_key in _COPY_PEOPLE_KEYS:
            raw_value = copied_registry.get(person_key)
            if not raw_value:
                raw_value = source_registry.get(person_key) or source_basic.get(person_key)
            if raw_value is None:
                continue
            if isinstance(raw_value, str):
                raw_value = raw_value.strip()
                if not raw_value:
                    continue
            copied_registry[person_key] = raw_value
            if person_key in _COPY_BASIC_KEYS:
                copied_basic[person_key] = raw_value

        copied_right_group = source_storage_div or None
        copied_retained_at = (source.retained_at or "").strip() or today_iso
        copied_right_name = (source.right_name or "").strip() or None

        copied_matter = Matter(
            our_ref=new_ref,
            right_name=copied_right_name,
            right_group=copied_right_group,
            matter_type=source_storage_type or source_type,
            retained_at=copied_retained_at,
            entered_at=today_iso,
        )
        db.session.add(copied_matter)
        db.session.flush()

        if namespace and copied_registry:
            db.session.add(
                MatterCustomField(
                    matter_id=str(copied_matter.matter_id),
                    namespace=namespace,
                    data=copied_registry,
                )
            )

        if copied_basic:
            db.session.add(
                MatterCustomField(
                    matter_id=str(copied_matter.matter_id),
                    namespace="basic",
                    data=copied_basic,
                )
            )

        copied_assignment_count = 0
        source_assignments = MatterStaffAssignment.query.filter_by(
            matter_id=str(source.matter_id)
        ).all()
        for assignment in source_assignments:
            db.session.add(
                MatterStaffAssignment(
                    matter_id=str(copied_matter.matter_id),
                    staff_party_id=assignment.staff_party_id,
                    staff_role_code=assignment.staff_role_code,
                    raw_text=assignment.raw_text,
                )
            )
            copied_assignment_count += 1

        if copied_assignment_count == 0:
            staff_pid = (getattr(current_user, "staff_party_id", None) or "").strip()
            if staff_pid:
                username = (getattr(current_user, "username", None) or "").strip() or None
                db.session.add(
                    MatterStaffAssignment(
                        matter_id=str(copied_matter.matter_id),
                        staff_party_id=staff_pid,
                        staff_role_code="manager",
                        raw_text=username,
                    )
                )
                db.session.add(
                    MatterStaffAssignment(
                        matter_id=str(copied_matter.matter_id),
                        staff_party_id=staff_pid,
                        staff_role_code="attorney",
                        raw_text=username,
                    )
                )

        try:
            upsert_case_flat_index(str(copied_matter.matter_id))
        except Exception:
            current_app.logger.exception(
                "copy_case: failed to refresh flat index (matter_id=%s)", copied_matter.matter_id
            )

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("copy_case failed (source_matter_id=%s)", source.matter_id)
        flash(" Matter Create In Progress Error .", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    flash(
        f" Matter Create: {source.our_ref or ''} → {copied_matter.our_ref or ''}",
        "success",
    )
    return redirect(url_for("case_work.case_detail", case_id=copied_matter.matter_id))


def _sync_related_application_result(*, matter_id: str, result: dict) -> None:
    target = (result.get("target") or "").strip().lower()
    data = dict(result.get("data") or {})
    if target == "pct":
        _sync_matter_identifiers_from_pct(matter_id=matter_id, pct=data)
        _sync_matter_events_from_pct(matter_id=matter_id, pct=data)
    elif target == "madrid":
        _sync_matter_identifiers_from_out_trademark(matter_id=matter_id, out_tm=data)
        _sync_matter_events_from_out_trademark(matter_id=matter_id, out_tm=data)
    elif target == "hague":
        _sync_matter_identifiers_from_out_design(matter_id=matter_id, out_design=data)
        _sync_matter_events_from_out_design(matter_id=matter_id, out_design=data)


def _sync_related_application_core_dockets(*, matter_id: str, result: dict) -> None:
    data = dict(result.get("data") or {})
    filing_deadline = str(data.get("filing_deadline") or "").strip()
    if filing_deadline:
        from app.services.deadlines.docket_service import upsert_filing_docket

        upsert_filing_docket(
            str(matter_id),
            filing_deadline,
            deadline_type=str(data.get("filing_deadline_type") or "").strip() or None,
            commit=False,
        )

    application_date = str(data.get("application_date") or data.get("filing_date") or "").strip()
    if application_date:
        from app.services.deadlines.docket_service import complete_filing_docket

        complete_filing_docket(str(matter_id), application_date, commit=False)


def _related_application_apply(case_id: str):
    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    try:
        family_ctx = _build_base(str(case_id), {}, current_user)
        family_ctx["_current_user"] = current_user
        family = _build_family_section(family_ctx)
        related_rows = family.get("related_family_rows") or []

        result = apply_related_application_suggestion(
            matter=matter,
            related_family_rows=related_rows,
        )
        before_data = dict(result.get("before_data") or {})

        if not result.get("changed"):
            db.session.rollback()
            target_label = (result.get("target_label") or "Registry").strip()
            flash(
                f"Related applicationsfrom Auto   {target_label}   not found.",
                "info",
            )
            return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

        data = dict(result.get("data") or {})
        _sync_related_application_result(matter_id=str(case_id), result=result)
        _sync_related_application_core_dockets(matter_id=str(case_id), result=result)

        from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

        ensure_mgmt_deadlines_for_matter(str(case_id), commit=False)
        apply_auto_status_cache_to_matter(matter=matter, memo=(matter.memo or "").strip())

        try:
            upsert_case_flat_index(str(case_id))
        except Exception:
            current_app.logger.exception(
                "related_application_apply: failed to refresh flat index (matter_id=%s)",
                case_id,
            )

        change_keys = [field.get("key") for field in result.get("changes") or [] if field.get("key")]
        audit_prefix = (result.get("target") or "case").strip() or "case"
        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name=f"{audit_prefix}.related_application_apply",
            actor_user_id=getattr(current_user, "id", None),
            old_value={key: before_data.get(key) for key in change_keys},
            new_value={key: data.get(key) for key in change_keys},
        )

        db.session.commit()
        source = ((result.get("suggestion") or {}).get("source") or {}).get("our_ref") or "Related applications"
        labels = [
            str(field.get("label") or field.get("key") or "").strip()
            for field in (result.get("changes") or [])
        ]
        labels = [x for x in labels if x]
        storage_label = (result.get("storage_label") or result.get("target_label") or "Registry").strip()
        flash(f"{source}  {storage_label} : {', '.join(labels)}", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Related application apply failed (case_id=%s)", case_id)
        flash("Apply related application information In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))


@bp.route("/<case_id>/related-application/apply", methods=["POST"])
@login_required
def related_application_apply(case_id: str):
    return _related_application_apply(case_id)


@bp.route("/<case_id>/pct-related-application/apply", methods=["POST"])
@login_required
def pct_related_application_apply(case_id: str):
    return _related_application_apply(case_id)


@bp.route("/<case_id>/family/link", methods=["POST"])
@login_required
def family_link(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    target_ref = (request.form.get("target_ref") or "").strip()
    target_id = (request.form.get("target_matter_id") or "").strip()
    if not target_id and target_ref:
        target_id = resolve_matter_id_for_case_ref(target_ref) or ""

    if not target_ref and not target_id:
        flash("Link Our Ref Input .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

    if not target_id:
        flash(" Ref not found.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

    if str(target_id) == str(case_id):
        flash(" Matter Link  none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

    require_matter_access(str(target_id), action="edit_case")

    family_key = (request.form.get("family_key") or "").strip()

    try:
        from app.services.matter.matter_family_service import link_matters_into_family

        primary = Matter.query.get_or_404(case_id)
        target = Matter.query.get_or_404(target_id)
        _fam_id, fam_key, _created = link_matters_into_family(
            primary_matter=primary,
            target_matter=target,
            explicit_family_key=family_key or None,
            prefer_primary=True,
            link_role="manual",
            actor=current_user,
        )
        _remove_auto_related_exclusion(str(case_id), str(target_id))
        _remove_auto_related_exclusion(str(target_id), str(case_id))
        db.session.commit()
        msg = f"Family Link: {primary.our_ref or ''} ↔ {target.our_ref or ''}"
        if fam_key:
            msg += f" (Family Key: {fam_key})"
        flash(msg, "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "warning")
    except PermissionError as e:
        db.session.rollback()
        flash(str(e), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Family link failed (case_id=%s, target_id=%s)", case_id, target_id
        )
        flash("Family Link In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))


@bp.route("/<case_id>/family/disconnect", methods=["POST"])
@login_required
def family_disconnect(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    target_id = (request.form.get("target_matter_id") or "").strip()
    target_ref = (request.form.get("target_ref") or "").strip()
    if not target_id and target_ref:
        target_id = resolve_matter_id_for_case_ref(target_ref) or ""

    if not target_id:
        flash("Clear Our Ref not found.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

    if str(target_id) == str(case_id):
        flash(" Matter Clear  none.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))

    target = Matter.query.get_or_404(target_id)
    require_matter_access(str(target_id), action="edit_case")

    try:
        source_family_ids, _source_component_mids = _collect_connected_family_component(
            seed_matter_ids={str(case_id)}
        )
        target_family_ids, _target_component_mids = _collect_connected_family_component(
            seed_matter_ids={str(target_id)}
        )
        shared_family_ids = source_family_ids.intersection(target_family_ids)
        removed_links = 0
        removed_families = 0
        if shared_family_ids:
            removed_links = (
                MatterFamily.query.filter(
                    MatterFamily.matter_id == str(target_id),
                    MatterFamily.family_id.in_(shared_family_ids),
                ).delete(synchronize_session=False)
                or 0
            )
            if removed_links:
                removed_families = _cleanup_degenerate_families(shared_family_ids)

        excluded_source = _upsert_auto_related_exclusion(str(case_id), str(target_id))
        excluded_target = _upsert_auto_related_exclusion(str(target_id), str(case_id))
        db.session.commit()

        details = []
        if removed_links:
            details.append(f"Family link {removed_links}items Clear")
        if removed_families:
            details.append(f" Family {removed_families}items ")
        if excluded_source or excluded_target:
            details.append("AutoLink  Registration")
        if not details:
            flash(" Link Clear/ Process Status.", "info")
        else:
            flash(
                f"Link Clear Done: {target.our_ref or target_id} ({', '.join(details)})",
                "success",
            )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Family disconnect failed (case_id=%s, target_id=%s)", case_id, target_id
        )
        flash("Link Clear In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-family"))


@bp.route("/<case_id>/memo/add", methods=["POST"])
@login_required
def memo_add(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    body = (request.form.get("body") or "").strip()
    files = [
        f
        for f in request.files.getlist("attachments")
        if f and (getattr(f, "filename", None) or "").strip()
    ]
    if not body and not files:
        flash("Notes Content Input .", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))

    try:
        memo = MatterMemo(
            matter_id=str(case_id),
            body=body,
            created_by_id=(
                current_user.id if getattr(current_user, "is_authenticated", False) else None
            ),
            created_by_name=(getattr(current_user, "username", None) or "Guest"),
        )
        db.session.add(memo)
        db.session.flush()

        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="memo.add",
            actor_user_id=getattr(current_user, "id", None),
            old_value=None,
            new_value={"memo_id": memo.id, "preview": body[:200]},
        )

        if files:
            file_service = get_file_asset_service()
            now = datetime.now()
            subdir = str(Path("memo") / now.strftime("%Y/%m"))
            seen_file_ids = set()
            for file in files:
                staged = file_service.stage_upload(file, subdir=subdir)
                file_id = str(staged.file_asset_id)
                if file_id in seen_file_ids:
                    continue
                seen_file_ids.add(file_id)
                db.session.add(
                    MatterMemoFileAsset(
                        memo_id=memo.id,
                        file_asset_id=file_id,
                        role="attachment",
                        created_at=now,
                    )
                )

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Memo add failed (case_id=%s)", case_id)
        flash("Notes Save In Progress Error .", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))
    flash("Notes Add.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))


@bp.route("/<case_id>/memo/<int:memo_id>/delete", methods=["POST"])
@login_required
def memo_delete(case_id: str, memo_id: int):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    memo = MatterMemo.query.get_or_404(memo_id)
    if memo.matter_id != str(case_id):
        abort(404)

    is_admin = (getattr(current_user, "role", "") or "").lower() == "admin"
    if not is_admin:
        if memo.created_by_id is not None and memo.created_by_id != getattr(
            current_user, "id", None
        ):
            flash("You do not have permission to delete this record.", "warning")
            return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))

    attachment_ids = (
        db.session.execute(
            text("SELECT file_asset_id FROM matter_memo_file_asset WHERE memo_id = :mid"),
            {"mid": memo_id},
        )
        .scalars()
        .all()
    )
    db.session.execute(
        text("DELETE FROM matter_memo_file_asset WHERE memo_id = :mid"),
        {"mid": memo_id},
    )

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="memo.delete",
        actor_user_id=getattr(current_user, "id", None),
        old_value={"memo_id": memo.id, "preview": (memo.body or "")[:200]},
        new_value={"deleted": True},
    )

    db.session.delete(memo)
    db.session.commit()
    if attachment_ids:
        file_service = get_file_asset_service()
        for fid in {str(x) for x in attachment_ids if x}:
            try:
                file_service.purge_if_orphan(fid, min_age_days=0, dry_run=False)
            except Exception:
                current_app.logger.warning(
                    "GC: failed to purge memo attachment (case_id=%s, memo_id=%s, fid=%s)",
                    case_id,
                    memo_id,
                    fid,
                )
    flash("Notes Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))


@bp.route("/<case_id>/memo/<int:memo_id>/attachment/<memo_file_id>/delete", methods=["POST"])
@login_required
def memo_attachment_delete(case_id: str, memo_id: int, memo_file_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    memo = MatterMemo.query.get_or_404(memo_id)
    if memo.matter_id != str(case_id):
        abort(404)

    is_admin = (getattr(current_user, "role", "") or "").lower() == "admin"
    if not is_admin:
        if memo.created_by_id is not None and memo.created_by_id != getattr(
            current_user, "id", None
        ):
            flash("You do not have permission to delete this record.", "warning")
            return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))

    link = MatterMemoFileAsset.query.filter_by(
        memo_id=memo_id,
        memo_file_id=memo_file_id,
    ).first()
    if not link:
        abort(404)

    file_asset_id = link.file_asset_id
    db.session.delete(link)
    db.session.commit()

    if file_asset_id:
        try:
            file_service = get_file_asset_service()
            file_service.purge_if_orphan(str(file_asset_id), min_age_days=0, dry_run=False)
        except Exception:
            current_app.logger.warning(
                "GC: failed to purge memo attachment (case_id=%s, memo_file_id=%s)",
                case_id,
                memo_file_id,
            )

    flash("File Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-memo"))
