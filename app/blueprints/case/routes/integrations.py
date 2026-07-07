from flask import flash, redirect, request, url_for
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.models.ip_records import Matter
from app.services.billing.invoice_bridge import InvoiceBridgeError
from app.services.billing.invoice_matter_link_usecase import InvoiceMatterLinkUseCase
from app.utils.permissions import matter_action, require_matter_access


@bp.route("/<case_id>/external-invoices/link", methods=["POST"])
@matter_action("invoice")
@login_required
def link_external_invoice_to_case(case_id: str):
    matter_id = str(case_id)
    Matter.query.get_or_404(matter_id)
    require_matter_access(str(case_id), action="invoice")

    ext_ref = (
        request.form.get("external_invoice_ref") or request.form.get("external_invoice_id") or ""
    ).strip()
    if not ext_ref:
        flash("External Invoice ID/ Input.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    try:
        InvoiceMatterLinkUseCase.link(
            matter_id=matter_id,
            external_invoice_ref=ext_ref,
            actor_id=getattr(current_user, "id", None),
        )
        flash("External Invoice Matter Link.", "success")
    except InvoiceBridgeError as exc:
        flash(f"Link Failed: {exc}", "danger")
    except Exception:
        flash("Link In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))


@bp.route("/<case_id>/external-invoices/unlink", methods=["POST"])
@matter_action("invoice")
@login_required
def unlink_external_invoice_from_case(case_id: str):
    matter_id = str(case_id)
    Matter.query.get_or_404(matter_id)
    require_matter_access(matter_id, action="invoice")
    ext_ref = (
        request.form.get("external_invoice_ref") or request.form.get("external_invoice_id") or ""
    ).strip()
    if not ext_ref:
        flash("External Invoice ID/ required.", "warning")
        return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))

    try:
        InvoiceMatterLinkUseCase.unlink(
            matter_id=matter_id,
            external_invoice_ref=ext_ref,
            actor_id=getattr(current_user, "id", None),
        )
        flash("External Invoice Link Clear.", "success")
    except InvoiceBridgeError as exc:
        flash(f"Clear Failed: {exc}", "danger")
    except Exception:
        flash("Clear In Progress Error .", "danger")

    return redirect(url_for("case_work.case_detail", case_id=case_id, _anchor="sec-cost"))
