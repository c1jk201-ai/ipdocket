from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

from app.models.matter import MatterCustomField, MatterEvent
from app.services.core.config_service import ConfigService
from app.services.workflow.sync_requests import (
    enqueue_annuity_ensure_for_matter_ids_after_commit,
    enqueue_annuity_sync_for_matter_ids_after_commit,
)
from app.utils.error_logging import report_swallowed_exception
from app.utils.registration_date import (
    REG_FEE_PAID_CUSTOM_KEYS,
    REG_FEE_PAID_EVENT_KEYS,
    REGISTRATION_CUSTOM_KEYS,
    REGISTRATION_EVENT_KEYS,
    find_first_date,
    normalize_key,
)

_ABANDON_KEYS = {"Abandoned/Withdrawn", "ABANDON_WITHDRAW_DATE"}
_KEY_ANNUITY_AUTOGEN = "annuity_autogen_matter_ids"
_KEY_ANNUITY_GIVEUP = "annuity_giveup_matter_ids"
_KEY_ANNUITY_PROCESSING = "annuity_autogen_processing"

_REG_EVENT_KEY_NORMS = {normalize_key(k) for k in REGISTRATION_EVENT_KEYS if k}
_REG_FEE_EVENT_KEY_NORMS = {normalize_key(k) for k in REG_FEE_PAID_EVENT_KEYS if k}
_ABANDON_KEY_NORMS = {normalize_key(k) for k in _ABANDON_KEYS if k}
_TERM_EXPIRY_KEY_NORMS = {
    normalize_key(k) for k in (" Period ", "TERM_EXPIRY_DATE", "term_expiry_date") if k
}


def _allow_reg_fee_paid_fallback() -> bool:
    # Avoid importing annuity_service here; keep config lookup lightweight.
    return bool(ConfigService.get_bool("ANNUITY_ALLOW_REG_FEE_PAID_FALLBACK", False))


def _queue_matter_id(session: Session, key: str, matter_id: str) -> None:
    q = session.info.setdefault(key, set())
    if not isinstance(q, set):
        q = set()
        session.info[key] = q
    q.add(str(matter_id))


def _trigger_annuity_sync(mapper, connection, target):
    matter_id = target.matter_id
    if not matter_id:
        return
    session = object_session(target)
    if session is None:
        return

    # Check for relevant event keys
    is_reg = False
    is_reg_fee = False
    is_abandon = False
    is_term_expiry = False
    if isinstance(target, MatterEvent):
        key_norm = normalize_key(getattr(target, "event_key", None))
        is_reg = key_norm in _REG_EVENT_KEY_NORMS or ("Registration date" in key_norm)
        is_reg_fee = key_norm in _REG_FEE_EVENT_KEY_NORMS or ("RegistrationPayment" in key_norm)
        is_abandon = key_norm in _ABANDON_KEY_NORMS
        is_term_expiry = key_norm in _TERM_EXPIRY_KEY_NORMS or ("Period" in key_norm)
    elif isinstance(target, MatterCustomField):
        data = target.data or {}
        is_reg = (
            find_first_date(data, REGISTRATION_CUSTOM_KEYS, key_substring="Registration date") is not None
        )
        is_reg_fee = (
            find_first_date(data, REG_FEE_PAID_CUSTOM_KEYS, key_substring="RegistrationPayment")
            is not None
        )
        is_abandon = (
            find_first_date(data, ("abandon_date", "Abandoned/Withdrawn"), key_substring="Abandoned")
            is not None
        )
        is_term_expiry = (
            find_first_date(
                data,
                ("term_expiry_date", "TERM_EXPIRY_DATE", " Period "),
                key_substring="Period",
            )
            is not None
        )
    else:
        return

    # Optional: treat registration-fee paid date as a registration trigger.
    if is_reg_fee and _allow_reg_fee_paid_fallback():
        is_reg = True

    if not (is_reg or is_abandon or is_term_expiry):
        return

    if is_reg or is_term_expiry:
        _queue_matter_id(session, _KEY_ANNUITY_AUTOGEN, str(matter_id))
    if is_abandon:
        _queue_matter_id(session, _KEY_ANNUITY_GIVEUP, str(matter_id))


def _drain_annuity_queue(session: Session) -> None:
    if session.info.get(_KEY_ANNUITY_PROCESSING):
        return

    autogen = session.info.pop(_KEY_ANNUITY_AUTOGEN, set()) or set()
    giveups = session.info.pop(_KEY_ANNUITY_GIVEUP, set()) or set()
    if not autogen and not giveups:
        return

    session.info[_KEY_ANNUITY_PROCESSING] = True
    try:
        # IMPORTANT:
        # `after_commit` hook must not emit SQL on the same Session.
        # Only schedule background sync execution here.
        if autogen:
            enqueue_annuity_ensure_for_matter_ids_after_commit(
                {str(mid) for mid in autogen},
                allow_testing_durable=False,
            )
        if giveups:
            enqueue_annuity_sync_for_matter_ids_after_commit(
                {str(mid) for mid in giveups},
                allow_testing_durable=False,
            )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="annuity_listeners._drain_annuity_queue",
            log_key="annuity_listeners._drain_annuity_queue",
            log_window_seconds=300,
        )
        if autogen:
            session.info[_KEY_ANNUITY_AUTOGEN] = set(autogen)
        if giveups:
            session.info[_KEY_ANNUITY_GIVEUP] = set(giveups)
    finally:
        session.info.pop(_KEY_ANNUITY_PROCESSING, None)


def _clear_annuity_queue(session: Session) -> None:
    session.info.pop(_KEY_ANNUITY_AUTOGEN, None)
    session.info.pop(_KEY_ANNUITY_GIVEUP, None)
    session.info.pop(_KEY_ANNUITY_PROCESSING, None)


def register_listeners():
    # Listen for MatterEvent changes
    event.listen(MatterEvent, "after_insert", _trigger_annuity_sync)
    event.listen(MatterEvent, "after_update", _trigger_annuity_sync)

    # Listen for MatterCustomField changes (legacy support)
    event.listen(MatterCustomField, "after_insert", _trigger_annuity_sync)
    event.listen(MatterCustomField, "after_update", _trigger_annuity_sync)

    # Drain queued annuity updates after commit to avoid flush-time writes.
    event.listen(Session, "after_commit", _drain_annuity_queue)
    event.listen(Session, "after_rollback", _clear_annuity_queue)
