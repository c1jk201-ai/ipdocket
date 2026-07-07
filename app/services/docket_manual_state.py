from __future__ import annotations

import json
from datetime import date

_LEGACY_MEMO_TEXT_KEY = "_legacy_text"
MANUAL_ABANDON_LOCK_REASON = "manual_abandon"
_TRUTHY_TOKENS = frozenset({"1", "T", "TRUE", "Y", "YES", "ON"})


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in _TRUTHY_TOKENS


def _date_token(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if ":" in raw and raw.split(":", 1)[0] in {"AUTO_CANCELLED", "AUTO_EXPIRED"}:
        raw = raw.split(":", 1)[1].strip()
    return raw.split("T")[0].strip()


def parse_docket_memo_payload(memo: str | None) -> dict[str, object]:
    raw = (memo or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return dict(parsed)
    return {_LEGACY_MEMO_TEXT_KEY: raw}


def serialize_docket_memo_payload(payload: dict[str, object] | None) -> str | None:
    data = dict(payload or {})
    for key in list(data.keys()):
        value = data.get(key)
        if value is None:
            data.pop(key, None)
            continue
        if isinstance(value, str) and not value.strip():
            data.pop(key, None)

    if not data:
        return None
    if set(data.keys()) == {_LEGACY_MEMO_TEXT_KEY}:
        raw = str(data.get(_LEGACY_MEMO_TEXT_KEY) or "").strip()
        return raw or None
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def memo_has_manual_abandon_lock(memo: str | dict[str, object] | None) -> bool:
    payload = memo if isinstance(memo, dict) else parse_docket_memo_payload(memo)
    if _is_truthy(payload.get("manual_abandoned")):
        return True
    if str(payload.get("lock_reason") or "").strip().lower() != MANUAL_ABANDON_LOCK_REASON:
        return False
    return _is_truthy(payload.get("locked"))


def mark_docket_manual_abandoned(
    docket_item,
    *,
    reason: str | None = None,
    when: date | str | None = None,
) -> None:
    payload = parse_docket_memo_payload(getattr(docket_item, "memo", None))
    payload["manual_abandoned"] = True
    payload["manual_abandoned_at"] = _date_token(when) or date.today().isoformat()
    if reason:
        payload["manual_abandon_reason"] = str(reason).strip()
    else:
        payload.pop("manual_abandon_reason", None)
    payload["locked"] = True
    payload["lock_reason"] = MANUAL_ABANDON_LOCK_REASON
    docket_item.memo = serialize_docket_memo_payload(payload)


def clear_docket_manual_abandoned(docket_item) -> None:
    payload = parse_docket_memo_payload(getattr(docket_item, "memo", None))
    payload.pop("manual_abandoned", None)
    payload.pop("manual_abandoned_at", None)
    payload.pop("manual_abandon_reason", None)
    if str(payload.get("lock_reason") or "").strip().lower() == MANUAL_ABANDON_LOCK_REASON:
        payload.pop("lock_reason", None)
        payload.pop("locked", None)
    docket_item.memo = serialize_docket_memo_payload(payload)
