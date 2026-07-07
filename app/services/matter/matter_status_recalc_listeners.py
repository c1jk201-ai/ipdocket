from __future__ import annotations

import hashlib

from flask import current_app, has_app_context
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session as SASession

from app.extensions import db
from app.models.assets import FileAsset
from app.models.communication import Communication, CommunicationFileAsset, OfficeAction
from app.models.docket import DocketItem
from app.models.matter import Matter, MatterCustomField, MatterEvent
from app.services.matter.matter_status_recalc_queue import (
    drain_matter_status_recalc_queue,
    enqueue_matter_status_recalc,
)
from app.services.ops.background import BackgroundService
from app.utils.error_logging import report_swallowed_exception

_MODULE_INITIALIZED = False
_PENDING_COMM_KEY = "matter_status_recalc_pending_comm_ids"
_PENDING_FILE_ASSET_KEY = "matter_status_recalc_pending_file_asset_ids"
_PENDING_KEY = "matter_status_recalc_pending_ids"
_QUEUE_KEY = "matter_status_recalc_queue_ids"
_IN_HANDLER_KEY = "matter_status_recalc_queue_in_handler"
_STATUS_DOCKET_NAME_REF_PREFIXES = ("MGMT:STATUS_RED:", "MGMT:NOTICE_SEND_3D:")
_STATUS_NAMESPACE_SET = {
    "domestic_patent",
    "domestic_design",
    "domestic_trademark",
    "incoming_patent",
    "incoming_design",
    "incoming_trademark",
    "outgoing_patent",
    "outgoing_design",
    "outgoing_trademark",
    "pct",
    "litigation",
}


def _attr_history_values(obj, attr_name: str) -> set[str]:
    values: set[str] = set()
    try:
        history = sa_inspect(obj).attrs[attr_name].history
    except Exception:
        return values

    for bucket in (history.added, history.deleted, history.unchanged):
        for value in bucket or ():
            text = str(value or "").strip()
            if text:
                values.add(text)
    return values


def _matter_id_candidates(obj) -> set[str]:
    candidates = _attr_history_values(obj, "matter_id")
    current = str(getattr(obj, "matter_id", "") or "").strip()
    if current:
        candidates.add(current)
    return candidates


def _attr_changed(obj, *attr_names: str) -> bool:
    try:
        state = sa_inspect(obj)
    except Exception:
        return False
    for attr_name in attr_names:
        try:
            if state.attrs[attr_name].history.has_changes():
                return True
        except Exception:
            continue
    return False


def _is_status_namespace(obj: MatterCustomField) -> bool:
    namespaces = _attr_history_values(obj, "namespace")
    current = str(getattr(obj, "namespace", "") or "").strip()
    if current:
        namespaces.add(current)
    return any(ns in _STATUS_NAMESPACE_SET for ns in namespaces)


def _is_status_red_name_ref(value: str | None) -> bool:
    ref = str(value or "").strip().upper()
    return any(ref.startswith(prefix) for prefix in _STATUS_DOCKET_NAME_REF_PREFIXES)


def _is_status_red_docket(obj: DocketItem) -> bool:
    refs = _attr_history_values(obj, "name_ref")
    current = str(getattr(obj, "name_ref", "") or "").strip()
    if current:
        refs.add(current)
    return any(_is_status_red_name_ref(ref) for ref in refs)


def _is_response_communication(obj: Communication) -> bool:
    kinds = _attr_history_values(obj, "comm_type")
    current = str(getattr(obj, "comm_type", "") or "").strip()
    if current:
        kinds.add(current)
    return any(kind.upper() == "R" for kind in kinds)


def _comm_id_candidates(obj: CommunicationFileAsset) -> set[str]:
    candidates = _attr_history_values(obj, "comm_id")
    current = str(getattr(obj, "comm_id", "") or "").strip()
    if current:
        candidates.add(current)
    return candidates


def _file_asset_id_candidates(obj) -> set[str]:
    candidates = _attr_history_values(obj, "file_asset_id")
    current = str(getattr(obj, "file_asset_id", "") or "").strip()
    if current:
        candidates.add(current)
    return candidates


def _queue_pending_ids(session: SASession, key: str, ids: set[str]) -> None:
    if not ids:
        return
    pending = session.info.setdefault(key, set())
    if not isinstance(pending, set):
        pending = set()
        session.info[key] = pending
    pending.update(ids)


def _resolve_comm_ids_to_matter_ids(session: SASession, comm_ids: set[str]) -> set[str]:
    ids = {str(comm_id).strip() for comm_id in comm_ids if str(comm_id or "").strip()}
    if not ids:
        return set()

    resolved: set[str] = set()
    rows = (
        session.query(Communication.matter_id)
        .filter(Communication.comm_id.in_(sorted(ids)))
        .filter(Communication.comm_type == "R")
        .all()
    )
    for row in rows:
        mid = str(row[0] or "").strip()
        if mid:
            resolved.add(mid)
    return resolved


def _resolve_file_asset_ids_to_matter_ids(session: SASession, file_asset_ids: set[str]) -> set[str]:
    ids = {
        str(file_asset_id).strip()
        for file_asset_id in file_asset_ids
        if str(file_asset_id or "").strip()
    }
    if not ids:
        return set()

    rows = (
        session.query(Communication.matter_id)
        .join(CommunicationFileAsset, CommunicationFileAsset.comm_id == Communication.comm_id)
        .filter(CommunicationFileAsset.file_asset_id.in_(sorted(ids)))
        .filter(Communication.comm_type == "R")
        .all()
    )
    resolved: set[str] = set()
    for row in rows:
        mid = str(row[0] or "").strip()
        if mid:
            resolved.add(mid)
    return resolved


def _collect_status_recalc_candidates(session: SASession, _flush_context, _instances=None) -> None:
    if session.info.get(_IN_HANDLER_KEY):
        return

    matter_ids: set[str] = set()
    pending_comm_ids: set[str] = set()
    pending_file_asset_ids: set[str] = set()

    def _add_candidates(obj) -> None:
        for mid in _matter_id_candidates(obj):
            matter_ids.add(mid)

    for obj in session.new:
        if isinstance(obj, (MatterEvent, OfficeAction)):
            _add_candidates(obj)
        elif isinstance(obj, MatterCustomField) and _is_status_namespace(obj):
            _add_candidates(obj)
        elif isinstance(obj, DocketItem) and _is_status_red_docket(obj):
            _add_candidates(obj)
        elif isinstance(obj, Communication) and _is_response_communication(obj):
            _add_candidates(obj)
        elif isinstance(obj, CommunicationFileAsset):
            pending_comm_ids.update(_comm_id_candidates(obj))
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))
        elif isinstance(obj, FileAsset):
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))
        elif isinstance(obj, Matter):
            _add_candidates(obj)

    for obj in session.dirty:
        if isinstance(obj, MatterEvent) and _attr_changed(
            obj, "matter_id", "event_key", "event_at", "raw_text", "source_column"
        ):
            _add_candidates(obj)
        elif (
            isinstance(obj, MatterCustomField)
            and _is_status_namespace(obj)
            and _attr_changed(obj, "matter_id", "namespace", "data", "updated_at")
        ):
            _add_candidates(obj)
        elif isinstance(obj, OfficeAction) and _attr_changed(
            obj,
            "matter_id",
            "doc_name",
            "raw_id",
            "received_date",
            "notified_date",
            "due_date",
            "extended_due_date",
            "done_date",
        ):
            _add_candidates(obj)
        elif (
            isinstance(obj, DocketItem)
            and _is_status_red_docket(obj)
            and _attr_changed(
                obj,
                "matter_id",
                "name_ref",
                "name_free",
                "due_date",
                "done_date",
                "is_deleted",
            )
        ):
            _add_candidates(obj)
        elif (
            isinstance(obj, Communication)
            and _is_response_communication(obj)
            and _attr_changed(
                obj,
                "matter_id",
                "comm_type",
                "sent_date",
                "received_date",
                "done_date",
                "note",
            )
        ):
            _add_candidates(obj)
        elif isinstance(obj, CommunicationFileAsset) and _attr_changed(
            obj,
            "comm_id",
            "file_asset_id",
            "is_deleted",
        ):
            pending_comm_ids.update(_comm_id_candidates(obj))
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))
        elif isinstance(obj, FileAsset) and _attr_changed(
            obj,
            "file_path",
            "original_name",
            "mime_type",
            "is_deleted",
        ):
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))
        elif isinstance(obj, Matter) and _attr_changed(
            obj, "right_group", "matter_type", "our_ref", "memo", "is_deleted"
        ):
            _add_candidates(obj)

    for obj in session.deleted:
        if isinstance(obj, (MatterEvent, OfficeAction, Matter)):
            _add_candidates(obj)
        elif isinstance(obj, MatterCustomField) and _is_status_namespace(obj):
            _add_candidates(obj)
        elif isinstance(obj, DocketItem) and _is_status_red_docket(obj):
            _add_candidates(obj)
        elif isinstance(obj, Communication) and _is_response_communication(obj):
            _add_candidates(obj)
        elif isinstance(obj, CommunicationFileAsset):
            pending_comm_ids.update(_comm_id_candidates(obj))
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))
        elif isinstance(obj, FileAsset):
            pending_file_asset_ids.update(_file_asset_id_candidates(obj))

    if not matter_ids and not pending_comm_ids and not pending_file_asset_ids:
        return

    _queue_pending_ids(session, _PENDING_KEY, matter_ids)
    _queue_pending_ids(session, _PENDING_COMM_KEY, pending_comm_ids)
    _queue_pending_ids(session, _PENDING_FILE_ASSET_KEY, pending_file_asset_ids)


def _enqueue_status_recalc_candidates(session: SASession, _flush_context) -> None:
    if session.info.get(_IN_HANDLER_KEY):
        return

    pending = session.info.pop(_PENDING_KEY, None) or set()
    pending_comm_ids = session.info.pop(_PENDING_COMM_KEY, None) or set()
    pending_file_asset_ids = session.info.pop(_PENDING_FILE_ASSET_KEY, None) or set()

    pending |= _resolve_comm_ids_to_matter_ids(session, pending_comm_ids)
    pending |= _resolve_file_asset_ids_to_matter_ids(session, pending_file_asset_ids)
    if not pending:
        return

    already_queued = session.info.setdefault(_QUEUE_KEY, set())
    if not isinstance(already_queued, set):
        already_queued = set()
        session.info[_QUEUE_KEY] = already_queued

    new_ids = [mid for mid in sorted(pending) if mid not in already_queued]
    if not new_ids:
        return

    enqueue_session = db.session if has_app_context() else session
    for mid in new_ids:
        if enqueue_matter_status_recalc(
            mid,
            reason="source_changed",
            session=enqueue_session,
        ):
            already_queued.add(mid)


def _after_commit_drain(session: SASession) -> None:
    in_nested = False
    try:
        in_nested = bool(session.in_nested_transaction())
    except Exception:
        in_nested = False
    if in_nested or session.info.get(_IN_HANDLER_KEY):
        return

    matter_ids = session.info.pop(_QUEUE_KEY, None) or set()
    if not matter_ids:
        return

    if not has_app_context():
        return

    try:
        session.info[_IN_HANDLER_KEY] = True
        _defer_matter_status_recalc_drain(set(matter_ids))
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="matter_status_recalc_listeners.after_commit",
            log_key="matter_status_recalc_listeners.after_commit",
            log_window_seconds=300,
        )
    finally:
        session.info.pop(_IN_HANDLER_KEY, None)


def _after_rollback_clear(session: SASession) -> None:
    session.info.pop(_PENDING_COMM_KEY, None)
    session.info.pop(_PENDING_FILE_ASSET_KEY, None)
    session.info.pop(_PENDING_KEY, None)
    session.info.pop(_QUEUE_KEY, None)
    session.info.pop(_IN_HANDLER_KEY, None)


def _defer_matter_status_recalc_drain(matter_ids: set[str]) -> None:
    clean_ids = sorted({str(mid).strip() for mid in matter_ids if str(mid).strip()})
    if not clean_ids:
        return

    use_durable = False
    try:
        use_durable = bool(
            current_app.config.get("DEFERRED_TASKS_DURABLE_QUEUE_ENABLED", True)
        ) and not bool(current_app.config.get("TESTING"))
    except Exception:
        use_durable = False

    if use_durable:
        try:
            from app.ops.durable_queue import build_queue_from_app

            build_queue_from_app(current_app._get_current_object()).enqueue(
                task="matter_status.recalc",
                payload={"matter_ids": clean_ids, "limit": len(clean_ids)},
                queue="deferred",
                max_attempts=5,
                dedupe_key=(
                    "matter_status.recalc:"
                    + hashlib.sha256(",".join(clean_ids).encode("utf-8")).hexdigest()
                ),
                idempotency_scope="matter_status.recalc",
            )
            return
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context="matter_status_recalc_listeners.enqueue_durable",
                log_key="matter_status_recalc_listeners.enqueue_durable",
                log_window_seconds=300,
            )

    BackgroundService.run_async(
        drain_matter_status_recalc_queue,
        matter_ids=clean_ids,
        _critical=True,
        _context="after_commit.matter_status_recalc",
    )


def init_matter_status_recalc_listeners() -> None:
    global _MODULE_INITIALIZED
    if _MODULE_INITIALIZED:
        return

    listeners = (
        ("before_flush", _collect_status_recalc_candidates),
        ("after_flush_postexec", _enqueue_status_recalc_candidates),
        ("after_commit", _after_commit_drain),
        ("after_rollback", _after_rollback_clear),
    )
    for event_name, listener in listeners:
        if event.contains(SASession, event_name, listener):
            continue
        event.listen(SASession, event_name, listener)

    _MODULE_INITIALIZED = True
