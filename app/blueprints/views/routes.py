from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import case

try:
    from flask_login import current_user, login_required
except Exception as exc:  # pragma: no cover
    raise RuntimeError("flask_login is required for views routes") from exc

from app.blueprints.views import bp
from app.extensions import db
from app.models.user_saved_view import UserSavedView
from app.services.productivity.view_service import serialize_view, system_saved_views_for_user


def _require_user_id() -> int:
    if current_user is None or not getattr(current_user, "is_authenticated", False):
        raise ValueError("auth required")
    return int(getattr(current_user, "id"))


def _current_team_key() -> str | None:
    key = (getattr(current_user, "department", None) or "").strip()
    return key or None


def _is_admin() -> bool:
    return (getattr(current_user, "role", "") or "").strip().lower() == "admin"


def _list_scope(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in {"private", "team", "all"}:
        return s
    return "all"


def _write_scope(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if s in {"team", "dept", "department"}:
        return "team"
    return "private"


def _can_read_view(row: UserSavedView, *, user_id: int, team_key: str | None) -> bool:
    if not row:
        return False
    if (row.scope or "").strip().lower() == "team":
        return bool(team_key) and (row.scope_key == team_key)
    return int(row.user_id) == int(user_id)


def _can_manage_view(row: UserSavedView, *, user_id: int) -> bool:
    if not row:
        return False
    if int(row.user_id) == int(user_id):
        return True
    return _is_admin()


def _get_view_for_access(
    view_id: str, *, user_id: int, team_key: str | None
) -> UserSavedView | None:
    if not view_id:
        return None
    row = UserSavedView.query.filter_by(id=view_id).first()
    if not row:
        return None
    return row if _can_read_view(row, user_id=user_id, team_key=team_key) else None


@bp.get("/api/views")
@login_required
def api_views_list():
    module = (request.args.get("module") or "").strip()
    if not module:
        return jsonify({"ok": False, "error": "module is required"}), 400
    user_id = _require_user_id()

    scope = _list_scope(request.args.get("scope"))
    team_key = _current_team_key()

    q = UserSavedView.query.filter_by(module=module)
    clauses = []
    if scope in {"private", "all"}:
        # Treat anything other than explicit "team" as private; but always per-user.
        clauses.append(db.and_(UserSavedView.user_id == user_id, UserSavedView.scope != "team"))
    if scope in {"team", "all"} and team_key:
        clauses.append(db.and_(UserSavedView.scope == "team", UserSavedView.scope_key == team_key))

    if not clauses:
        return jsonify({"ok": True, "items": []})

    q = q.filter(db.or_(*clauses)) if len(clauses) > 1 else q.filter(clauses[0])
    scope_order = case((UserSavedView.scope == "team", 1), else_=0)
    rows = q.order_by(
        scope_order.asc(), UserSavedView.is_default.desc(), UserSavedView.updated_at.desc()
    ).all()
    items = [serialize_view(v) for v in rows]
    if scope == "all":
        has_personal_default = any(
            bool(v.is_default) and (v.scope or "").strip().lower() != "team" for v in rows
        )
        items.extend(
            system_saved_views_for_user(
                module,
                current_user,
                allow_system_default=not has_personal_default,
            )
        )
    return jsonify({"ok": True, "items": items})


@bp.post("/api/views")
@login_required
def api_views_create():
    payload = request.get_json(silent=True) or {}
    module = (payload.get("module") or "").strip()
    name = (payload.get("name") or "").strip()
    scope = _write_scope(payload.get("scope"))
    view_payload = payload.get("payload") or {}
    set_default = bool(payload.get("set_default"))

    if not module:
        return jsonify({"ok": False, "error": "module is required"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    if not isinstance(view_payload, dict):
        return jsonify({"ok": False, "error": "payload must be an object"}), 400

    user_id = _require_user_id()
    scope_key = None
    if scope == "team":
        scope_key = _current_team_key()
        if not scope_key:
            return jsonify({"ok": False, "error": "department is required for team views"}), 400
        # Keep defaults strictly per-user to avoid surprising auto-apply behavior.
        set_default = False

    if set_default and scope != "team":
        UserSavedView.query.filter_by(user_id=user_id, module=module, is_default=True).update(
            {"is_default": False}
        )

    row = UserSavedView(
        user_id=user_id,
        module=module,
        name=name,
        scope=scope,
        scope_key=scope_key,
        payload_json=view_payload,
        is_default=set_default,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"ok": True, "item": serialize_view(row)})


@bp.put("/api/views/<view_id>")
@login_required
def api_views_update(view_id: str):
    payload = request.get_json(silent=True) or {}
    user_id = _require_user_id()
    team_key = _current_team_key()
    row = _get_view_for_access(view_id, user_id=user_id, team_key=team_key)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not _can_manage_view(row, user_id=user_id):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    name = payload.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            return jsonify({"ok": False, "error": "name is required"}), 400
        row.name = name

    if "payload" in payload:
        view_payload = payload.get("payload") or {}
        if not isinstance(view_payload, dict):
            return jsonify({"ok": False, "error": "payload must be an object"}), 400
        row.payload_json = view_payload

    if "scope" in payload:
        requested = _write_scope(payload.get("scope"))
        if requested != _write_scope(row.scope):
            return jsonify({"ok": False, "error": "scope cannot be changed"}), 400

    db.session.commit()
    return jsonify({"ok": True, "item": serialize_view(row)})


@bp.delete("/api/views/<view_id>")
@login_required
def api_views_delete(view_id: str):
    user_id = _require_user_id()
    team_key = _current_team_key()
    row = _get_view_for_access(view_id, user_id=user_id, team_key=team_key)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not _can_manage_view(row, user_id=user_id):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/api/views/<view_id>/set-default")
@login_required
def api_views_set_default(view_id: str):
    user_id = _require_user_id()
    team_key = _current_team_key()
    row = _get_view_for_access(view_id, user_id=user_id, team_key=team_key)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not _can_manage_view(row, user_id=user_id):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    if _write_scope(row.scope) == "team":
        return jsonify({"ok": False, "error": "team views cannot be set as default"}), 400

    UserSavedView.query.filter_by(user_id=user_id, module=row.module, is_default=True).filter(
        UserSavedView.scope != "team"
    ).update({"is_default": False})
    row.is_default = True
    db.session.commit()
    return jsonify({"ok": True, "item": serialize_view(row)})
