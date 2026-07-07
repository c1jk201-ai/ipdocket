"""Routes for progress status (Progress) CRUD."""

from __future__ import annotations

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.matter import MatterProgress
from app.models.ip_records import Matter
from app.services.case.case_audit_service import record_case_audit
from app.services.citations.cited_reference_service import (
    create_ids_tasks_for_us_family,
    is_notice_doc_for_citations,
    replace_office_action_citations_from_text,
)
from app.services.history.office_action_service import OfficeActionData, get_office_action_service
from app.services.matter.matter_auto_status import date_only_str
from app.utils.permissions import require_matter_access
from app.utils.policy_sql import policy_text as text
from app.utils.timezone import today_local


@bp.route("/<case_id>/progress", methods=["GET"])
@login_required
def list_progress(case_id: str):
    """API endpoint to list all progress entries for a case."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="view")
    entries = (
        MatterProgress.query.filter_by(matter_id=str(case_id))
        .order_by(MatterProgress.created_at.desc())
        .all()
    )
    return jsonify(
        [
            {
                "id": e.id,
                "content": e.content,
                "category": e.category,
                "created_by": e.created_by.username if e.created_by else e.created_by_name,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    )


@bp.route("/<case_id>/progress/add", methods=["POST"])
@login_required
def add_progress(case_id: str):
    """Add a new progress entry."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    content = (request.form.get("content") or "").strip()
    if not content:
        flash("Content Input.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))

    category = (request.form.get("category") or "general").strip()

    entry = MatterProgress(
        matter_id=str(case_id),
        content=content,
        category=category,
        created_by_id=current_user.id if current_user.is_authenticated else None,
        created_by_name=current_user.username if current_user.is_authenticated else None,
    )
    db.session.add(entry)
    db.session.commit()

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="progress.add",
        actor_user_id=getattr(current_user, "id", None),
        old_value=None,
        new_value={"progress_id": entry.id, "preview": (content or "")[:200]},
    )

    flash("Open  Add.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))


@bp.route("/<case_id>/progress/oa-citations", methods=["POST"])
@login_required
def save_oa_citations(case_id: str):
    """Save OA cited references from the progress section."""
    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    oa_id = (request.form.get("oa_id") or "").strip()
    citation_text = request.form.get("cited_references") or ""
    if not oa_id:
        if not citation_text.strip():
            flash(" Input.", "warning")
            return redirect(
                url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress")
            )
        doc_name = (request.form.get("doc_name") or "").strip() or "Office action"
        if not is_notice_doc_for_citations(doc_name):
            flash(" Notice Registration  exists.", "warning")
            return redirect(
                url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress")
            )
        data = OfficeActionData(
            matter_id=str(case_id),
            doc_name=doc_name,
            received_date=today_local().isoformat(),
            notified_date=date_only_str(request.form.get("notified_date") or "") or None,
            due_date=date_only_str(request.form.get("due_date") or "") or None,
            extended_due_date=date_only_str(request.form.get("extended_due_date") or "") or None,
        )
        result = get_office_action_service().create(data)
        if not result.success or not result.oa_id:
            db.session.rollback()
            flash(" / ".join(result.errors or ["Office correspondence Registration Failed."]), "danger")
            return redirect(
                url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress")
            )
        oa_id = str(result.oa_id)

    row = db.session.execute(
        text(
            """
            SELECT doc_name
            FROM office_action
            WHERE oa_id = :oid AND matter_id = :mid
        """
        ).execution_options(policy_bypass=True),
        {"oid": oa_id, "mid": str(case_id)},
    ).fetchone()
    if not row:
        flash("Office correspondence   none.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))
    if not is_notice_doc_for_citations(row[0]):
        flash(" Notice Registration  exists.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))

    rows = replace_office_action_citations_from_text(
        matter_id=str(case_id),
        office_action_id=str(oa_id),
        text_value=citation_text,
        source="manual_progress",
    )
    ids_result = create_ids_tasks_for_us_family(
        source_matter_id=str(case_id),
        source_oa_id=str(oa_id),
        citations=rows,
        source_doc_name=row[0] or "",
        actor_id=getattr(current_user, "id", None),
    )
    db.session.commit()

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="progress.oa_citations.save",
        actor_user_id=getattr(current_user, "id", None),
        old_value=None,
        new_value={"oa_id": oa_id, "cited_references": len(rows)},
    )

    if rows:
        msg = f"Saved {len(rows)} office action item(s)."
        if ids_result.created_count:
            msg += f" Created {ids_result.created_count} US family IDS task(s)."
        flash(msg, "success")
    else:
        flash("OA  .", "info")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))


@bp.route("/<case_id>/progress/<int:progress_id>/delete", methods=["POST"])
@login_required
def delete_progress(case_id: str, progress_id: int):
    """Delete a progress entry."""
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")
    entry = MatterProgress.query.filter_by(id=progress_id, matter_id=str(case_id)).first_or_404()

    db.session.delete(entry)
    db.session.commit()

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="progress.delete",
        actor_user_id=getattr(current_user, "id", None),
        old_value={"progress_id": entry.id, "preview": (entry.content or "")[:200]},
        new_value={"deleted": True},
    )

    flash("Open  Delete.", "success")
    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-progress"))
