from __future__ import annotations

import uuid

from flask import jsonify, request
from flask_login import current_user, login_required

from app.blueprints.case import bp
from app.extensions import db
from app.models.ip_records import Matter, MatterCustomField
from app.services.case.case_audit_service import record_case_audit
from app.services.history.history_merge_service import (
    HISTORY_MERGE_NAMESPACE,
    MAX_HISTORY_MERGE_GROUPS,
    ensure_non_overlapping_history_merge_groups,
    filter_valid_history_merge_groups_for_matter,
    get_valid_history_row_keys_for_matter,
    load_history_merge_groups_for_matter,
    normalize_history_group_title,
    normalize_history_member_keys,
    upsert_history_merge_groups_for_matter,
)
from app.utils.permissions import require_matter_access


def _save_or_delete_groups(case_id: str, groups: list[dict], *, actor_user_id: int | None) -> None:
    if groups:
        upsert_history_merge_groups_for_matter(
            str(case_id),
            groups,
            actor_user_id=actor_user_id,
        )
        return

    row = MatterCustomField.query.filter_by(
        matter_id=str(case_id),
        namespace=HISTORY_MERGE_NAMESPACE,
    ).first()
    if row:
        db.session.delete(row)


@bp.post("/<case_id>/history/merge")
@login_required
def save_history_merge(case_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    payload = request.get_json(silent=True) or {}

    normalized_keys = normalize_history_member_keys(
        payload.get("row_keys") or payload.get("member_keys") or []
    )
    if len(normalized_keys) < 2:
        return (
            jsonify({"ok": False, "message": "  2items  Select."}),
            400,
        )

    valid_key_set = get_valid_history_row_keys_for_matter(str(case_id))
    valid_keys = [k for k in normalized_keys if k in valid_key_set]
    dropped_count = max(0, len(normalized_keys) - len(valid_keys))
    if len(valid_keys) < 2:
        return jsonify({"ok": False, "message": "  target ."}), 400

    group_id = str(payload.get("group_id") or "").strip() or uuid.uuid4().hex
    title = normalize_history_group_title(payload.get("title") or "")

    groups = load_history_merge_groups_for_matter(str(case_id))
    old_group_count = len(groups)
    old_member_count = sum(len(g.get("member_keys") or []) for g in groups)

    selected = set(valid_keys)
    next_groups: list[dict] = []
    for g in groups:
        gid = str(g.get("group_id") or "").strip()
        if not gid or gid == group_id:
            continue
        remaining = [k for k in (g.get("member_keys") or []) if k not in selected]
        if len(remaining) < 2:
            continue
        item = dict(g)
        item["member_keys"] = remaining
        next_groups.append(item)

    next_groups.insert(
        0,
        {
            "group_id": group_id,
            "title": title,
            "member_keys": valid_keys,
            "collapsed": bool(payload.get("collapsed", True)),
        },
    )

    next_groups = filter_valid_history_merge_groups_for_matter(str(case_id), next_groups)
    next_groups = ensure_non_overlapping_history_merge_groups(next_groups)
    if len(next_groups) > MAX_HISTORY_MERGE_GROUPS:
        next_groups = next_groups[:MAX_HISTORY_MERGE_GROUPS]

    actor_user_id = getattr(current_user, "id", None)
    _save_or_delete_groups(str(case_id), next_groups, actor_user_id=actor_user_id)

    new_group_count = len(next_groups)
    new_member_count = sum(len(g.get("member_keys") or []) for g in next_groups)

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="history.merge",
        actor_user_id=actor_user_id,
        old_value={"group_count": old_group_count, "member_count": old_member_count},
        new_value={
            "group_count": new_group_count,
            "member_count": new_member_count,
            "selected_count": len(valid_keys),
            "dropped_count": dropped_count,
        },
    )

    db.session.commit()

    group = next((g for g in next_groups if str(g.get("group_id")) == group_id), None)
    return jsonify(
        {
            "ok": True,
            "group": group,
            "group_count": new_group_count,
            "selected_count": len(valid_keys),
            "dropped_count": dropped_count,
        }
    )


@bp.patch("/<case_id>/history/merge/<group_id>")
@login_required
def update_history_merge(case_id: str, group_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    payload = request.get_json(silent=True) or {}
    groups = load_history_merge_groups_for_matter(str(case_id))

    idx = -1
    for i, g in enumerate(groups):
        if str(g.get("group_id") or "").strip() == str(group_id or "").strip():
            idx = i
            break
    if idx < 0:
        return jsonify({"ok": False, "message": "    none."}), 404

    target = dict(groups[idx])
    old_title = str(target.get("title") or "")
    old_collapsed = bool(target.get("collapsed", True))

    if "title" in payload:
        target["title"] = normalize_history_group_title(payload.get("title") or "")
    if "collapsed" in payload:
        target["collapsed"] = bool(payload.get("collapsed"))

    groups[idx] = target
    groups = filter_valid_history_merge_groups_for_matter(str(case_id), groups)
    groups = ensure_non_overlapping_history_merge_groups(groups)

    actor_user_id = getattr(current_user, "id", None)
    _save_or_delete_groups(str(case_id), groups, actor_user_id=actor_user_id)

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="history.merge.update",
        actor_user_id=actor_user_id,
        old_value={"group_id": group_id, "title": old_title, "collapsed": old_collapsed},
        new_value={
            "group_id": group_id,
            "title": target.get("title") or "",
            "collapsed": bool(target.get("collapsed", True)),
        },
    )

    db.session.commit()

    group = next((g for g in groups if str(g.get("group_id")) == str(group_id)), None)
    return jsonify({"ok": True, "group": group})


@bp.delete("/<case_id>/history/merge/<group_id>")
@login_required
def delete_history_merge(case_id: str, group_id: str):
    Matter.query.get_or_404(case_id)
    require_matter_access(str(case_id), action="edit_case")

    groups = load_history_merge_groups_for_matter(str(case_id))
    old_group_count = len(groups)

    next_groups = [
        g for g in groups if str(g.get("group_id") or "").strip() != str(group_id or "").strip()
    ]
    if len(next_groups) == len(groups):
        return jsonify({"ok": False, "message": "    none."}), 404

    actor_user_id = getattr(current_user, "id", None)
    _save_or_delete_groups(str(case_id), next_groups, actor_user_id=actor_user_id)

    record_case_audit(
        case_id=str(case_id),
        action="USER",
        field_name="history.merge.delete",
        actor_user_id=actor_user_id,
        old_value={"group_id": group_id, "group_count": old_group_count},
        new_value={"group_id": group_id, "group_count": len(next_groups)},
    )

    db.session.commit()
    return jsonify({"ok": True, "group_count": len(next_groups)})
