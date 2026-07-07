from flask import Blueprint, abort, current_app, request
from flask_login import current_user

bp = Blueprint("case_work", __name__)


@bp.before_request
def _guard_matter_access():
    if getattr(current_user, "is_anonymous", True):
        return
    view_args = request.view_args or {}
    from app.models.ip_records import Matter
    from app.utils.permissions import can_access_matter, extract_matter_id, resolve_matter_action

    matter_id = extract_matter_id(view_args)
    if not matter_id:
        return
    if not Matter.query.get(str(matter_id)):
        return

    view_fn = None
    try:
        view_fn = current_app.view_functions.get(request.endpoint)
    except Exception:
        view_fn = None
    action = getattr(view_fn, "_matter_action", None) if view_fn else None
    if not action:
        action = resolve_matter_action(request)
    if not can_access_matter(current_user, matter_id, action):
        abort(403, "You do not have permission to access this matter.")


# Quick routes (sticky quick panel / bulk edit / quick create)
from . import quick_routes, status_routes
from .routes import (
    api_refs,
    clients,
    custom_text,
    detail,
    general_create,
    general_edit,
    history_merge,
    history_order,
    integrations,
    invoices,
    list,
    priority,
    progress,
)
from . import views
