from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.case import Case
from app.models.ip_records import Matter
from app.models.workflow import Workflow
from app.services.case.cascade_delete_service import (
    delete_matter_fk_children,
    delete_workflow_fk_children_for_matter,
)
from app.services.case.case_audit_service import record_case_audit
from app.services.case.status_task_cleanup import apply_case_status_side_effects
from app.utils.permissions import (
    matter_action,
    require_matter_access,
    resolve_matter_id_for_case_ref,
)
from app.utils.policy_sql import policy_text as text


@bp.route("/<case_id>/status/update", methods=["POST"])
@login_required
def update_status(case_id: str):
    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(matter.matter_id), action="edit_case")
    new_status = request.form.get("new_status")
    status_date_str = request.form.get("status_date")
    note = request.form.get("status_note")

    if not new_status or not status_date_str:
        flash("Status  Required Input Item.", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))

    try:
        # Update current status
        old_status = getattr(matter, "inhouse_status", None)
        matter.inhouse_status = new_status

        # Create history record
        from app.models.ip_records import MatterStatusHistory

        hist = MatterStatusHistory(
            matter_id=matter.matter_id,
            status=new_status,
            status_date=status_date_str,
            note=note,
            created_by_id=current_user.id,
            created_by_name=getattr(current_user, "name", str(current_user.id)),
        )
        db.session.add(hist)

        # ✅ Audit log
        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="status.inhouse_status",
            actor_user_id=getattr(current_user, "id", None),
            old_value={"inhouse_status": old_status},
            new_value={
                "inhouse_status": new_status,
                "status_date": status_date_str,
                "note": (note or "")[:200],
            },
        )

        db.session.commit()
        cleanup_result = apply_case_status_side_effects(
            matter_id=str(case_id),
            old_status=old_status,
            new_status=new_status,
            status_date=status_date_str,
            note=note,
            actor_id=getattr(current_user, "id", None),
            logger_override=current_app.logger,
        )
        if (
            cleanup_result.docket_closed
            or cleanup_result.workflow_closed
            or cleanup_result.worklog_closed
        ):
            flash(
                "Status . "
                f"Closed {cleanup_result.docket_closed} deadline(s) and "
                f"{cleanup_result.workflow_closed} task(s).",
                "success",
            )
        else:
            flash("Status .", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Matter status update failed (case_id=%s)", case_id)
        flash("Error .   Retry .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id))


@bp.route("/<case_id>/delete", methods=["POST"])
@matter_action("delete_case")
@login_required
def delete_matter(case_id: str):

    matter = Matter.query.get_or_404(case_id)
    require_matter_access(str(matter.matter_id), action="delete_case")
    matter_id = str(matter.matter_id)
    matter_snapshot = {
        "our_ref": getattr(matter, "our_ref", None),
        "matter_type": getattr(matter, "matter_type", None),
    }
    matter_refs = {
        (getattr(matter, "our_ref", None) or "").strip(),
        (getattr(matter, "old_our_ref", None) or "").strip(),
        (getattr(matter, "your_ref", None) or "").strip(),
    }
    try:

        # ✅ Audit log (Delete Matter    Delete  Log)
        record_case_audit(
            case_id=str(case_id),
            action="USER",
            field_name="matter.delete",
            actor_user_id=getattr(current_user, "id", None),
            old_value=matter_snapshot,
            new_value={"deleted": True},
        )


        # Matter from Delete  legacy Case(+deadlines/reminders)    .
        linked_refs = sorted({ref for ref in matter_refs if ref})
        if linked_refs:
            linked_legacy_cases = Case.query.filter(Case.ref_no.in_(linked_refs)).all()
            for legacy_case in linked_legacy_cases:
                resolved_mid = resolve_matter_id_for_case_ref(getattr(legacy_case, "ref_no", None))
                if resolved_mid and str(resolved_mid) != matter_id:
                    continue
                db.session.execute(
                    text(
                        """
                        DELETE FROM reminders
                        WHERE deadline_id IN (
                            SELECT id FROM deadlines WHERE case_id = :case_id
                        )
                        """
                    ),
                    {"case_id": int(legacy_case.id)},
                )
                db.session.delete(legacy_case)

        # Collect file_asset_ids referenced by this matter (best-effort) before deleting link rows.
        all_asset_ids: set[str] = set()
        try:
            mfa_assets = (
                db.session.execute(
                    text("SELECT file_asset_id FROM matter_file_asset WHERE matter_id = :mid"),
                    {"mid": matter_id},
                )
                .scalars()
                .all()
            )
            cfa_assets = (
                db.session.execute(
                    text(
                        """
                        SELECT cfa.file_asset_id
                        FROM communication_file_asset cfa
                        JOIN communication c ON c.comm_id = cfa.comm_id
                        WHERE c.matter_id = :mid
                        """
                    ),
                    {"mid": matter_id},
                )
                .scalars()
                .all()
            )
            ofa_assets = (
                db.session.execute(
                    text(
                        """
                        SELECT ofa.file_asset_id
                        FROM office_action_file_asset ofa
                        JOIN office_action oa ON oa.oa_id = ofa.oa_id
                        WHERE oa.matter_id = :mid
                        """
                    ),
                    {"mid": matter_id},
                )
                .scalars()
                .all()
            )
            mmfa_assets = (
                db.session.execute(
                    text(
                        """
                        SELECT mmfa.file_asset_id
                        FROM matter_memo_file_asset mmfa
                        JOIN matter_memo mm ON mm.id = mmfa.memo_id
                        WHERE mm.matter_id = :mid
                        """
                    ),
                    {"mid": matter_id},
                )
                .scalars()
                .all()
            )
            all_asset_ids = {
                str(x)
                for x in (set(mfa_assets) | set(cfa_assets) | set(ofa_assets) | set(mmfa_assets))
                if x
            }
        except Exception:
            current_app.logger.exception(
                "Failed to collect file_asset_ids for matter delete (matter_id=%s)",
                matter_id,
            )
            all_asset_ids = set()

        # --- Email ingestion / comm crosslinks cleanup (best-effort) ---
        # Unlink emails that reference communications of this matter before deleting communications.
        try:
            db.session.execute(
                text(
                    """
                    UPDATE email_message
                       SET linked_comm_id = NULL
                     WHERE linked_comm_id IN (
                           SELECT comm_id FROM communication WHERE matter_id = :mid
                     )
                    """
                ),
                {"mid": matter_id},
            )
        except Exception:
            current_app.logger.exception(
                "Failed to unlink email_message.linked_comm_id for matter delete (matter_id=%s)",
                matter_id,
            )

        # Clear suggested/selected pointers that point to this matter.
        try:
            db.session.execute(
                text(
                    """
                    UPDATE email_message
                       SET suggested_matter_id = NULL,
                           suggested_score = NULL,
                           suggested_reasons = NULL
                     WHERE suggested_matter_id = :mid
                    """
                ),
                {"mid": matter_id},
            )
            db.session.execute(
                text(
                    """
                    UPDATE email_message
                       SET selected_matter_id = NULL,
                           selected_by = NULL
                     WHERE selected_matter_id = :mid
                    """
                ),
                {"mid": matter_id},
            )
        except Exception:
            current_app.logger.exception(
                "Failed to clear email_message suggested/selected pointers (matter_id=%s)",
                matter_id,
            )

        # Remove stored link/candidate rows for this matter.
        try:
            db.session.execute(
                text("DELETE FROM email_message_matter_link WHERE matter_id = :mid"),
                {"mid": matter_id},
            )
            db.session.execute(
                text("DELETE FROM mail_match_candidate WHERE candidate_matter_id = :mid"),
                {"mid": matter_id},
            )
        except Exception:
            current_app.logger.exception(
                "Failed to delete email match rows for matter delete (matter_id=%s)",
                matter_id,
            )

        # --- DELETE DEPENDENTS (mirror general_edit delete cascade) ---
        # 1) Links to FileAssets
        db.session.execute(
            text("DELETE FROM matter_file_asset WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text(
                """
                DELETE FROM communication_file_asset
                WHERE comm_id IN (SELECT comm_id FROM communication WHERE matter_id = :mid)
                """
            ),
            {"mid": matter_id},
        )
        db.session.execute(
            text(
                """
                DELETE FROM office_action_file_asset
                WHERE oa_id IN (SELECT oa_id FROM office_action WHERE matter_id = :mid)
                """
            ),
            {"mid": matter_id},
        )
        db.session.execute(
            text(
                """
                DELETE FROM matter_memo_file_asset
                WHERE memo_id IN (SELECT id FROM matter_memo WHERE matter_id = :mid)
                """
            ),
            {"mid": matter_id},
        )

        # 2) Functional Records
        db.session.execute(
            text("DELETE FROM communication WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM office_action WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM docket_item WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        # Future-proof: remove any direct FK children of workflows discovered from DB metadata.
        delete_workflow_fk_children_for_matter(matter_id)
        # workflows    /  Delete.
        db.session.execute(
            text(
                """
                DELETE FROM workflow_checklist_item
                WHERE workflow_id IN (SELECT id FROM workflows WHERE case_id = :mid)
                """
            ),
            {"mid": matter_id},
        )
        db.session.execute(
            text(
                """
                DELETE FROM workflow_reminder_sent
                WHERE workflow_id IN (SELECT id FROM workflows WHERE case_id = :mid)
                """
            ),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM workflows WHERE case_id = :mid"),
            {"mid": matter_id},
        )

        # 3) Matter Details / indexes
        db.session.execute(
            text("DELETE FROM matter_identifier WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_event WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_custom_field WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_party_role WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_staff_assignment WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_memo WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_progress WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM matter_family WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM case_flat_index WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM external_invoice_case_link WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        db.session.execute(
            text("DELETE FROM external_invoice_case_map WHERE matter_id = :mid"),
            {"mid": matter_id},
        )
        # Future-proof: remove any additional direct FK children of matter.
        delete_matter_fk_children(
            matter_id,
            exclude_tables={
                "workflows",
                "external_invoice_case_link",
                "external_invoice_case_map",
            },
        )

        # 4) The Matter itself
        db.session.execute(
            text("DELETE FROM matter WHERE matter_id = :mid"),
            {"mid": matter_id},
        )

        db.session.commit()

        # Best-effort: purge orphaned FileAssets (disk + DB) after the transaction is committed.
        if all_asset_ids:
            try:
                from app.services.storage.file_asset_service import get_file_asset_service

                file_service = get_file_asset_service()
                for fid in {str(x) for x in all_asset_ids if x}:
                    try:
                        file_service.purge_if_orphan(fid, min_age_days=0, dry_run=False)
                    except Exception as e:
                        current_app.logger.warning(
                            "Failed to purge orphaned file asset (matter_id=%s, fid=%s): %s",
                            matter_id,
                            fid,
                            e,
                        )
            except Exception as e:
                current_app.logger.warning(
                    "Failed to initialize file service for asset purge (matter_id=%s): %s",
                    matter_id,
                    e,
                )

        flash("Matter Delete.", "success")
        return redirect(url_for("case_work.case_list"))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Matter delete failed (case_id=%s)", case_id)
        flash("Delete In Progress Error .   Retry .", "danger")
        return redirect(url_for("case_work.case_detail", case_id=case_id))


# ---------------------------------------------------------------------
# P2: Status Transition Wizard / ICS Export / TC->Invoice helper screens
# ---------------------------------------------------------------------
# NOTE: bp   below 2 bp .
from .p2_mount import register_p2_routes  # noqa: E402

register_p2_routes(bp)
