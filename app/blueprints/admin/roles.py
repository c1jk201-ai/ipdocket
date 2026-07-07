from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.admin import bp
from app.extensions import db
from app.models.permissions import Permissions
from app.models.role import Role
from app.utils.permissions import role_required


@bp.route("/roles", methods=["GET"])
@login_required
@role_required("admin")
def list_roles():
    roles = Role.query.order_by(Role.id).all()
    all_perms = Permissions.all_permissions()
    return render_template(
        "admin/roles.html", active_page="roles", roles=roles, all_perms=all_perms
    )


@bp.route("/roles/new", methods=["POST"])
@login_required
@role_required("admin")
def create_role():
    name = request.form.get("name")
    desc = request.form.get("description")
    perms = request.form.getlist("permissions")

    if not name:
        flash(" Name Required.", "danger")
        return redirect(url_for("admin.list_roles"))

    role = Role(name=name, description=desc, permissions=perms)
    db.session.add(role)
    db.session.commit()
    flash(" Role Create.", "success")
    return redirect(url_for("admin.list_roles"))


@bp.route("/roles/edit/<int:role_id>", methods=["POST"])
@login_required
@role_required("admin")
def edit_role(role_id):
    role = db.session.get(Role, role_id)
    if not role:
        flash("Role   none.", "danger")
        return redirect(url_for("admin.list_roles"))

    role.name = request.form.get("name", role.name)
    role.description = request.form.get("description", role.description)
    role.permissions = request.form.getlist("permissions")
    db.session.commit()
    flash("Role Edit.", "success")
    return redirect(url_for("admin.list_roles"))


@bp.route("/roles/delete/<int:role_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_role(role_id):
    role = db.session.get(Role, role_id)
    if not role:
        flash("Role   none.", "danger")
        return redirect(url_for("admin.list_roles"))

    db.session.delete(role)
    db.session.commit()
    flash("Role Delete.", "success")
    return redirect(url_for("admin.list_roles"))
