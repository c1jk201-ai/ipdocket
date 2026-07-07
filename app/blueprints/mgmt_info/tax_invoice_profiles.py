from flask import abort, flash, redirect, render_template, request, url_for

from app.blueprints.billing_invoices.auth import role_required
from app.blueprints.billing_invoices.db import (
    ensure_tax_invoice_profiles,
    get_all_tax_invoice_profiles,
    get_db,
    row_to_dict,
)
from app.blueprints.mgmt_info import bp


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


@bp.route("/tax-invoice-profiles")
@role_required("admin", "staff")
def list_tax_invoice_profiles():
    return render_template(
        "mgmt_info/tax_invoice_profiles_list.html",
        profiles=get_all_tax_invoice_profiles(),
    )


@bp.route("/tax-invoice-profiles/new", methods=["GET", "POST"])
@role_required("admin")
def new_tax_invoice_profile():
    if request.method == "POST":
        conn = get_db()
        ensure_tax_invoice_profiles(conn)
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            abort(400, " Input.")
        exists = conn.execute(
            "SELECT id FROM tax_invoice_profiles WHERE name=?",
            (name,),
        ).fetchone()
        if exists:
            conn.close()
            abort(400, "  .")
        is_default = 1 if _is_truthy(request.form.get("is_default")) else 0
        if is_default:
            conn.execute("UPDATE tax_invoice_profiles SET is_default=0")
        conn.execute(
            """
            INSERT INTO tax_invoice_profiles
            (name, tax_id, ceo_name, address, biz_type, biz_class, email, phone, is_default, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                name,
                request.form.get("tax_id"),
                request.form.get("ceo_name"),
                request.form.get("address"),
                request.form.get("biz_type"),
                request.form.get("biz_class"),
                request.form.get("email"),
                request.form.get("phone"),
                is_default,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("mgmt_info.list_tax_invoice_profiles"))
    return render_template("mgmt_info/tax_invoice_profile_form.html", profile=None)


@bp.route("/tax-invoice-profiles/<int:profile_id>")
@role_required("admin", "staff")
def view_tax_invoice_profile(profile_id):
    conn = get_db()
    ensure_tax_invoice_profiles(conn)
    profile = conn.execute(
        "SELECT * FROM tax_invoice_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    conn.close()
    if not profile:
        abort(404)
    return render_template(
        "mgmt_info/tax_invoice_profile_view.html",
        profile=row_to_dict(profile),
    )


@bp.route("/tax-invoice-profiles/<int:profile_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit_tax_invoice_profile(profile_id):
    conn = get_db()
    ensure_tax_invoice_profiles(conn)
    profile = conn.execute(
        "SELECT * FROM tax_invoice_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if not profile:
        conn.close()
        abort(404)
    profile = row_to_dict(profile)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            conn.close()
            abort(400, " Input.")
        exists = conn.execute(
            "SELECT id FROM tax_invoice_profiles WHERE name=? AND id<>?",
            (name, profile_id),
        ).fetchone()
        if exists:
            conn.close()
            abort(400, "  .")
        is_default = 1 if _is_truthy(request.form.get("is_default")) else 0
        if is_default:
            conn.execute("UPDATE tax_invoice_profiles SET is_default=0")
        conn.execute(
            """
            UPDATE tax_invoice_profiles
            SET name=?, tax_id=?, ceo_name=?, address=?, biz_type=?, biz_class=?, email=?, phone=?, is_default=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                name,
                request.form.get("tax_id"),
                request.form.get("ceo_name"),
                request.form.get("address"),
                request.form.get("biz_type"),
                request.form.get("biz_class"),
                request.form.get("email"),
                request.form.get("phone"),
                is_default,
                profile_id,
            ),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("mgmt_info.list_tax_invoice_profiles"))
    conn.close()
    return render_template("mgmt_info/tax_invoice_profile_form.html", profile=profile)


@bp.route("/tax-invoice-profiles/<int:profile_id>/default", methods=["POST"])
@role_required("admin")
def set_tax_invoice_profile_default(profile_id):
    conn = get_db()
    ensure_tax_invoice_profiles(conn)
    exists = conn.execute(
        "SELECT id FROM tax_invoice_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if not exists:
        conn.close()
        abort(404)
    conn.execute("UPDATE tax_invoice_profiles SET is_default=0")
    conn.execute(
        "UPDATE tax_invoice_profiles SET is_default=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (profile_id,),
    )
    conn.commit()
    conn.close()
    flash("Default  Settings.", "success")
    return redirect(url_for("mgmt_info.list_tax_invoice_profiles"))


@bp.route("/tax-invoice-profiles/<int:profile_id>/delete", methods=["POST"])
@role_required("admin")
def delete_tax_invoice_profile(profile_id):
    conn = get_db()
    ensure_tax_invoice_profiles(conn)
    profile = conn.execute(
        "SELECT * FROM tax_invoice_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if not profile:
        conn.close()
        abort(404)

    count = conn.execute("SELECT COUNT(*) FROM tax_invoice_profiles").fetchone()[0]
    if count <= 1:
        conn.close()
        abort(400, "At least one provider is required.")

    was_default = (
        int(profile[9] if not hasattr(profile, "keys") else profile["is_default"] or 0) == 1
    )
    conn.execute("DELETE FROM tax_invoice_profiles WHERE id=?", (profile_id,))
    if was_default:
        next_row = conn.execute(
            "SELECT id FROM tax_invoice_profiles ORDER BY id LIMIT 1"
        ).fetchone()
        if next_row:
            conn.execute(
                "UPDATE tax_invoice_profiles SET is_default=1 WHERE id=?",
                (next_row[0],),
            )
    conn.commit()
    conn.close()
    return redirect(url_for("mgmt_info.list_tax_invoice_profiles"))
