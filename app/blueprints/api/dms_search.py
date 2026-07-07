from __future__ import annotations

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.api import bp
from app.services.document_search_service import (
    rebuild_matter_search_index,
    search_document_knowledge,
)
from app.utils.permissions import can_access_matter


@bp.get("/dms/search")
@login_required
def dms_search():
    q = str(request.args.get("q") or "").strip()
    matter_id = str(request.args.get("matter_id") or "").strip() or None
    source_type = str(request.args.get("source_type") or "").strip() or None
    refresh = str(request.args.get("refresh") or "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        limit = int(request.args.get("limit") or 20)
    except Exception:
        limit = 20
    try:
        result = search_document_knowledge(
            query=q,
            user=current_user,
            matter_id=matter_id,
            source_type=source_type,
            limit=limit,
            refresh=refresh,
        )
    except PermissionError:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "q": q, **result})


@bp.post("/dms/reindex/<string:matter_id>")
@login_required
def dms_reindex_matter(matter_id: str):
    if not can_access_matter(current_user, matter_id, action="view"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        counts = rebuild_matter_search_index(
            matter_id,
            indexed_by_id=getattr(current_user, "id", None),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "matter_id": matter_id, "indexed": counts})
