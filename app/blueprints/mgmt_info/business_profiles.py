from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.billing_invoices.auth import get_current_user, log_audit, role_required
from app.blueprints.billing_invoices.db import (
    _ensure_column,
    _get_column_names,
    get_all_business_profiles,
    get_db,
    row_to_dict,
)
from app.blueprints.billing_invoices.routes.admin import _create_backup_file, _write_backup_meta
from app.blueprints.mgmt_info import bp
from app.services.billing.utils import save_logo
from app.utils.error_logging import report_swallowed_exception


def _ensure_sort_order_column(conn) -> bool:
    """Best-effort add sort_order column for business profiles."""
    try:
        _ensure_column(conn, "business_profile", "sort_order", "INTEGER DEFAULT 0")
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="mgmt_info.business_profiles._ensure_sort_order_column.ensure_column",
            log_key="mgmt_info.business_profiles._ensure_sort_order_column.ensure_column",
            log_window_seconds=300,
        )
    try:
        cols = _get_column_names(conn, "business_profile")
        if "sort_order" in cols:
            try:
                conn.execute("UPDATE business_profile SET sort_order=0 WHERE sort_order IS NULL")
            except Exception as exc:
                report_swallowed_exception(
                    exc,
                    context="mgmt_info.business_profiles._ensure_sort_order_column.backfill",
                    log_key="mgmt_info.business_profiles._ensure_sort_order_column.backfill",
                    log_window_seconds=300,
                )
            return True
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="mgmt_info.business_profiles._ensure_sort_order_column.get_columns",
            log_key="mgmt_info.business_profiles._ensure_sort_order_column.get_columns",
            log_window_seconds=300,
        )
    return False


@bp.route("/business-profiles")
@role_required("admin", "staff")
def list_business_profiles():
    return render_template(
        "mgmt_info/business_profiles_list.html", profiles=get_all_business_profiles()
    )


@bp.route("/business-profiles/new", methods=["GET", "POST"])
@role_required("admin")
def new_business_profile():
    if request.method == "POST":
        conn = get_db()
        has_sort_order = _ensure_sort_order_column(conn)
        logo_path = None
        if "logo_file" in request.files and request.files["logo_file"].filename:
            try:
                logo_path = save_logo(request.files["logo_file"], None)
            except ValueError as exc:
                conn.close()
                flash(str(exc), "error")
                return redirect(url_for("mgmt_info.new_business_profile"))
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            abort(400, "Representative Input.")
        exists = conn.execute(
            "SELECT id FROM business_profile WHERE name=?",
            (name,),
        ).fetchone()
        if exists:
            conn.close()
            abort(400, "  Representative.")
        sort_order = None
        sort_raw = (request.form.get("sort_order") or "").strip()
        if has_sort_order and sort_raw:
            try:
                sort_order = int(sort_raw)
            except Exception:
                sort_order = None
        if has_sort_order and sort_order is None:
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) FROM business_profile"
                ).fetchone()
                sort_order = int(row[0] or 0) + 1
            except Exception:
                sort_order = 0
        try:
            if has_sort_order:
                conn.execute(
                    "INSERT INTO business_profile (name, sort_order, address, email, phone, tax_id, currency, vat_rate, next_invoice_no, logo_path, bank_account, language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        sort_order,
                        request.form.get("address"),
                        request.form.get("email"),
                        request.form.get("phone"),
                        request.form.get("tax_id"),
                        (request.form.get("currency") or "USD").upper(),
                        float(request.form.get("vat_rate") or 0),
                        int(request.form.get("next_invoice_no") or 1),
                        logo_path,
                        request.form.get("bank_account"),
                        request.form.get("language") or "en",
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO business_profile (name, address, email, phone, tax_id, currency, vat_rate, next_invoice_no, logo_path, bank_account, language) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        name,
                        request.form.get("address"),
                        request.form.get("email"),
                        request.form.get("phone"),
                        request.form.get("tax_id"),
                        (request.form.get("currency") or "USD").upper(),
                        float(request.form.get("vat_rate") or 0),
                        int(request.form.get("next_invoice_no") or 1),
                        logo_path,
                        request.form.get("bank_account"),
                        request.form.get("language") or "en",
                    ),
                )
            conn.commit()
            conn.close()
            return redirect(url_for("mgmt_info.list_business_profiles"))
        except Exception:
            conn.close()
            abort(400, "Business profile Create Failed. Inputvalue Confirm.")
    return render_template("mgmt_info/business_profile_form.html", profile=None)


@bp.route("/business-profiles/<int:profile_id>")
@role_required("admin", "staff")
def view_business_profile(profile_id):
    conn = get_db()
    profile = conn.execute("SELECT * FROM business_profile WHERE id=?", (profile_id,)).fetchone()
    conn.close()
    if not profile:
        abort(404)
    profile = row_to_dict(profile)
    return render_template("mgmt_info/business_profile_view.html", profile=profile)


@bp.route("/business-profiles/<int:profile_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_business_profile(profile_id):
    conn = get_db()
    has_sort_order = _ensure_sort_order_column(conn)
    profile = conn.execute("SELECT * FROM business_profile WHERE id=?", (profile_id,)).fetchone()
    if not profile:
        conn.close()
        abort(404)
    profile = row_to_dict(profile)
    if request.method == "POST":
        logo_path = profile["logo_path"]
        if "logo_file" in request.files and request.files["logo_file"].filename:
            try:
                logo_path = save_logo(request.files["logo_file"], logo_path)
            except ValueError as exc:
                conn.close()
                flash(str(exc), "error")
                return redirect(url_for("mgmt_info.edit_business_profile", profile_id=profile_id))
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            abort(400, "Representative Input.")
        exists = conn.execute(
            "SELECT id FROM business_profile WHERE name=? AND id<>?",
            (name, profile_id),
        ).fetchone()
        if exists:
            conn.close()
            abort(400, "  Representative.")
        sort_order = None
        sort_raw = (request.form.get("sort_order") or "").strip()
        if has_sort_order and sort_raw:
            try:
                sort_order = int(sort_raw)
            except Exception:
                sort_order = None
        if has_sort_order and sort_order is None:
            try:
                sort_order = int(profile["sort_order"] or 0)
            except Exception:
                sort_order = 0
        try:
            if has_sort_order:
                conn.execute(
                    "UPDATE business_profile SET name=?, sort_order=?, address=?, email=?, phone=?, tax_id=?, currency=?, vat_rate=?, next_invoice_no=?, logo_path=?, bank_account=?, language=? WHERE id=?",
                    (
                        name,
                        sort_order,
                        request.form.get("address"),
                        request.form.get("email"),
                        request.form.get("phone"),
                        request.form.get("tax_id"),
                        (request.form.get("currency") or "USD").upper(),
                        float(request.form.get("vat_rate") or 0),
                        int(request.form.get("next_invoice_no") or 1),
                        logo_path,
                        request.form.get("bank_account"),
                        request.form.get("language") or "en",
                        profile_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE business_profile SET name=?, address=?, email=?, phone=?, tax_id=?, currency=?, vat_rate=?, next_invoice_no=?, logo_path=?, bank_account=?, language=? WHERE id=?",
                    (
                        name,
                        request.form.get("address"),
                        request.form.get("email"),
                        request.form.get("phone"),
                        request.form.get("tax_id"),
                        (request.form.get("currency") or "USD").upper(),
                        float(request.form.get("vat_rate") or 0),
                        int(request.form.get("next_invoice_no") or 1),
                        logo_path,
                        request.form.get("bank_account"),
                        request.form.get("language") or "en",
                        profile_id,
                    ),
                )
            conn.commit()
            conn.close()
            return redirect(url_for("mgmt_info.list_business_profiles"))
        except Exception:
            conn.close()
            abort(400, "Business profile Edit Failed. Inputvalue Confirm.")
    conn.close()
    return render_template("mgmt_info/business_profile_form.html", profile=profile)


@bp.route("/business-profiles/<int:profile_id>/delete", methods=["POST"])
@role_required("admin")
def delete_business_profile(profile_id):
    conn = get_db()
    profile = conn.execute("SELECT * FROM business_profile WHERE id=?", (profile_id,)).fetchone()
    if not profile:
        conn.close()
        abort(404)

    count = conn.execute("SELECT COUNT(*) FROM business_profile").fetchone()[0]
    if count <= 1:
        conn.close()
        abort(400, "At least one business profile is required.")

    #  Business profile Link Invoice   Delete 
    inv_count = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE business_profile_id=?",
        (profile_id,),
    ).fetchone()[0]
    if inv_count > 0:
        conn.close()
        flash(
            " Business profile Link Invoice  Delete  none.  Invoice  Business profile Change Delete.",
            "error",
        )
        return redirect(url_for("mgmt_info.list_business_profiles"))

    # Pre-delete DB backup (restore point)
    try:
        backup_path = _create_backup_file()
        try:
            user = get_current_user()
        except Exception:
            user = None
        try:
            _write_backup_meta(
                backup_path,
                source="forced",
                note=f"pre-delete business_profile id={profile_id} name={profile['name']}",
                tags=["pre-delete", "business_profile", f"id:{profile_id}"],
                created_by=(user["id"] if user else None),
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_info.business_profiles.delete.write_backup_meta",
                log_key="mgmt_info.business_profiles.delete.write_backup_meta",
                log_window_seconds=300,
            )
        try:
            log_audit(
                "backup.pre_delete",
                "business_profile",
                profile_id,
                f'{{"path": "{backup_path}", "name": "{profile["name"]}"}}',
            )
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="mgmt_info.business_profiles.delete.audit_pre_delete",
                log_key="mgmt_info.business_profiles.delete.audit_pre_delete",
                log_window_seconds=300,
            )
    except Exception as e:
        # Soft fail: If backup fails (e.g. no pg_dump), just warn and proceed
        flash(f"Backup Create Failed Delete Open. (Error: {e})", "warning")

    conn.execute("DELETE FROM business_profile WHERE id=?", (profile_id,))
    conn.commit()
    try:
        log_audit(
            "business_profile.delete",
            "business_profile",
            profile_id,
            f'{{"name": "{profile["name"]}"}}',
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="mgmt_info.business_profiles.delete.audit_delete",
            log_key="mgmt_info.business_profiles.delete.audit_delete",
            log_window_seconds=300,
        )
    conn.close()
    return redirect(url_for("mgmt_info.list_business_profiles"))
