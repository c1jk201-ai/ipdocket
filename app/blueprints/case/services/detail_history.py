from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flask import current_app
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models.ip_records import (
    CommunicationFileAsset,
    EmailMessageMatterLink,
    FileAsset,
    MatterCustomField,
    OfficeActionFileAsset,
)
from app.services.history.history_merge_service import (
    ensure_non_overlapping_history_merge_groups,
    is_email_asset_like,
    load_history_merge_groups_for_matter,
    split_history_row_key,
)
from app.services.matter.matter_auto_status import date_only_str as _date_only_str
from app.utils.error_logging import report_swallowed_exception
from app.utils.policy_sql import policy_text as text


def _rollback_session() -> None:
    try:
        db.session.rollback()
    except SQLAlchemyError as exc:
        report_swallowed_exception(
            exc,
            context="case.detail_history.rollback_session",
            log_key="case.detail_history.rollback_session",
            log_window_seconds=300,
        )


def _is_missing_table_error(err: Exception, table: str) -> bool:
    msg = (str(err) or "").lower()
    token = (table or "").lower()
    if token and token not in msg:
        return False
    return ("undefinedtable" in msg) or ("does not exist" in msg) or ("no such table" in msg)


def _detail_int_cfg(key: str, default: int, *, min_v: int = 1, max_v: int = 5000) -> int:
    raw = current_app.config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(min_v, min(max_v, value))


@dataclass
class HistorySourceRows:
    total_count: int
    communications: list[dict]
    office_actions: list[dict]
    email_id_by_comm_id: dict[str, str]


@dataclass
class HistoryMergeGroupInfo:
    group_id: str
    title: str
    collapsed: bool
    member_keys: list[str]
    member_rows: list[dict]
    primary_date: str
    doc_names: list[str]
    action_summary: str
    attachment_count: int
    attachment_count_resolved: int
    email_attachment_count: int
    owner_name: str
    target: str
    first_email_id: str

    @property
    def member_count(self) -> int:
        return len(self.member_rows)

    @property
    def work_attachment_count_resolved(self) -> int:
        return max(0, self.attachment_count_resolved - self.email_attachment_count)


@dataclass
class HistoryDataset:
    total_count: int
    rows: list[dict]
    merge_group_infos: list[HistoryMergeGroupInfo]
    communications: list[dict]


def _matter_rows_with_params(mid_str: str, sql: str, params: dict | None = None) -> list[dict]:
    merged = {"mid": mid_str}
    merged.update(params or {})
    return [
        dict(r._mapping)
        for r in db.session.execute(text(sql).execution_options(policy_bypass=True), merged).all()
    ]


def _load_history_source_rows(
    *,
    mid_str: str,
    history_limit: int,
    include_details: bool,
    log_context: str,
) -> HistorySourceRows:
    history_total_count = 0
    communications: list[dict] = []

    try:
        history_total_count += (
            db.session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM communication c
                    WHERE c.matter_id = :mid
                      AND (c.comm_type IS NULL OR TRIM(c.comm_type) = '' OR c.comm_type IN ('M', 'R', 'T'))
                    """
                ).execution_options(policy_bypass=True),
                {"mid": mid_str},
            ).scalar()
            or 0
        )
        if include_details:
            communications = _matter_rows_with_params(
                mid_str,
                """
                 SELECT
                   c.comm_id AS id,
                   (SELECT MIN(cf.created_at) FROM communication_file_asset cf WHERE cf.comm_id = c.comm_id) AS uploaded_at,
                   (
                     SELECT MAX(
                       COALESCE(
                         NULLIF(TRIM(cf2.created_at), ''),
                         NULLIF(TRIM(fa_sort.created_at), '')
                       )
                     )
                     FROM communication_file_asset cf2
                     LEFT JOIN file_asset fa_sort ON fa_sort.file_asset_id = cf2.file_asset_id
                     WHERE cf2.comm_id = c.comm_id
                   ) AS sort_uploaded_at,
                   COALESCE(
                     NULLIF(TRIM(c.note), ''),
                     NULLIF(TRIM(SUBSTR(c.body, 1, 200)), ''),
                     c.comm_type
                   ) AS doc_name,
                   c.comm_type AS comm_type,
                   c.received_date AS received_date,
                   c.sent_date AS sent_date,
                   c.due_date AS due_date,
                   c.done_date AS done_date,
                   c.to_text AS target,
                   COALESCE(p.name_display, '') AS owner_name,
                   (SELECT COUNT(*) FROM communication_file_asset cf WHERE cf.comm_id = c.comm_id) AS attach_count,
                   EXISTS (
                     SELECT 1
                     FROM communication_file_asset cf2
                     JOIN file_asset fa2 ON fa2.file_asset_id = cf2.file_asset_id
                     WHERE cf2.comm_id = c.comm_id
                       AND (
                          LOWER(COALESCE(fa2.original_name, '')) LIKE '%.eml'
                          OR LOWER(COALESCE(fa2.original_name, '')) LIKE '%.msg'
                          OR LOWER(COALESCE(fa2.mime_type, '')) IN ('message/rfc822', 'application/vnd.ms-outlook')
                          OR LOWER(COALESCE(fa2.file_path, '')) LIKE 'emails/%'
                        )
                    ) AS has_email_asset
                  FROM communication c
                  LEFT JOIN party p ON p.party_id = c.owner_staff_party_id
                  WHERE c.matter_id = :mid
                    AND (c.comm_type IS NULL OR TRIM(c.comm_type) = '' OR c.comm_type IN ('M', 'R', 'T'))
                  ORDER BY COALESCE(c.sent_date, c.received_date, c.due_date, c.done_date) DESC, c.comm_id DESC
                  LIMIT :limit
                """,
                {"limit": history_limit},
            )
    except Exception as exc:
        _rollback_session()
        if _is_missing_table_error(exc, "communication"):
            current_app.logger.warning(
                "communication table missing; history(letters) will be empty until DB migrations are applied "
                "(try: flask --app run.py db upgrade)."
            )
        else:
            current_app.logger.error("Error in %s communications query: %s", log_context, exc)
        communications = []

    email_id_by_comm_id: dict[str, str] = {}
    try:
        comm_ids = [
            str((row or {}).get("id") or "").strip()
            for row in communications
            if (row or {}).get("id")
        ]
        if include_details and comm_ids:
            rows = (
                EmailMessageMatterLink.query.with_entities(
                    EmailMessageMatterLink.comm_id, EmailMessageMatterLink.email_id
                )
                .filter(EmailMessageMatterLink.comm_id.in_(comm_ids))
                .all()
            )
            for comm_id, email_id in rows:
                key = str(comm_id or "").strip()
                value = str(email_id or "").strip()
                if key and value and key not in email_id_by_comm_id:
                    email_id_by_comm_id[key] = value
    except Exception as exc:
        _rollback_session()
        if _is_missing_table_error(exc, "email_message_matter_link"):
            current_app.logger.warning(
                "email_message_matter_link table missing; communication↔mail cross-links disabled."
            )
        else:
            current_app.logger.error("Error in %s email links: %s", log_context, exc)

    office_actions: list[dict] = []
    try:
        history_total_count += (
            db.session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM office_action oa
                    WHERE oa.matter_id = :mid
                      AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
                      AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
                      AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
                    """
                ).execution_options(policy_bypass=True),
                {"mid": mid_str},
            ).scalar()
            or 0
        )
        if include_details:
            office_actions = _matter_rows_with_params(
                mid_str,
                """
                SELECT
                  oa.oa_id AS id,
                  oa.doc_name AS doc_name,
                  oa.received_date AS received_date,
                  oa.notified_date AS notified_date,
                  oa.due_date AS due_date,
                  oa.extended_due_date AS extended_due_date,
                  oa.done_date AS done_date,
                  NULL AS target,
                  '' AS owner_name,
                  (SELECT COUNT(*) FROM office_action_file_asset f WHERE f.oa_id = oa.oa_id) AS attach_count,
                  (
                    SELECT MAX(
                      COALESCE(
                        NULLIF(TRIM(f2.created_at), ''),
                        NULLIF(TRIM(fa_sort.created_at), '')
                      )
                    )
                    FROM office_action_file_asset f2
                    LEFT JOIN file_asset fa_sort ON fa_sort.file_asset_id = f2.file_asset_id
                    WHERE f2.oa_id = oa.oa_id
                  ) AS sort_uploaded_at
                FROM office_action oa
                WHERE oa.matter_id = :mid
                  AND (oa.raw_id IS NULL OR oa.raw_id NOT LIKE 'MIGRATED_TO_COMM:%')
                  AND COALESCE(oa.doc_name, '') NOT LIKE 'from%'
                  AND COALESCE(oa.doc_name, '') NOT LIKE ' to%'
                ORDER BY COALESCE(oa.due_date, oa.notified_date, oa.received_date) DESC, oa.oa_id DESC
                LIMIT :limit
                """,
                {"limit": history_limit},
            )
    except Exception as exc:
        _rollback_session()
        if _is_missing_table_error(exc, "office_action"):
            current_app.logger.warning(
                "office_action table missing; history(notices) will be empty until DB migrations are applied "
                "(try: flask --app run.py db upgrade)."
            )
        else:
            current_app.logger.error("Error in %s office_actions query: %s", log_context, exc)
        office_actions = []

    return HistorySourceRows(
        total_count=history_total_count,
        communications=communications,
        office_actions=office_actions,
        email_id_by_comm_id=email_id_by_comm_id,
    )


def _history_comm_action(
    comm_type: str | None, received_date: str | None, sent_date: str | None
) -> str:
    normalized = (comm_type or "").strip().upper()
    if normalized and normalized not in ("M", "R", "T"):
        return "Notes"
    if normalized == "R":
        return ""
    if normalized == "T":
        return "(Currency)"
    if (sent_date or "").strip():
        return "Send"
    if (received_date or "").strip():
        return "Upload"
    return ""


def _build_history_rows(source_rows: HistorySourceRows) -> list[dict]:
    history_rows: list[dict] = []

    for row in source_rows.office_actions:
        oa_id = str(row.get("id") or "").strip()
        history_rows.append(
            {
                "kind": "notice",
                "id": oa_id,
                "row_key": f"notice:{oa_id}" if oa_id else "",
                "doc_name": row.get("doc_name") or "",
                "action": "Upload",
                "received_date": row.get("received_date") or "",
                "notified_date": row.get("notified_date") or "",
                "due_date": row.get("due_date") or "",
                "extended_due_date": row.get("extended_due_date") or "",
                "done_date": row.get("done_date") or "",
                "owner_name": row.get("owner_name") or "",
                "target": row.get("target") or "",
                "attach_count": row.get("attach_count") or 0,
                "sort_uploaded_at": row.get("sort_uploaded_at") or "",
            }
        )

    for row in source_rows.communications:
        uploaded_at = (row.get("uploaded_at") or "").strip()
        received_date = (row.get("received_date") or "").strip()
        sent_date = (row.get("sent_date") or "").strip()
        comm_type = "M" if row.get("has_email_asset") else row.get("comm_type")
        comm_id = str(row.get("id") or "").strip()
        history_rows.append(
            {
                "kind": "letter",
                "comm_type": comm_type,
                "id": comm_id,
                "row_key": f"letter:{comm_id}" if comm_id else "",
                "doc_name": row.get("doc_name") or "",
                "action": _history_comm_action(comm_type, received_date, sent_date),
                "received_date": uploaded_at or received_date,
                "notified_date": sent_date or received_date,
                "due_date": row.get("due_date") or "",
                "extended_due_date": "",
                "done_date": row.get("done_date") or "",
                "owner_name": row.get("owner_name") or "",
                "target": row.get("target") or "",
                "attach_count": row.get("attach_count") or 0,
                "sort_uploaded_at": row.get("sort_uploaded_at") or "",
                "email_id": source_rows.email_id_by_comm_id.get(comm_id),
            }
        )

    return history_rows


def _parse_history_dt(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=None)
    date_only = _date_only_str(normalized)
    if not date_only:
        return None
    try:
        return datetime.fromisoformat(date_only).replace(tzinfo=None)
    except ValueError:
        return None


def _history_sort_key(row: dict) -> tuple[datetime, datetime]:
    kind = str((row or {}).get("kind") or "").strip().lower()
    if kind == "notice":
        primary_dt = (
            _parse_history_dt(row.get("notified_date"))
            or _parse_history_dt(row.get("received_date"))
            or _parse_history_dt(row.get("due_date"))
            or _parse_history_dt(row.get("done_date"))
        )
    else:
        primary_dt = (
            _parse_history_dt(row.get("received_date"))
            or _parse_history_dt(row.get("notified_date"))
            or _parse_history_dt(row.get("due_date"))
            or _parse_history_dt(row.get("done_date"))
        )
    primary = primary_dt or datetime.min
    secondary = datetime.min
    uploaded = _parse_history_dt(row.get("sort_uploaded_at") or row.get("uploaded_at"))
    if uploaded and primary != datetime.min and uploaded.date() == primary.date():
        secondary = uploaded
    return (primary, secondary)


def _load_saved_history_order(mid_str: str, *, log_context: str) -> list[str]:
    saved_history_order: list[str] = []
    try:
        order_row = MatterCustomField.query.filter_by(
            matter_id=mid_str, namespace="history_order"
        ).first()
        if order_row and isinstance(order_row.data, dict):
            saved_history_order = [
                str(item or "").strip() for item in (order_row.data.get("order") or [])
            ]
            saved_history_order = [item for item in saved_history_order if item]
    except Exception as exc:
        _rollback_session()
        current_app.logger.error("Error loading saved history order for %s: %s", log_context, exc)
        saved_history_order = []
    return saved_history_order


def _apply_saved_history_order(
    history_rows: list[dict], saved_history_order: list[str]
) -> list[dict]:
    if not saved_history_order or not history_rows:
        return history_rows

    current_row_keys = [
        str((row or {}).get("row_key") or "").strip()
        for row in history_rows
        if str((row or {}).get("row_key") or "").strip()
    ]
    current_key_set = set(current_row_keys)
    order_index: dict[str, int] = {}
    for item in saved_history_order:
        if item in current_key_set and item not in order_index:
            order_index[item] = len(order_index)

    if not order_index or len(order_index) != len(current_key_set):
        return history_rows

    ranked_rows: list[tuple[int, int, int, dict]] = []
    for idx, row in enumerate(history_rows):
        row_key = str((row or {}).get("row_key") or "").strip()
        if row_key and row_key in order_index:
            ranked_rows.append((0, order_index[row_key], idx, row))
        else:
            ranked_rows.append((1, idx, idx, row))
    ranked_rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked_rows]


def _history_row_primary_date_text(row: dict) -> str:
    kind = str((row or {}).get("kind") or "").strip().lower()
    if kind == "notice":
        return (
            str(row.get("notified_date") or "").strip()
            or str(row.get("received_date") or "").strip()
            or str(row.get("due_date") or "").strip()
            or str(row.get("done_date") or "").strip()
        )
    return (
        str(row.get("received_date") or "").strip()
        or str(row.get("notified_date") or "").strip()
        or str(row.get("due_date") or "").strip()
        or str(row.get("done_date") or "").strip()
    )


def _load_history_merge_group_attachments(
    valid_groups: list[dict], *, log_context: str
) -> dict[str, list[tuple[str, str, str, str]]]:
    attachments_by_row_key: dict[str, list[tuple[str, str, str, str]]] = {}
    if not valid_groups:
        return attachments_by_row_key

    comm_ids: set[str] = set()
    oa_ids: set[str] = set()
    for group in valid_groups:
        for key in group.get("member_keys") or []:
            parsed = split_history_row_key(str(key or ""))
            if not parsed:
                continue
            kind, row_id = parsed
            if kind == "letter":
                comm_ids.add(row_id)
            elif kind == "notice":
                oa_ids.add(row_id)

    if comm_ids:
        try:
            comm_attach_rows = (
                db.session.query(
                    CommunicationFileAsset.comm_id,
                    FileAsset.file_asset_id,
                    FileAsset.original_name,
                    FileAsset.mime_type,
                    FileAsset.file_path,
                )
                .join(
                    FileAsset,
                    FileAsset.file_asset_id == CommunicationFileAsset.file_asset_id,
                )
                .filter(CommunicationFileAsset.comm_id.in_(sorted(comm_ids)))
                .all()
            )
            for comm_id, file_asset_id, original_name, mime_type, file_path in comm_attach_rows:
                row_key = f"letter:{str(comm_id or '').strip()}"
                if not row_key:
                    continue
                attachments_by_row_key.setdefault(row_key, []).append(
                    (
                        str(file_asset_id or "").strip(),
                        str(original_name or ""),
                        str(mime_type or ""),
                        str(file_path or ""),
                    )
                )
        except Exception as exc:
            _rollback_session()
            current_app.logger.error(
                "Error loading merge communication attachments for %s: %s", log_context, exc
            )

    if oa_ids:
        try:
            oa_attach_rows = (
                db.session.query(
                    OfficeActionFileAsset.oa_id,
                    FileAsset.file_asset_id,
                    FileAsset.original_name,
                    FileAsset.mime_type,
                    FileAsset.file_path,
                )
                .join(
                    FileAsset,
                    FileAsset.file_asset_id == OfficeActionFileAsset.file_asset_id,
                )
                .filter(OfficeActionFileAsset.oa_id.in_(sorted(oa_ids)))
                .all()
            )
            for oa_id, file_asset_id, original_name, mime_type, file_path in oa_attach_rows:
                row_key = f"notice:{str(oa_id or '').strip()}"
                if not row_key:
                    continue
                attachments_by_row_key.setdefault(row_key, []).append(
                    (
                        str(file_asset_id or "").strip(),
                        str(original_name or ""),
                        str(mime_type or ""),
                        str(file_path or ""),
                    )
                )
        except Exception as exc:
            _rollback_session()
            current_app.logger.error(
                "Error loading merge office-action attachments for %s: %s", log_context, exc
            )

    return attachments_by_row_key


def _build_history_merge_group_infos(
    *, mid_str: str, history_rows: list[dict], log_context: str
) -> list[HistoryMergeGroupInfo]:
    if not history_rows:
        return []

    try:
        saved_history_merge_groups = load_history_merge_groups_for_matter(mid_str)
    except Exception as exc:
        _rollback_session()
        current_app.logger.error("Error loading history merge groups for %s: %s", log_context, exc)
        saved_history_merge_groups = []

    if not saved_history_merge_groups:
        return []

    row_by_key: dict[str, dict] = {}
    row_index: dict[str, int] = {}
    for idx, row in enumerate(history_rows):
        row_key = str((row or {}).get("row_key") or "").strip()
        if not row_key:
            continue
        row_by_key[row_key] = row
        row_index[row_key] = idx

    normalized_groups = ensure_non_overlapping_history_merge_groups(saved_history_merge_groups)
    valid_groups: list[dict] = []
    claimed_keys: set[str] = set()
    for group in normalized_groups:
        keys: list[str] = []
        for key in group.get("member_keys") or []:
            row_key = str(key or "").strip()
            if not row_key or row_key in claimed_keys or row_key not in row_by_key:
                continue
            claimed_keys.add(row_key)
            keys.append(row_key)
        if len(keys) < 2:
            continue
        item = dict(group)
        item["member_keys"] = keys
        valid_groups.append(item)
        for key in keys:
            row_by_key[key]["merge_group_id"] = str(group.get("group_id") or "")

    if not valid_groups:
        return []

    attachments_by_row_key = _load_history_merge_group_attachments(
        valid_groups, log_context=log_context
    )

    valid_groups.sort(
        key=lambda group: min(
            row_index.get(str(key or "").strip(), len(history_rows) + 1)
            for key in (group.get("member_keys") or [])
        )
    )

    group_infos: list[HistoryMergeGroupInfo] = []
    for idx, group in enumerate(valid_groups, start=1):
        member_keys = [
            str(key or "").strip()
            for key in (group.get("member_keys") or [])
            if str(key or "").strip()
        ]
        member_rows = [row_by_key[key] for key in member_keys if key in row_by_key]
        member_rows.sort(
            key=lambda row: row_index.get(str((row or {}).get("row_key") or ""), len(history_rows))
        )
        if len(member_rows) < 2:
            continue

        latest_row = member_rows[0]
        default_title = str((latest_row or {}).get("doc_name") or "").strip() or f"  {idx}"
        group_title = str(group.get("title") or "").strip() or default_title

        doc_names: list[str] = []
        seen_doc_names: set[str] = set()
        for row in member_rows:
            doc_name = str((row or {}).get("doc_name") or "").strip()
            if not doc_name or doc_name in seen_doc_names:
                continue
            seen_doc_names.add(doc_name)
            doc_names.append(doc_name)
            if len(doc_names) >= 5:
                break

        actions = sorted(
            {
                str((row or {}).get("action") or "").strip()
                for row in member_rows
                if str((row or {}).get("action") or "").strip()
            }
        )
        action_summary = ", ".join(actions[:3]) if actions else ""

        unique_file_ids: set[str] = set()
        email_file_ids: set[str] = set()
        for key in member_keys:
            for file_asset_id, original_name, mime_type, file_path in attachments_by_row_key.get(
                key, []
            ):
                fid = str(file_asset_id or "").strip()
                if not fid:
                    continue
                unique_file_ids.add(fid)
                if is_email_asset_like(
                    original_name=str(original_name or ""),
                    mime_type=str(mime_type or ""),
                    file_path=str(file_path or ""),
                ):
                    email_file_ids.add(fid)

        attachment_count = len(unique_file_ids)
        attachment_count_resolved = attachment_count
        if attachment_count_resolved <= 0:
            attachment_count_resolved = sum(
                int((row or {}).get("attach_count") or 0) for row in member_rows
            )

        owner_name = ""
        target = ""
        for row in member_rows:
            if not owner_name:
                owner_name = str((row or {}).get("owner_name") or "").strip()
            if not target:
                target = str((row or {}).get("target") or "").strip()
            if owner_name and target:
                break

        first_email_id = ""
        for row in member_rows:
            email_id = str((row or {}).get("email_id") or "").strip()
            if email_id:
                first_email_id = email_id
                break

        group_infos.append(
            HistoryMergeGroupInfo(
                group_id=str(group.get("group_id") or "").strip(),
                title=group_title,
                collapsed=bool(group.get("collapsed", True)),
                member_keys=member_keys,
                member_rows=member_rows,
                primary_date=_history_row_primary_date_text(latest_row),
                doc_names=doc_names,
                action_summary=action_summary,
                attachment_count=attachment_count,
                attachment_count_resolved=attachment_count_resolved,
                email_attachment_count=len(email_file_ids),
                owner_name=owner_name,
                target=target,
                first_email_id=first_email_id,
            )
        )

    return group_infos


def _format_history_panel_merge_groups(group_infos: list[HistoryMergeGroupInfo]) -> list[dict]:
    return [
        {
            "group_id": group_info.group_id,
            "title": group_info.title,
            "doc_names": group_info.doc_names,
            "action_summary": group_info.action_summary,
            "member_keys": group_info.member_keys,
            "member_count": group_info.member_count,
            "member_rows": group_info.member_rows,
            "primary_date": group_info.primary_date,
            "attachment_count": group_info.attachment_count,
            "email_attachment_count": group_info.email_attachment_count,
        }
        for group_info in group_infos
    ]


def format_history_section_merge_groups(group_infos: list[HistoryMergeGroupInfo]) -> list[dict]:
    return [
        {
            "group_id": group_info.group_id,
            "title": group_info.title,
            "collapsed": group_info.collapsed,
            "member_keys": group_info.member_keys,
            "member_count": group_info.member_count,
            "latest_date": group_info.primary_date,
            "doc_names": group_info.doc_names,
            "action_summary": group_info.action_summary,
            "owner_name": group_info.owner_name,
            "target": group_info.target,
            "attach_total_count": group_info.attachment_count_resolved,
            "attach_email_count": group_info.email_attachment_count,
            "attach_work_count": group_info.work_attachment_count_resolved,
            "first_email_id": group_info.first_email_id,
        }
        for group_info in group_infos
    ]


def build_history_dataset(
    *,
    mid_str: str,
    history_limit: int,
    include_details: bool,
    log_context: str,
) -> HistoryDataset:
    source_rows = _load_history_source_rows(
        mid_str=mid_str,
        history_limit=history_limit,
        include_details=include_details,
        log_context=log_context,
    )

    history_rows: list[dict] = []
    merge_group_infos: list[HistoryMergeGroupInfo] = []
    if include_details:
        history_rows = _build_history_rows(source_rows)
        history_rows.sort(key=_history_sort_key, reverse=True)
        history_rows = _apply_saved_history_order(
            history_rows,
            _load_saved_history_order(mid_str, log_context=log_context),
        )
        if history_limit and history_rows:
            history_rows = history_rows[:history_limit]
        merge_group_infos = _build_history_merge_group_infos(
            mid_str=mid_str,
            history_rows=history_rows,
            log_context=log_context,
        )

    total_count = source_rows.total_count or len(history_rows)
    return HistoryDataset(
        total_count=total_count,
        rows=history_rows,
        merge_group_infos=merge_group_infos,
        communications=source_rows.communications,
    )


def build_history_panel_context(ctx: dict) -> dict:
    mid_str = ctx["_mid_str"]
    history_limit = _detail_int_cfg("CASE_DETAIL_HISTORY_LIMIT", 200, min_v=20, max_v=2000)
    history_dataset = build_history_dataset(
        mid_str=mid_str,
        history_limit=history_limit,
        include_details=True,
        log_context="build_history_panel_context",
    )
    history_rows = history_dataset.rows
    history_merge_groups = _format_history_panel_merge_groups(history_dataset.merge_group_infos)
    history_total_count = history_dataset.total_count or len(history_rows)

    return {
        "history_rows": history_rows,
        "history_merge_groups": history_merge_groups,
        "history_total_count": history_total_count,
        "history_truncated": bool(history_total_count and history_total_count > len(history_rows)),
        "_history_count": history_total_count,
    }


def load_notice_send_prompt_communications(*, mid_str: str, limit: int) -> list[dict]:
    try:
        from app.services.deadlines.notice_send_semi_close import (
            load_notice_send_communications_for_matter,
        )

        return load_notice_send_communications_for_matter(
            matter_id=mid_str,
            limit=max(1, int(limit or 1)),
        )
    except SQLAlchemyError:
        _rollback_session()
        return []
