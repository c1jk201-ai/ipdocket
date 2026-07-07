from flask import Blueprint, abort, current_app, request
from flask_login import current_user

bp = Blueprint("api", __name__)


@bp.before_request
def _guard_matter_access():
    if not getattr(current_user, "is_authenticated", False):
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


from app.blueprints.api import dms_search  # noqa: E402,F401
from app.blueprints.api import legacy_case  # noqa: E402,F401
from app.blueprints.api import routes  # noqa: E402,F401
from app.blueprints.api import routes_audits  # noqa: E402,F401
from app.blueprints.api import routes_finance  # noqa: E402,F401
