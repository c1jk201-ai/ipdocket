from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

try:
    from flask_login import login_required
except Exception as exc:  # pragma: no cover
    raise RuntimeError("flask_login is required for productivity routes") from exc

from app.services.productivity.capacity_planner import build_capacity_plan
from app.services.productivity.productivity_service import (
    apply_doc_suggestions,
    doc_suggest_from_upload,
    get_today_todos,
    quick_add_docket,
    quick_add_invoice,
    quick_add_workflow,
    quick_search,
    undo_by_token,
)

bp = Blueprint("productivity", __name__)


@bp.get("/productivity/capacity-planner")
@login_required
def capacity_planner_page():
    return render_template("productivity/capacity_planner.html")


@bp.get("/api/productivity/todos")
@login_required
def api_todos():
    """
    /Matter view  "Task"  Data
    - matter_id  Matter  Filter
    """
    matter_id = (request.args.get("matter_id") or "").strip() or None
    items = get_today_todos(matter_id=matter_id)
    return jsonify({"ok": True, "count": len(items), "items": items})


@bp.get("/api/productivity/capacity-planner")
@login_required
def api_capacity_planner():
    raw_windows = str(request.args.get("windows") or "").strip()
    windows = (14, 28, 56)
    if raw_windows:
        parsed = []
        for token in raw_windows.split(","):
            try:
                days = int(token.strip())
            except Exception:
                continue
            if 1 <= days <= 120:
                parsed.append(days)
        if parsed:
            windows = tuple(sorted(set(parsed)))
    plan = build_capacity_plan(user=current_user, windows=windows)
    return jsonify({"ok": True, **plan})


@bp.get("/api/productivity/quick-search")
@login_required
def api_quick_search():
    q = (request.args.get("q") or "").strip()
    limit_raw = (request.args.get("limit") or "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 20
    except Exception:
        limit = 20
    type_raw = (request.args.get("type") or "").strip()
    types = {t.strip().lower() for t in type_raw.split(",") if t.strip()} if type_raw else None
    out = quick_search(q=q, limit=max(5, min(limit, 50)), type_filter=types)
    return jsonify({"ok": True, "q": q, "items": out})


@bp.post("/api/quickadd/docket")
@login_required
def api_quickadd_docket():
    payload = request.get_json(silent=True) or {}
    try:
        result = quick_add_docket(
            matter_id=(payload.get("matter_id") or "").strip(),
            title=(payload.get("title") or "").strip(),
            due_date=(payload.get("due_date") or "").strip(),
            assignee_id=(payload.get("assignee_id") or "").strip() or None,
            priority=(payload.get("priority") or "").strip() or None,
        )
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **result})


@bp.post("/api/quickadd/workflow")
@login_required
def api_quickadd_workflow():
    payload = request.get_json(silent=True) or {}
    try:
        result = quick_add_workflow(
            matter_id=(payload.get("matter_id") or "").strip(),
            title=(payload.get("title") or "").strip(),
            template_key=(payload.get("template_key") or "").strip() or None,
            legal_due_date=(payload.get("legal_due_date") or "").strip() or None,
            assignee_id=(payload.get("assignee_id") or "").strip() or None,
            manager_assignee_id=(payload.get("manager_assignee_id") or "").strip() or None,
            reviewer_id=(payload.get("reviewer_id") or "").strip() or None,
            priority=(payload.get("priority") or "").strip() or None,
        )
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **result})


@bp.post("/api/quickadd/invoice")
@login_required
def api_quickadd_invoice():
    payload = request.get_json(silent=True) or {}
    try:
        result = quick_add_invoice(matter_id=(payload.get("matter_id") or "").strip())
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **result})


@bp.post("/api/productivity/doc-suggest")
@login_required
def api_doc_suggest():
    """
    Document(PDF/ ) Upload -> Deadline/Task  (Auto Create X)
    """
    matter_id = (request.form.get("matter_id") or "").strip() or None
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "file is required"}), 400
    data = f.read() or b""
    suggestions = doc_suggest_from_upload(
        file_bytes=data,
        filename=(f.filename or "upload"),
        matter_id=matter_id,
    )
    return jsonify({"ok": True, "suggestions": suggestions})


@bp.post("/api/productivity/doc-apply")
@login_required
def api_doc_apply():
    """
      User Confirm  "Apply" ->  Task/Deadline Create
    - Create  undo_token  (1 Undo)
    """
    payload = request.get_json(silent=True) or {}
    matter_id = (payload.get("matter_id") or "").strip() or None
    suggestions = payload.get("suggestions") or []
    if not matter_id:
        return jsonify({"ok": False, "error": "matter_id is required"}), 400
    if not isinstance(suggestions, list) or not suggestions:
        return jsonify({"ok": False, "error": "suggestions must be a non-empty list"}), 400

    try:
        result = apply_doc_suggestions(matter_id=matter_id, suggestions=suggestions)
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    return jsonify({"ok": True, **result})


@bp.post("/api/productivity/undo/<token>")
@login_required
def api_undo(token: str):
    res = undo_by_token(token=token)
    return jsonify({"ok": True, **res})
