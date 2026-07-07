from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.models.docket import DocketItem
from app.models.user import User
from app.services.docket_manual_state import parse_docket_memo_payload


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_deadline_bulk_api_marks_manual_abandon_and_reopens(
    admin_client, db_session, admin_user, sample_matter, monkeypatch
):
    matter_id = _matter_id(sample_matter)
    admin_user.staff_party_id = admin_user.staff_party_id or f"spid-bulk-{uuid.uuid4().hex[:8]}"
    db_session.add(admin_user)
    db_session.commit()

    docket_ids = [f"bulk-deadline-{uuid.uuid4().hex[:8]}", f"bulk-deadline-{uuid.uuid4().hex[:8]}"]
    due = (date.today() + timedelta(days=5)).isoformat()
    rows = [
        DocketItem(
            docket_id=docket_ids[0],
            matter_id=matter_id,
            category="MGMT",
            name_free="Text",
            due_date=due,
            owner_staff_party_id=admin_user.staff_party_id,
            is_deleted=False,
        ),
        DocketItem(
            docket_id=docket_ids[1],
            matter_id=matter_id,
            category="WORK",
            name_free="Text",
            due_date=due,
            owner_staff_party_id=admin_user.staff_party_id,
            is_deleted=False,
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()

    sync_calls: list[tuple[str, int | None]] = []

    def _fake_enqueue(*, docket_item, actor_id=None):
        sync_calls.append((str(docket_item.docket_id), actor_id))

    monkeypatch.setattr(
        "app.blueprints.deadline.routes.enqueue_docket_sync_for_item",
        _fake_enqueue,
    )

    resp = admin_client.post(
        "/deadline/api/deadlines/bulk",
        json={
            "ids": docket_ids,
            "action": "cancelled",
            "reason": "Text Text Text",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    assert payload["success"] is True
    assert payload["processed_count"] == 2

    db_session.expire_all()
    refreshed = {row.docket_id: db_session.get(DocketItem, row.docket_id) for row in rows}
    for docket_id in docket_ids:
        docket = refreshed[docket_id]
        assert docket is not None
        assert str(docket.done_date or "").startswith("AUTO_CANCELLED:")
        memo_payload = parse_docket_memo_payload(docket.memo)
        assert memo_payload.get("manual_abandoned") is True
        assert memo_payload.get("manual_abandon_reason") == "Text Text Text"

    assert {call[0] for call in sync_calls} == set(docket_ids)

    resp_reopen = admin_client.post(
        "/deadline/api/deadlines/bulk",
        json={
            "ids": docket_ids,
            "action": "pending",
        },
    )
    assert resp_reopen.status_code == 200
    reopen_payload = resp_reopen.get_json() or {}
    assert reopen_payload["processed_count"] == 2

    db_session.expire_all()
    for docket_id in docket_ids:
        docket = db_session.get(DocketItem, docket_id)
        assert docket is not None
        assert (docket.done_date or "") == ""
        memo_payload = parse_docket_memo_payload(docket.memo)
        assert memo_payload.get("manual_abandoned") is None
        assert memo_payload.get("manual_abandon_reason") is None


def test_deadline_patch_accepts_cancelled_status_and_clears_on_pending(
    authenticated_client, db_session, sample_matter, sample_user, monkeypatch
):
    matter_id = _matter_id(sample_matter)
    sample_user.staff_party_id = (
        sample_user.staff_party_id or f"spid-deadline-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(sample_user)
    db_session.commit()

    user = db_session.get(User, getattr(sample_user, "_test_id", None) or sample_user.id)
    staff_pid = str(getattr(user, "staff_party_id", "") or "").strip()
    docket_id = f"patch-deadline-{uuid.uuid4().hex[:8]}"

    docket = DocketItem(
        docket_id=docket_id,
        matter_id=matter_id,
        category="WORK",
        name_free="Text Text",
        due_date=(date.today() + timedelta(days=7)).isoformat(),
        owner_staff_party_id=staff_pid,
        is_deleted=False,
    )
    db_session.add(docket)
    db_session.commit()

    monkeypatch.setattr(
        "app.blueprints.deadline.routes.enqueue_docket_sync_for_item",
        lambda **_: None,
    )

    cancel_resp = authenticated_client.patch(
        f"/deadline/api/deadlines/{docket_id}",
        json={"status": "cancelled", "reason": "Text Text"},
    )
    assert cancel_resp.status_code == 200

    db_session.expire_all()
    cancelled = db_session.get(DocketItem, docket_id)
    assert cancelled is not None
    assert str(cancelled.done_date or "").startswith("AUTO_CANCELLED:")
    cancel_memo = parse_docket_memo_payload(cancelled.memo)
    assert cancel_memo.get("manual_abandon_reason") == "Text Text"

    reopen_resp = authenticated_client.patch(
        f"/deadline/api/deadlines/{docket_id}",
        json={"status": "pending"},
    )
    assert reopen_resp.status_code == 200

    db_session.expire_all()
    reopened = db_session.get(DocketItem, docket_id)
    assert reopened is not None
    assert reopened.done_date in (None, "")
    reopen_memo = parse_docket_memo_payload(reopened.memo)
    assert reopen_memo.get("manual_abandoned") is None
