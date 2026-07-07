from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.extensions import db
from app.models.ip_records import MatterCustomField
from app.utils.policy_sql import policy_text as text

HISTORY_MERGE_NAMESPACE = "history_merge_groups"
MAX_HISTORY_MERGE_GROUPS = 400
MAX_HISTORY_MERGE_MEMBERS = 4000


def _policy_sql(sql: str):
    return text(sql).execution_options(policy_bypass=True)


def normalize_history_row_key(raw: Any) -> str:
    txt = str(raw or "").strip()
    if ":" not in txt:
        return ""
    kind_raw, row_id_raw = txt.split(":", 1)
    kind = (kind_raw or "").strip().lower()
    row_id = (row_id_raw or "").strip()
    if kind not in {"notice", "letter"} or not row_id:
        return ""
    return f"{kind}:{row_id}"


def split_history_row_key(row_key: str) -> tuple[str, str] | None:
    key = normalize_history_row_key(row_key)
    if not key:
        return None
    kind, row_id = key.split(":", 1)
    return kind, row_id


def normalize_history_member_keys(
    raw_keys: Any, *, max_items: int = MAX_HISTORY_MERGE_MEMBERS
) -> list[str]:
    if not isinstance(raw_keys, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_keys:
        key = normalize_history_row_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= max(1, int(max_items or 1)):
            break
    return out


def normalize_history_group_title(raw: Any, *, fallback: str = "") -> str:
    title = " ".join(str(raw or "").split()).strip()
    if not title:
        title = " ".join(str(fallback or "").split()).strip()
    if len(title) > 300:
        title = title[:300].rstrip()
    return title


def normalize_history_merge_groups(
    raw_groups: Any,
    *,
    max_groups: int = MAX_HISTORY_MERGE_GROUPS,
) -> list[dict[str, Any]]:
    if not isinstance(raw_groups, list):
        return []

    out: list[dict[str, Any]] = []
    seen_group_ids: set[str] = set()
    max_groups = max(1, int(max_groups or 1))

    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            continue
        group_id = (
            str(raw_group.get("group_id") or raw_group.get("id") or "").strip() or uuid.uuid4().hex
        )
        if group_id in seen_group_ids:
            continue

        member_keys = normalize_history_member_keys(
            raw_group.get("member_keys") or raw_group.get("rows") or []
        )
        if len(member_keys) < 2:
            continue

        collapsed_raw = raw_group.get("collapsed")
        if collapsed_raw is None:
            collapsed = True
        else:
            collapsed = bool(collapsed_raw)

        out.append(
            {
                "group_id": group_id,
                "title": normalize_history_group_title(
                    raw_group.get("title") or raw_group.get("name") or ""
                ),
                "member_keys": member_keys,
                "collapsed": collapsed,
                "created_at": str(raw_group.get("created_at") or "").strip() or None,
                "created_by": raw_group.get("created_by"),
                "updated_at": str(raw_group.get("updated_at") or "").strip() or None,
                "updated_by": raw_group.get("updated_by"),
            }
        )
        seen_group_ids.add(group_id)
        if len(out) >= max_groups:
            break

    return out


def load_history_merge_groups_for_matter(matter_id: str) -> list[dict[str, Any]]:
    row = MatterCustomField.query.filter_by(
        matter_id=str(matter_id),
        namespace=HISTORY_MERGE_NAMESPACE,
    ).first()
    if not row or not isinstance(row.data, dict):
        return []
    return normalize_history_merge_groups((row.data or {}).get("groups") or [])


def upsert_history_merge_groups_for_matter(
    matter_id: str,
    groups: list[dict[str, Any]],
    *,
    actor_user_id: int | None,
) -> MatterCustomField:
    normalized = normalize_history_merge_groups(groups)

    row = MatterCustomField.query.filter_by(
        matter_id=str(matter_id),
        namespace=HISTORY_MERGE_NAMESPACE,
    ).first()
    if not row:
        row = MatterCustomField(
            matter_id=str(matter_id), namespace=HISTORY_MERGE_NAMESPACE, data={}
        )
        db.session.add(row)

    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    final_groups: list[dict[str, Any]] = []
    for g in normalized:
        item = dict(g)
        if not item.get("created_at"):
            item["created_at"] = now_iso
        if item.get("created_by") is None:
            item["created_by"] = actor_user_id
        item["updated_at"] = now_iso
        item["updated_by"] = actor_user_id
        final_groups.append(item)

    row.data = {
        "groups": final_groups,
        "updated_at": now_iso,
        "updated_by": actor_user_id,
    }
    return row


def get_valid_history_row_keys_for_matter(matter_id: str) -> set[str]:
    notice_ids = {
        str(row[0] or "")
        for row in db.session.execute(
            _policy_sql(
                """
                SELECT oa.oa_id
                FROM office_action oa
                WHERE oa.matter_id = :mid
                  AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
                  AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
                  AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
                """
            ),
            {"mid": str(matter_id)},
        ).all()
    }
    letter_ids = {
        str(row[0] or "")
        for row in db.session.execute(
            _policy_sql(
                """
                SELECT c.comm_id
                FROM communication c
                WHERE c.matter_id = :mid
                  AND (c.comm_type IS NULL OR TRIM(c.comm_type) = '' OR c.comm_type IN ('M', 'R', 'T'))
                """
            ),
            {"mid": str(matter_id)},
        ).all()
    }

    keys: set[str] = set()
    keys.update({f"notice:{row_id}" for row_id in notice_ids if row_id})
    keys.update({f"letter:{row_id}" for row_id in letter_ids if row_id})
    return keys


def filter_valid_history_merge_groups_for_matter(
    matter_id: str,
    groups: list[dict[str, Any]],
    *,
    min_members: int = 2,
) -> list[dict[str, Any]]:
    normalized = normalize_history_merge_groups(groups)
    valid_keys = get_valid_history_row_keys_for_matter(matter_id)
    out: list[dict[str, Any]] = []
    for g in normalized:
        keys = [k for k in g.get("member_keys") or [] if k in valid_keys]
        if len(keys) < max(1, int(min_members or 1)):
            continue
        item = dict(g)
        item["member_keys"] = keys
        out.append(item)
    return out


def ensure_non_overlapping_history_merge_groups(
    groups: list[dict[str, Any]],
    *,
    min_members: int = 2,
) -> list[dict[str, Any]]:
    normalized = normalize_history_merge_groups(groups)
    claimed: set[str] = set()
    out: list[dict[str, Any]] = []
    min_members = max(1, int(min_members or 1))

    for g in normalized:
        keys: list[str] = []
        for key in g.get("member_keys") or []:
            if key in claimed:
                continue
            keys.append(key)
            claimed.add(key)
        if len(keys) < min_members:
            continue
        item = dict(g)
        item["member_keys"] = keys
        out.append(item)
    return out


def is_email_asset_like(
    original_name: str | None,
    mime_type: str | None,
    file_path: str | None,
) -> bool:
    name = (original_name or "").strip().lower()
    mime = (mime_type or "").strip().lower()
    path = (file_path or "").strip().lower()

    if name.endswith(".eml") or name.endswith(".msg"):
        return True
    if mime in {"message/rfc822", "application/vnd.ms-outlook"}:
        return True
    if path.startswith("emails/"):
        return True
    return False
