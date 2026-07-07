from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from typing import Iterable

from sqlalchemy import or_

from app.extensions import db
from app.models.docket import DocketItem
from app.utils.error_logging import report_swallowed_exception

_NOTICE_SEND_NAME_REF_PREFIX = "MGMT:NOTICE_SEND_3D"
_NOTICE_SEND_TITLE_HINTS = ("Notice Client", "Notice")
_SEMI_AUTO_KEY = "notice_send_semi_auto"
_RESPONSE_DOC_HINTS = (
    "",
    "",
    "",
    "response",
    "amendment",
    "opinion",
)
_NOTICE_DOC_HINTS = (
    "Notice",
    "",
    "Publication",
    "",
    "",
    "Guidance",
)
_GENERIC_TASK_HINTS = (
    "    Communication",
    "    Communication",
)
_GENERIC_NOTICE_DOC_HINTS = (
    "Notice",
    "",
    "Publication",
    "",
    "",
    "Communication",
)
_UNRELATED_DOC_HINTS = (
    "Tax",
    "",
    "invoice",
    "",
    "Payment",
    "Deposit",
    "payment",
    "",
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOC_MATCH_ALIAS_GROUPS: dict[str, tuple[str, ...]] = {
    # Practical equivalence classes for outbound mail subjects that do not
    # share enough literal tokens with the docket title.
    "gazette_decision": (
        "Publication decision notice",
        "Publication decision",
        "publicationdecision",
    ),
    "registration_decision": (
        "Notice of allowance",
        "DesignNotice of allowance",
        "decisiontoregister",
        "registrationdecision",
    ),
    "refund_notice": (
        "FeeGuidance",
        "FeeGuidance",
        "Official feeGuidance",
        "Official feeGuidance",
        "Guidance",
        "Process",
        "",
        "refundnotice",
        "refund",
    ),
    "refusal_decision": (
        "Final rejection",
        "",
        "noticeofrefusal",
        "refusaldecision",
        "refusal",
    ),
}


def _compact(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _normalize_doc_key(value: str | None) -> str:
    # Keep localized/ASCII alphanumerics only for loose contains matching.
    cleaned = re.sub(r"[^0-9A-Za-z-]+", "", str(value or ""))
    return cleaned.lower().strip()


def _normalized_hint_keys(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(key for key in (_normalize_doc_key(value) for value in values) if key)


def _char_ngrams(value: str, n: int = 2) -> set[str]:
    s = str(value or "").strip()
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i : i + n] for i in range(0, len(s) - n + 1)}


def _approx_doc_match(hint_key: str, doc_key: str) -> bool:
    h = str(hint_key or "").strip()
    d = str(doc_key or "").strip()
    if not h or not d:
        return False

    # Fast-path for strict containment.
    if h in d or d in h:
        return True

    h2 = _char_ngrams(h, n=2)
    d2 = _char_ngrams(d, n=2)
    if not h2 or not d2:
        return False

    inter = len(h2 & d2)
    if inter <= 0:
        return False

    union = len(h2 | d2)
    jaccard = inter / max(1, union)
    hint_coverage = inter / max(1, len(h2))

    # Allow practical title variants (//Add ) without opening false positives.
    return (hint_coverage >= 0.55 and jaccard >= 0.28) or hint_coverage >= 0.72


def _doc_match_alias_groups(value: str | None) -> set[str]:
    key = _normalize_doc_key(value)
    if not key:
        return set()
    groups: set[str] = set()
    for group, aliases in _DOC_MATCH_ALIAS_GROUPS.items():
        if any(alias in key for alias in _normalized_hint_keys(aliases)):
            groups.add(group)
    return groups


def _alias_doc_match(task_hint: str | None, doc_name: str | None) -> bool:
    task_groups = _doc_match_alias_groups(task_hint)
    if not task_groups:
        return False
    doc_groups = _doc_match_alias_groups(doc_name)
    if not doc_groups:
        return False
    return bool(task_groups & doc_groups)


def _source_text_matches_task(task_hint: str | None, source_text: str | None) -> bool:
    hint_key = _normalize_doc_key(task_hint)
    source_key = _normalize_doc_key(source_text)
    if not source_key:
        return False
    if hint_key:
        if hint_key in source_key:
            return True
        if _alias_doc_match(task_hint, source_text):
            return True
        if _is_generic_task_hint(task_hint):
            return _is_generic_notice_doc(source_text)
        return False
    return False


def _parse_memo_payload(raw_memo: str | None) -> tuple[dict, str]:
    """
    Returns:
        (payload_dict, legacy_note_text)
    """
    raw = str(raw_memo or "").strip()
    if not raw:
        return {}, ""
    # Legacy memo often stores plain text notes; avoid JSON parse noise for those.
    if raw[0] not in {"{", "["}:
        return {}, raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed, ""
    except json.JSONDecodeError as exc:
        report_swallowed_exception(
            exc,
            context="notice_send_semi_close.parse_memo_payload",
            log_key="notice_send_semi_close.parse_memo_payload",
        )
    except Exception as exc:
        report_swallowed_exception(
            exc,
            context="notice_send_semi_close.parse_memo_payload",
            log_key="notice_send_semi_close.parse_memo_payload",
        )
    return {}, raw


def _dump_memo_payload(payload: dict, legacy_note: str) -> str:
    out = dict(payload or {})
    note = (legacy_note or "").strip()
    if note and not str(out.get("legacy_note") or "").strip():
        out["legacy_note"] = note
    return json.dumps(out, ensure_ascii=False)


def _is_response_like_doc(doc_name: str | None) -> bool:
    normalized = (doc_name or "").strip().lower()
    if not normalized:
        return False
    return any(hint in normalized for hint in (h.strip().lower() for h in _RESPONSE_DOC_HINTS) if hint)


def _task_doc_hint(item: DocketItem, state: dict) -> str:
    title = str(getattr(item, "name_free", "") or "").strip()
    for separator in ("·", " - "):
        if separator in title:
            tail = title.split(separator, 1)[1].strip()
            if tail:
                return tail
    hinted = str(state.get("trigger_doc_name") or "").strip()
    return hinted


def _is_notice_related_doc(doc_name: str | None) -> bool:
    key = _normalize_doc_key(doc_name)
    if not key:
        return False
    if any(h in key for h in _normalized_hint_keys(_UNRELATED_DOC_HINTS)):
        return False
    return any(h in key for h in _normalized_hint_keys(_NOTICE_DOC_HINTS))


def _is_generic_task_hint(hint: str | None) -> bool:
    key = _normalize_doc_key(hint)
    if not key:
        return False
    return any(h in key for h in _normalized_hint_keys(_GENERIC_TASK_HINTS))


def _is_generic_notice_doc(doc_name: str | None) -> bool:
    key = _normalize_doc_key(doc_name)
    if not key:
        return False
    if any(h in key for h in _normalized_hint_keys(_UNRELATED_DOC_HINTS)):
        return False
    return any(h in key for h in _normalized_hint_keys(_GENERIC_NOTICE_DOC_HINTS))


def _normalize_iso_date(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw[:10]
    return candidate if _ISO_DATE_RE.match(candidate) else ""


def _task_anchor_date(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for k in ("received_date", "notified_date", "trigger_date", "candidate_at"):
        d = _normalize_iso_date(payload.get(k))
        if d:
            return d
    return ""


def _is_relevant_doc_for_task(
    *,
    doc_name: str | None,
    item: DocketItem,
    state: dict,
    source_text: str | None = None,
) -> bool:
    doc_key = _normalize_doc_key(doc_name)

    task_hint = _task_doc_hint(item, state)
    hint_key = _normalize_doc_key(task_hint)
    if doc_key:
        if hint_key:
            # Prefer title affinity (strict or fuzzy) when the task carries explicit doc hint.
            if _approx_doc_match(hint_key, doc_key):
                return True
            if _alias_doc_match(task_hint, doc_name):
                return True
            if _is_generic_task_hint(task_hint):
                # Generic task titles can only use a stricter broad fallback.
                return _is_generic_notice_doc(doc_name) or _source_text_matches_task(
                    task_hint, source_text
                )
        elif _is_notice_related_doc(doc_name):
            return True

    if hint_key:
        # Source text matching is intentionally stricter than title matching because email bodies
        # are much broader than subjects and should not trigger broad notice fallbacks.
        return _source_text_matches_task(task_hint, source_text)

    return False


def is_notice_send_task(item: DocketItem) -> bool:
    """Whether the docket item is a notice-send(3d) task."""
    name_ref = str(getattr(item, "name_ref", "") or "").strip().upper()
    if name_ref.startswith(_NOTICE_SEND_NAME_REF_PREFIX):
        return True
    merged = _compact(
        f"{getattr(item, 'name_free', '')}{getattr(item, 'name_ref', '')}{getattr(item, 'category', '')}"
    )
    return any(_compact(hint) in merged for hint in _NOTICE_SEND_TITLE_HINTS)


def _state_from_payload(payload: dict) -> dict:
    state = payload.get(_SEMI_AUTO_KEY)
    return dict(state) if isinstance(state, dict) else {}


def _normalize_comm_type(value: str | None) -> str:
    return str(value or "").strip().upper()


def _communication_doc_name(comm: dict | None) -> str:
    row = comm or {}
    return str(row.get("doc_name") or row.get("note") or row.get("subject") or "").strip()


def _communication_source_text(comm: dict | None) -> str:
    row = comm or {}
    return str(row.get("source_text") or row.get("body_text") or "").strip()


def _sorted_communications(rows: Iterable[dict] | None) -> list[dict]:
    items = [dict(row or {}) for row in (rows or [])]

    def _sort_key(row: dict) -> tuple[str, str]:
        return (
            _normalize_iso_date(str(row.get("sent_date") or "").strip()),
            str(row.get("comm_id") or "").strip(),
        )

    return sorted(items, key=_sort_key, reverse=True)


def _matching_communication_for_task(
    *,
    item: DocketItem,
    payload: dict,
    state: dict,
    communications: Iterable[dict] | None,
) -> dict | None:
    anchor_date = _task_anchor_date(payload)

    for comm in _sorted_communications(communications):
        sent_date = _normalize_iso_date(str(comm.get("sent_date") or "").strip())
        direction = str(comm.get("direction") or "").strip()
        action = str(comm.get("action") or "").strip()
        if not sent_date and direction != "Send" and action != "Send":
            continue
        if sent_date and anchor_date and sent_date < anchor_date:
            continue

        comm_type = _normalize_comm_type(comm.get("comm_type"))
        if comm_type and comm_type != "M":
            continue

        doc_name = _communication_doc_name(comm)
        source_text = _communication_source_text(comm)
        if doc_name and _is_response_like_doc(doc_name):
            continue
        if not _is_relevant_doc_for_task(
            doc_name=doc_name,
            item=item,
            state=state,
            source_text=source_text,
        ):
            continue

        matched_doc_name = doc_name or str(_task_doc_hint(item, state) or "").strip()
        return {
            "sent_date": sent_date,
            "doc_name": matched_doc_name,
            "comm_id": str(comm.get("comm_id") or "").strip(),
        }
    return None


def mark_notice_send_candidates(
    *,
    matter_id: str,
    direction: str | None,
    doc_name: str | None,
    source_text: str | None = None,
    sent_date: str | None = None,
    comm_type: str | None = None,
    source: str | None = None,
    actor_user_id: int | str | None = None,
    candidate_date: str | None = None,
) -> int:
    """
    Mark open notice-send tasks as semi-auto close candidates.

    Rules:
    - only outbound letters (direction='Send')
    - skip response-like documents (comm_type='R' or response-ish title)
    - prompted tasks may keep their 1-time popup closed while still receiving recommendation state
    """
    if (direction or "").strip() != "Send":
        return 0
    normalized_comm_type = _normalize_comm_type(comm_type)
    if normalized_comm_type and normalized_comm_type != "M":
        return 0
    if _is_response_like_doc(doc_name):
        return 0

    sent_ymd = _normalize_iso_date(sent_date)
    today = sent_ymd or (candidate_date or "").strip() or date.today().isoformat()
    updated = 0

    query = DocketItem.query.filter(
        DocketItem.matter_id == str(matter_id),
        or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
    )
    if hasattr(DocketItem, "is_deleted"):
        query = query.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))

    for item in query.all():
        if not is_notice_send_task(item):
            continue

        try:
            payload, legacy_note = _parse_memo_payload(getattr(item, "memo", None))
            before = deepcopy(payload)
            state = _state_from_payload(payload)
            anchor_date = _task_anchor_date(payload)

            if sent_ymd and anchor_date and sent_ymd < anchor_date:
                continue
            if not _is_relevant_doc_for_task(
                doc_name=doc_name,
                item=item,
                state=state,
                source_text=source_text,
            ):
                continue

            state["candidate"] = True
            state.setdefault("prompted", False)
            state["candidate_at"] = today
            if source:
                state["source"] = str(source)
            if doc_name:
                state["trigger_doc_name"] = str(doc_name).strip()[:200]
            if actor_user_id:
                state["candidate_by"] = str(actor_user_id)

            payload[_SEMI_AUTO_KEY] = state
            if payload != before:
                item.memo = _dump_memo_payload(payload, legacy_note)
                db.session.add(item)
                updated += 1
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=f"notice_send_semi_close.mark_candidates(docket_id={getattr(item, 'docket_id', '')})",
                log_key="notice_send_semi_close.mark_candidates",
                log_window_seconds=300,
            )
    return updated


def _append_comm_source_text(
    bucket: dict[str, list[str]], comm_id: str | None, source_text: str | None
) -> None:
    cid = str(comm_id or "").strip()
    text = str(source_text or "").strip()
    if not cid or not text:
        return
    rows = bucket.setdefault(cid, [])
    if text not in rows:
        rows.append(text)


def _load_comm_source_text_by_comm_id(comm_ids: Iterable[str] | None) -> dict[str, str]:
    normalized_ids = sorted({str(comm_id or "").strip() for comm_id in (comm_ids or []) if comm_id})
    if not normalized_ids:
        return {}

    from app.models.email_automation import EmailMessage, EmailMessageMatterLink

    source_texts: dict[str, list[str]] = {}

    linked_rows = (
        db.session.query(EmailMessageMatterLink.comm_id, EmailMessage.body_text)
        .join(EmailMessage, EmailMessage.id == EmailMessageMatterLink.email_id)
        .filter(EmailMessageMatterLink.comm_id.in_(normalized_ids))
        .filter(EmailMessage.body_text.isnot(None))
        .all()
    )
    for comm_id, body_text in linked_rows:
        _append_comm_source_text(source_texts, comm_id, body_text)

    legacy_rows = (
        EmailMessage.query.with_entities(EmailMessage.linked_comm_id, EmailMessage.body_text)
        .filter(EmailMessage.linked_comm_id.in_(normalized_ids))
        .filter(EmailMessage.body_text.isnot(None))
        .all()
    )
    for comm_id, body_text in legacy_rows:
        _append_comm_source_text(source_texts, comm_id, body_text)

    return {comm_id: "\n\n".join(parts) for comm_id, parts in source_texts.items() if parts}


def load_notice_send_communications_for_matter(
    *, matter_id: str, limit: int | None = None
) -> list[dict]:
    mid = str(matter_id or "").strip()
    if not mid:
        return []

    from app.models.communication import Communication

    query = (
        Communication.query.with_entities(
            Communication.comm_id,
            Communication.comm_type,
            Communication.sent_date,
            Communication.note,
        )
        .filter(Communication.matter_id == mid)
        .filter(Communication.sent_date.isnot(None))
        .order_by(Communication.sent_date.desc(), Communication.comm_id.desc())
    )
    if limit is not None:
        query = query.limit(max(1, int(limit or 1)))

    comm_rows = query.all()
    source_text_by_comm_id = _load_comm_source_text_by_comm_id(
        [str(comm_id or "").strip() for comm_id, *_rest in comm_rows]
    )
    return [
        {
            "comm_id": str(comm_id or "").strip(),
            "comm_type": str(comm_type or "").strip(),
            "sent_date": str(sent_date or "").strip(),
            "note": str(note or "").strip(),
            "source_text": source_text_by_comm_id.get(str(comm_id or "").strip(), ""),
        }
        for comm_id, comm_type, sent_date, note in comm_rows
    ]


def load_notice_send_communications_for_matters(
    *, matter_ids: Iterable[str]
) -> dict[str, list[dict]]:
    mids = sorted({str(matter_id or "").strip() for matter_id in (matter_ids or []) if matter_id})
    if not mids:
        return {}

    from app.models.communication import Communication

    comm_rows = (
        Communication.query.with_entities(
            Communication.matter_id,
            Communication.comm_id,
            Communication.comm_type,
            Communication.sent_date,
            Communication.note,
        )
        .filter(Communication.matter_id.in_(mids))
        .filter(Communication.sent_date.isnot(None))
        .order_by(
            Communication.matter_id.asc(),
            Communication.sent_date.desc(),
            Communication.comm_id.desc(),
        )
        .all()
    )
    source_text_by_comm_id = _load_comm_source_text_by_comm_id(
        [str(comm_id or "").strip() for _mid, comm_id, *_rest in comm_rows]
    )

    by_matter_id: dict[str, list[dict]] = {}
    for matter_id, comm_id, comm_type, sent_date, note in comm_rows:
        mid = str(matter_id or "").strip()
        cid = str(comm_id or "").strip()
        if not mid or not cid:
            continue
        by_matter_id.setdefault(mid, []).append(
            {
                "comm_id": cid,
                "comm_type": str(comm_type or "").strip(),
                "sent_date": str(sent_date or "").strip(),
                "note": str(note or "").strip(),
                "source_text": source_text_by_comm_id.get(cid, ""),
            }
        )
    return by_matter_id


def _preserve_notice_send_history(state: dict) -> dict:
    kept: dict[str, object] = {}
    prompted = bool((state or {}).get("prompted"))
    if prompted:
        kept["prompted"] = True
    for key in ("decision", "prompted_at", "prompted_by"):
        value = (state or {}).get(key)
        if str(value or "").strip():
            kept[key] = value
    return kept


def refresh_notice_send_candidates_for_matter(
    *,
    matter_id: str,
    source: str | None = None,
    actor_user_id: int | str | None = None,
) -> int:
    """
    Recompute semi-auto candidate flags from persisted communication history.

    Use this after communication edit/delete paths so stale candidates are
    cleared and the best remaining outbound mail becomes the new recommendation.
    """
    mid = str(matter_id or "").strip()
    if not mid:
        return 0

    query = DocketItem.query.filter(
        DocketItem.matter_id == mid,
        or_(DocketItem.done_date.is_(None), DocketItem.done_date == ""),
    )
    if hasattr(DocketItem, "is_deleted"):
        query = query.filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))

    docket_items = [item for item in query.all() if is_notice_send_task(item)]
    if not docket_items:
        return 0

    communications = load_notice_send_communications_for_matter(matter_id=mid)

    updated = 0
    for item in docket_items:
        try:
            payload, legacy_note = _parse_memo_payload(getattr(item, "memo", None))
            before = deepcopy(payload)
            state = _state_from_payload(payload)

            matched = _matching_communication_for_task(
                item=item,
                payload=payload,
                state=state,
                communications=communications,
            )
            if matched:
                next_state = _preserve_notice_send_history(state)
                next_state["candidate"] = True
                next_state.setdefault("prompted", False)
                next_state["candidate_at"] = matched.get("sent_date") or date.today().isoformat()
                next_state["trigger_doc_name"] = str(matched.get("doc_name") or "").strip()[:200]
                if source:
                    next_state["source"] = str(source)
                elif not str(next_state.get("source") or "").strip():
                    next_state.pop("source", None)
                if actor_user_id:
                    next_state["candidate_by"] = str(actor_user_id)
                payload[_SEMI_AUTO_KEY] = next_state
            else:
                next_state = _preserve_notice_send_history(state)
                if next_state:
                    next_state["candidate"] = False
                    payload[_SEMI_AUTO_KEY] = next_state
                else:
                    payload.pop(_SEMI_AUTO_KEY, None)

            if payload != before:
                item.memo = _dump_memo_payload(payload, legacy_note)
                db.session.add(item)
                updated += 1
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=(
                    "notice_send_semi_close.refresh_candidates"
                    f"(docket_id={getattr(item, 'docket_id', '')})"
                ),
                log_key="notice_send_semi_close.refresh_candidates",
                log_window_seconds=300,
            )
    return updated


def get_notice_send_prompt_candidate(docket_items: Iterable[DocketItem]) -> dict | None:
    """
    Pick one pending semi-auto prompt candidate for case-view entry.
    Returns a JSON-serializable dict or None.
    """
    for item in docket_items or []:
        try:
            if not is_notice_send_task(item):
                continue
            if str(getattr(item, "done_date", "") or "").strip():
                continue

            payload, _legacy_note = _parse_memo_payload(getattr(item, "memo", None))
            state = _state_from_payload(payload)
            if not bool(state.get("candidate")):
                continue
            if bool(state.get("prompted")):
                continue

            title = (
                str(getattr(item, "name_free", "") or "").strip()
                or str(getattr(item, "name_ref", "") or "").strip()
                or "Notice Client(3 )"
            )
            return {
                "docket_id": str(getattr(item, "docket_id", "") or "").strip(),
                "title": title,
                "question": f"{title}  Done?",
            }
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=f"notice_send_semi_close.get_candidate(docket_id={getattr(item, 'docket_id', '')})",
                log_key="notice_send_semi_close.get_candidate",
                log_window_seconds=300,
            )
    return None


def infer_notice_send_prompt_candidate_from_communications(
    *,
    docket_items: Iterable[DocketItem],
    communications: Iterable[dict] | None,
    respect_prompted: bool = True,
) -> dict | None:
    """
    Best-effort fallback when candidate flags were not stored at write-time.

    This does not mutate DB; it only derives a prompt from existing outbound
    communication history.
    """
    rows = _sorted_communications(communications)
    if not rows:
        return None

    for item in docket_items or []:
        try:
            if not is_notice_send_task(item):
                continue
            if str(getattr(item, "done_date", "") or "").strip():
                continue

            payload, _legacy_note = _parse_memo_payload(getattr(item, "memo", None))
            state = _state_from_payload(payload)
            if respect_prompted and bool(state.get("prompted")):
                continue

            matched = _matching_communication_for_task(
                item=item,
                payload=payload,
                state=state,
                communications=rows,
            )
            if not matched:
                continue

            title = (
                str(getattr(item, "name_free", "") or "").strip()
                or str(getattr(item, "name_ref", "") or "").strip()
                or "Notice Client(3 )"
            )
            return {
                "docket_id": str(getattr(item, "docket_id", "") or "").strip(),
                "title": title,
                "question": f"{title}  Done?",
                "inferred": True,
                "matched_doc_name": str(matched.get("doc_name") or "").strip(),
            }
        except Exception as exc:
            report_swallowed_exception(
                exc,
                context=f"notice_send_semi_close.infer_candidate(docket_id={getattr(item, 'docket_id', '')})",
                log_key="notice_send_semi_close.infer_candidate",
                log_window_seconds=300,
            )
    return None


def get_notice_send_recommendation_state(item: DocketItem | None) -> dict:
    """
    Return recommendation state for Worklog/Case UIs.

    recommended=True means "candidate exists for an open notice-send task".
    """
    if not item:
        return {
            "candidate": False,
            "prompted": False,
            "recommended": False,
            "trigger_doc_name": "",
            "decision": "",
        }

    if not is_notice_send_task(item):
        return {
            "candidate": False,
            "prompted": False,
            "recommended": False,
            "trigger_doc_name": "",
            "decision": "",
        }

    payload, _legacy_note = _parse_memo_payload(getattr(item, "memo", None))
    state = _state_from_payload(payload)
    candidate = bool(state.get("candidate"))
    prompted = bool(state.get("prompted"))
    is_open = not bool(str(getattr(item, "done_date", "") or "").strip())
    trigger_doc_name = str(state.get("trigger_doc_name") or "").strip()
    decision = str(state.get("decision") or "").strip().lower()

    return {
        "candidate": candidate,
        "prompted": prompted,
        "recommended": bool(candidate and is_open),
        "trigger_doc_name": trigger_doc_name,
        "decision": decision,
    }


def ack_notice_send_prompt(
    *,
    matter_id: str,
    docket_id: str,
    decision: str,
    actor_user_id: int | None = None,
    prompted_date: str | None = None,
) -> bool:
    """
    Record that the one-time semi-auto popup has been answered.
    """
    mid = str(matter_id or "").strip()
    did = str(docket_id or "").strip()
    if not mid or not did:
        return False

    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"yes", "no"}:
        return False

    item = DocketItem.query.filter_by(matter_id=mid, docket_id=did).first()
    if not item or not is_notice_send_task(item):
        return False

    payload, legacy_note = _parse_memo_payload(getattr(item, "memo", None))
    state = _state_from_payload(payload)
    if bool(state.get("prompted")):
        return False

    if not bool(state.get("candidate")):
        matched = _matching_communication_for_task(
            item=item,
            payload=payload,
            state=state,
            communications=load_notice_send_communications_for_matter(matter_id=mid),
        )
        if matched:
            state["candidate"] = True
            if matched.get("sent_date"):
                state["candidate_at"] = str(matched.get("sent_date") or "").strip()
            if matched.get("doc_name"):
                state["trigger_doc_name"] = str(matched.get("doc_name") or "").strip()[:200]
        else:
            # Allow ack for inferred prompts even when historical candidate flag is missing.
            state["candidate"] = False

    state["prompted"] = True
    state["decision"] = normalized_decision
    state["prompted_at"] = (prompted_date or "").strip() or date.today().isoformat()
    if actor_user_id:
        state["prompted_by"] = str(actor_user_id)

    payload[_SEMI_AUTO_KEY] = state
    item.memo = _dump_memo_payload(payload, legacy_note)
    db.session.add(item)
    return True
