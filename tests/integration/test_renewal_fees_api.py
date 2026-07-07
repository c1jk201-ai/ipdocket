"""
Integration tests for Renewal Fees API (/renewal/api/fees).

Covers permissions, pagination, next-mode, overdue status, and date range filters.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta


def _disable_annuity_management_for_matter(db_session, matter_id: str):
    from app.models.client import Client
    from app.models.ip_records import MatterCustomField

    client = Client(
        name=f"Text-{uuid.uuid4().hex[:8]}",
        extra={"annuity_management_disabled": True},
    )
    db_session.add(client)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(matter_id),
            namespace="domestic_patent",
            data={"client_id": str(client.id), "client_name": client.name},
        )
    )
    db_session.commit()
    return client


def _create_annuity(
    db_session,
    *,
    matter_id: str,
    cycle_no: int,
    due_date: str | None,
    extended_due_date: str | None = None,
    internal_due_date: str | None = None,
    status: str = "pending",
    paid_date: str | None = None,
):
    from app.models.ip_records import AnnuityItem

    annuity = AnnuityItem(
        annuity_id=uuid.uuid4().hex,
        matter_id=str(matter_id),
        cycle_no=cycle_no,
        due_date=due_date,
        extended_due_date=extended_due_date,
        internal_due_date=internal_due_date,
        annuity_status=status,
        paid_date=paid_date,
    )
    db_session.add(annuity)
    return annuity


def test_fees_post_requires_global_permission(authenticated_client, sample_matter, db_session):
    from app.models.ip_records import MatterStaffAssignment

    MatterStaffAssignment.query.filter_by(matter_id=sample_matter.matter_id).delete()
    db_session.commit()

    payload = {
        "matter_id": sample_matter.matter_id,
        "due_date": "2026-03-10",
        "year": 1,
        "fee_amount": 1000,
    }
    resp = authenticated_client.post("/renewal/api/fees", json=payload)
    assert resp.status_code == 403


def test_fees_post_requires_year(admin_client, sample_matter):
    payload = {
        "matter_id": sample_matter.matter_id,
        "due_date": "2026-03-10",
        "fee_amount": 1000,
    }
    resp = admin_client.post("/renewal/api/fees", json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error"] == "year required"


def test_fees_post_requires_due_date(admin_client, sample_matter):
    resp = admin_client.post(
        "/renewal/api/fees",
        json={
            "matter_id": sample_matter.matter_id,
            "year": 1,
            "fee_amount": 1000,
        },
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "due_date required"


def test_fees_post_rejects_non_positive_year(admin_client, sample_matter):
    payload = {
        "matter_id": sample_matter.matter_id,
        "due_date": "2026-03-10",
        "year": 0,
        "fee_amount": 1000,
    }
    resp = admin_client.post("/renewal/api/fees", json=payload)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["error"] == "invalid year (positive integer required)"


def test_fees_pagination(admin_client, db_session, sample_matter):
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=1, due_date="2026-01-10"
    )
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=2, due_date="2026-02-10"
    )
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=3, due_date="2026-03-10"
    )
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewpage=1&per_page=2")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["has_next"] is True
    assert len(data["items"]) == 2
    assert [item["cycle_no"] for item in data["items"]] == [1, 2]


def test_fees_post_preserves_existing_fee_amount_when_omitted(
    admin_client, db_session, sample_matter
):
    from app.models.ip_records import AnnuityItem

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-10",
    )
    annuity.official_fee = 12345
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.post(
        "/renewal/api/fees",
        json={
            "matter_id": sample_matter.matter_id,
            "year": 4,
            "due_date": "2026-03-11",
        },
    )
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["updated"] is True
    assert payload["id"] == annuity_id
    assert payload["matter_id"] == str(sample_matter.matter_id)

    db_session.expire_all()
    row = AnnuityItem.query.filter_by(annuity_id=annuity_id).one()
    assert row.due_date == "2026-03-11"
    assert row.official_fee == 12345


def test_fees_post_restores_soft_deleted_matching_cycle(admin_client, db_session, sample_matter):
    from app.models.ip_records import AnnuityItem

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-10",
    )
    annuity.is_deleted = True
    annuity.delete_reason = "manual delete"
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.post(
        "/renewal/api/fees",
        json={
            "matter_id": sample_matter.matter_id,
            "year": 4,
            "due_date": "2026-03-11",
            "fee_amount": 7777,
        },
    )
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["updated"] is True
    assert payload["id"] == annuity_id
    assert payload["matter_id"] == str(sample_matter.matter_id)

    db_session.expire_all()
    row = AnnuityItem.query.filter_by(annuity_id=annuity_id).one()
    assert row.is_deleted is False
    assert row.delete_reason is None
    assert row.due_date == "2026-03-11"
    assert row.official_fee == 7777


def test_fees_get_excludes_annuity_management_disabled_matters(
    admin_client, db_session, sample_matter
):
    annuity = _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=4, due_date="2026-03-10"
    )
    annuity_id = annuity.annuity_id
    _disable_annuity_management_for_matter(db_session, sample_matter.matter_id)
    db_session.commit()

    resp = admin_client.get("/renewal/api/fees?status=open")
    assert resp.status_code == 200
    data = resp.get_json()
    ids = {item["id"] for item in data["items"]}
    assert annuity_id not in ids


def test_fees_next_mode_returns_earliest_per_matter(admin_client, db_session):
    from app.models.ip_records import Matter

    matter_a_id = uuid.uuid4().hex
    matter_b_id = uuid.uuid4().hex
    matter_a = Matter(matter_id=matter_a_id, our_ref="NEXT-A", right_name="A")
    matter_b = Matter(matter_id=matter_b_id, our_ref="NEXT-B", right_name="B")
    db_session.add_all([matter_a, matter_b])

    today = date.today()
    _create_annuity(
        db_session,
        matter_id=matter_a_id,
        cycle_no=1,
        due_date=(today + timedelta(days=10)).isoformat(),
    )
    _create_annuity(
        db_session,
        matter_id=matter_a_id,
        cycle_no=2,
        due_date=(today + timedelta(days=40)).isoformat(),
    )
    _create_annuity(
        db_session,
        matter_id=matter_b_id,
        cycle_no=1,
        due_date=(today + timedelta(days=70)).isoformat(),
    )
    _create_annuity(
        db_session,
        matter_id=matter_b_id,
        cycle_no=2,
        due_date=(today + timedelta(days=20)).isoformat(),
    )
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewmode=next&status=open&next_n=1")
    assert resp.status_code == 200
    data = resp.get_json()

    items = data["items"]
    assert len(items) == 2
    by_matter = {item["matter_id"]: item for item in items}
    assert by_matter[str(matter_a_id)]["cycle_no"] == 1
    assert by_matter[str(matter_b_id)]["cycle_no"] == 2


def test_fees_next_open_prefers_upcoming_anchor_over_old_overdue(admin_client, db_session):
    from app.models.ip_records import Matter

    matter_id = uuid.uuid4().hex
    db_session.add(Matter(matter_id=matter_id, our_ref="NEXT-OVERDUE", right_name="Overdue"))

    today = date.today()
    _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=4,
        due_date=(today - timedelta(days=30)).isoformat(),
    )
    _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=5,
        due_date=(today + timedelta(days=30)).isoformat(),
    )
    _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=6,
        due_date=(today + timedelta(days=60)).isoformat(),
    )
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewmode=next&status=open&next_n=2")
    assert resp.status_code == 200
    items = resp.get_json()["items"]

    target_cycles = sorted(item["cycle_no"] for item in items if item["matter_id"] == matter_id)
    assert target_cycles == [5, 6]


def test_fee_patch_giveup_preserves_paid_future_cycles(admin_client, db_session, sample_matter):
    from app.models.ip_records import AnnuityItem

    open_row = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-10",
        status="pending",
    )
    paid_row = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=5,
        due_date="2027-03-10",
        status="paid",
        paid_date="2027-03-01",
    )
    db_session.commit()

    resp = admin_client.patch(f"/renewal/api/fees/{open_row.annuity_id}", json={"status": "giveup"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["cascade"] is True
    assert payload["updated"] == 1

    db_session.expire_all()
    rows = (
        AnnuityItem.query.filter_by(matter_id=sample_matter.matter_id)
        .order_by(AnnuityItem.cycle_no.asc())
        .all()
    )
    assert [(row.cycle_no, row.annuity_status, row.paid_date) for row in rows] == [
        (4, "giveup", None),
        (5, "paid", "2027-03-01"),
    ]


def test_fee_bulk_patch_giveup_preserves_paid_future_cycles(
    admin_client, db_session, sample_matter
):
    from app.models.ip_records import AnnuityItem

    open_row = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-10",
        status="pending",
    )
    paid_row = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=5,
        due_date="2027-03-10",
        status="paid",
        paid_date="2027-03-01",
    )
    db_session.commit()

    resp = admin_client.patch(
        "/renewal/api/fees/bulk",
        json={"ids": [open_row.annuity_id], "status": "giveup"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["cascade"] is True
    assert payload["updated"] == 1

    db_session.expire_all()
    rows = (
        AnnuityItem.query.filter_by(matter_id=sample_matter.matter_id)
        .order_by(AnnuityItem.cycle_no.asc())
        .all()
    )
    assert [(row.cycle_no, row.annuity_status, row.paid_date) for row in rows] == [
        (4, "giveup", None),
        (5, "paid", "2027-03-01"),
    ]


def test_fees_overdue_uses_effective_due_date(admin_client, db_session, sample_matter):
    today = date.today()
    internal_due = (today - timedelta(days=1)).isoformat()
    legal_due = (today + timedelta(days=10)).isoformat()

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=1,
        due_date=legal_due,
        internal_due_date=internal_due,
        status="pending",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.get("/renewal/api/fees?status=overdue")
    assert resp.status_code == 200
    data = resp.get_json()

    ids = {item["id"] for item in data["items"]}
    assert ids == {annuity_id}


def test_fees_next_overdue_mode_preserves_date_range_and_disabled_filters(admin_client, db_session):
    from app.models.ip_records import Matter

    today = date.today()
    matter_id = uuid.uuid4().hex
    disabled_matter_id = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(matter_id=matter_id, our_ref="OVERDUE-KEEP", right_name="Keep"),
            Matter(matter_id=disabled_matter_id, our_ref="OVERDUE-DISABLED", right_name="Disabled"),
        ]
    )
    _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=1,
        due_date=(today - timedelta(days=30)).isoformat(),
    )
    keep_annuity = _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=2,
        due_date=(today - timedelta(days=5)).isoformat(),
    )
    keep_annuity_id = keep_annuity.annuity_id
    _create_annuity(
        db_session,
        matter_id=disabled_matter_id,
        cycle_no=1,
        due_date=(today - timedelta(days=3)).isoformat(),
    )
    db_session.commit()
    _disable_annuity_management_for_matter(db_session, disabled_matter_id)

    start = (today - timedelta(days=10)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    resp = admin_client.get(
        f"/renewal/api/feesNewmode=next&status=overdue&next_n=1&start={start}&end={end}"
    )
    assert resp.status_code == 200
    items = resp.get_json()["items"]

    assert [item["id"] for item in items] == [keep_annuity_id]
    assert [item["matter_id"] for item in items] == [matter_id]


def test_fees_next_pending_mode_preserves_date_range_filter(admin_client, db_session):
    from app.models.ip_records import Matter

    today = date.today()
    matter_id = uuid.uuid4().hex
    db_session.add(Matter(matter_id=matter_id, our_ref="PENDING-NEXT", right_name="Pending"))
    _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=1,
        due_date=(today + timedelta(days=5)).isoformat(),
    )
    keep_annuity = _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=2,
        due_date=(today + timedelta(days=15)).isoformat(),
    )
    keep_annuity_id = keep_annuity.annuity_id
    db_session.commit()

    start = (today + timedelta(days=10)).isoformat()
    end = (today + timedelta(days=20)).isoformat()
    resp = admin_client.get(
        f"/renewal/api/feesNewmode=next&status=pending&next_n=1&start={start}&end={end}"
    )
    assert resp.status_code == 200
    items = resp.get_json()["items"]

    assert [item["id"] for item in items] == [keep_annuity_id]
    assert [item["cycle_no"] for item in items] == [2]


def test_fees_date_range_filter(admin_client, db_session, sample_matter):
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=1, due_date="2026-01-05"
    )
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=2, due_date="2026-01-15"
    )
    _create_annuity(
        db_session, matter_id=sample_matter.matter_id, cycle_no=3, due_date="2026-02-01"
    )
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewstart=2026-01-10&end=2026-01-20")
    assert resp.status_code == 200
    data = resp.get_json()

    cycle_nos = [item["cycle_no"] for item in data["items"]]
    assert cycle_nos == [2]


def test_fees_reg_source_filter(admin_client, db_session):
    from app.models.matter_facts import MatterFacts
    from app.models.ip_records import Matter

    fallback_mid = uuid.uuid4().hex
    plain_mid = uuid.uuid4().hex
    db_session.add_all(
        [
            Matter(matter_id=fallback_mid, our_ref="REG-FB", right_name="Fallback"),
            Matter(matter_id=plain_mid, our_ref="REG-PLAIN", right_name="Plain"),
            MatterFacts(
                matter_id=fallback_mid,
                registration_date_source="reg_fee_paid_date_fallback",
            ),
            MatterFacts(
                matter_id=plain_mid,
                registration_date_source="matter_event",
            ),
        ]
    )
    _create_annuity(db_session, matter_id=fallback_mid, cycle_no=1, due_date="2026-03-10")
    _create_annuity(db_session, matter_id=plain_mid, cycle_no=1, due_date="2026-03-11")
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewreg_source=fallback")
    assert resp.status_code == 200
    items = resp.get_json()["items"]
    assert {item["matter_id"] for item in items} == {fallback_mid}


def test_fees_prefers_legal_due_over_extended_due(admin_client, db_session, sample_matter):
    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-30",
        extended_due_date="2026-09-30",
        status="pending",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.get("/renewal/api/feesNewstatus=open&mode=next&next_n=1")
    assert resp.status_code == 200
    data = resp.get_json()
    target = next((item for item in data["items"] if item["id"] == annuity_id), None)
    assert target is not None
    assert target["due_date"] == "2026-03-30"
    assert target["extended_due_date"] == "2026-09-30"
    # renewal list display/filter due should follow legal due first.
    assert target["effective_due_date"] == "2026-03-30"


def test_calendar_events_prefers_legal_due_over_extended_due(
    admin_client, db_session, sample_matter
):
    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=4,
        due_date="2026-03-30",
        extended_due_date="2026-09-30",
        status="pending",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    march = admin_client.get("/renewal/api/calendar/eventsNewstart=2026-03-01&end=2026-04-01")
    assert march.status_code == 200
    march_payload = march.get_json()
    march_ids = {event["id"] for event in march_payload["events"]}
    assert annuity_id in march_ids

    september = admin_client.get("/renewal/api/calendar/eventsNewstart=2026-09-01&end=2026-10-01")
    assert september.status_code == 200
    september_payload = september.get_json()
    september_ids = {event["id"] for event in september_payload["events"]}
    assert annuity_id not in september_ids


def test_calendar_events_uses_matter_facts_right_type_for_trademark_label(
    admin_client, db_session
):
    from app.models.matter_facts import MatterFacts
    from app.models.ip_records import Matter

    matter_id = uuid.uuid4().hex
    db_session.add(Matter(matter_id=matter_id, our_ref="26TD0001US", right_name="TM"))
    db_session.add(MatterFacts(matter_id=matter_id, right_type_norm="TRADEMARK"))
    annuity = _create_annuity(
        db_session,
        matter_id=matter_id,
        cycle_no=10,
        due_date="2026-03-30",
        status="pending",
    )
    db_session.commit()

    resp = admin_client.get("/renewal/api/calendar/events?start=2026-03-01&end=2026-04-01")
    assert resp.status_code == 200
    event = next((item for item in resp.get_json()["events"] if item["id"] == annuity.annuity_id))
    assert event["title"].startswith("[Section 8/9 Renewal] 26TD0001US")
    assert event["extendedProps"]["cycle_label"] == "Section 8/9 Renewal"


def test_fee_patch_giveup_is_idempotent_for_selected_item(admin_client, db_session, sample_matter):
    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=6,
        due_date="2026-03-10",
        status="giveup",
    )
    db_session.commit()

    resp = admin_client.patch(
        f"/renewal/api/fees/{annuity.annuity_id}",
        json={"status": "giveup"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["cascade"] is True
    assert payload["updated"] == 1


def test_fee_bulk_patch_giveup_is_idempotent_for_selected_item(
    admin_client, db_session, sample_matter
):
    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=7,
        due_date="2026-03-10",
        status="giveup",
    )
    db_session.commit()

    resp = admin_client.patch(
        "/renewal/api/fees/bulk",
        json={"ids": [annuity.annuity_id], "status": "giveup"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["cascade"] is True
    assert payload["updated"] == 1


def test_fees_bulk_delete_continues_when_deletion_log_fails(
    admin_client, db_session, sample_matter, monkeypatch
):
    from app.blueprints.renewal import routes as renewal_routes
    from app.models.ip_records import AnnuityItem

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=8,
        due_date="2026-03-10",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    class _BrokenDeletionService:
        def archive(self, *args, **kwargs):
            raise RuntimeError("log insert blocked")

    monkeypatch.setattr(renewal_routes, "DeletionService", lambda: _BrokenDeletionService())

    resp = admin_client.delete("/renewal/api/fees/bulk", json={"ids": [annuity_id]})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 1

    db_session.expire_all()
    row = AnnuityItem.query.filter_by(annuity_id=annuity_id).one()
    assert row.is_deleted is True
    assert row.delete_reason == "renewal_fee_bulk_delete"


def test_calendar_events_falls_back_to_paid_date_when_due_missing(
    admin_client, db_session, sample_matter
):
    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=9,
        due_date=None,
        status="paid",
        paid_date="2026-03-12",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.get("/renewal/api/calendar/eventsNewstart=2026-03-01&end=2026-04-01")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["filled_from_paid_date"] == 1
    event = next((item for item in payload["events"] if item["id"] == annuity_id), None)
    assert event is not None
    assert event["start"] == "2026-03-12"


def test_fees_per_page_is_clamped(admin_client, db_session, sample_matter):
    for cycle_no in range(1, 4):
        _create_annuity(
            db_session,
            matter_id=sample_matter.matter_id,
            cycle_no=cycle_no,
            due_date=f"2026-03-0{cycle_no}",
        )
    db_session.commit()

    resp = admin_client.get("/renewal/api/fees?per_page=99999")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["per_page"] == 5000
    assert len(payload["items"]) == 3


def test_giveup_page_fetches_full_archive_not_next_window(admin_client):
    resp = admin_client.get("/renewal/giveup")
    assert resp.status_code == 200

    html = resp.get_data(as_text=True)
    giveup_loader = html.split("async function loadGiveups", 1)[1].split(
        "const r = await fetch",
        1,
    )[0]
    assert 'status: "giveup"' in giveup_loader
    assert 'mode: "next"' not in giveup_loader


def test_case_annuity_add_auto_marks_paid_when_paid_date_given(
    admin_client, db_session, sample_matter
):
    from app.models.ip_records import AnnuityItem

    resp = admin_client.post(
        f"/case/{sample_matter.matter_id}/annuity/add",
        data={
            "cycle_no": "11",
            "due_date": "2026-03-10",
            "annuity_status": "",
            "paid_date": "2026-03-01",
        },
    )
    assert resp.status_code == 302

    row = AnnuityItem.query.filter_by(
        matter_id=sample_matter.matter_id,
        cycle_no=11,
    ).one()
    assert row.annuity_status == "paid"
    assert row.paid_date == "2026-03-01"


def test_case_annuity_add_revives_soft_deleted_existing_cycle(
    admin_client, db_session, sample_matter
):
    from app.models.ip_records import AnnuityItem

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=12,
        due_date="2026-03-10",
    )
    annuity.is_deleted = True
    annuity.deleted_at = datetime(2026, 1, 1)
    annuity.deleted_by = 1
    annuity.delete_reason = "old delete"
    db_session.commit()

    resp = admin_client.post(
        f"/case/{sample_matter.matter_id}/annuity/add",
        data={
            "cycle_no": "12",
            "due_date": "2026-04-10",
            "annuity_status": "pending",
        },
    )
    assert resp.status_code == 302

    row = AnnuityItem.query.filter_by(
        matter_id=sample_matter.matter_id,
        cycle_no=12,
    ).one()
    assert row.annuity_id == annuity.annuity_id
    assert row.is_deleted is False
    assert row.deleted_at is None
    assert row.deleted_by is None
    assert row.delete_reason is None
    assert row.due_date == "2026-04-10"


def test_fee_detail_delete_soft_deletes_annuity(admin_client, db_session, sample_matter):
    from app.models.ip_records import AnnuityItem

    annuity = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=13,
        due_date="2026-03-10",
    )
    annuity_id = annuity.annuity_id
    db_session.commit()

    resp = admin_client.delete(f"/renewal/api/fees/{annuity_id}")
    assert resp.status_code == 200

    row = db_session.get(AnnuityItem, annuity_id)
    assert row is not None
    assert row.is_deleted is True
    assert row.deleted_at is not None
    assert row.delete_reason == "renewal_fee_delete"


def test_fees_bulk_delete_soft_deletes_annuity_items(admin_client, db_session, sample_matter):
    from app.models.ip_records import AnnuityItem

    first = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=14,
        due_date="2026-03-10",
    )
    second = _create_annuity(
        db_session,
        matter_id=sample_matter.matter_id,
        cycle_no=15,
        due_date="2026-04-10",
    )
    ids = [first.annuity_id, second.annuity_id]
    db_session.commit()

    resp = admin_client.delete("/renewal/api/fees/bulk", json={"ids": ids})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 2

    rows = AnnuityItem.query.filter(AnnuityItem.annuity_id.in_(ids)).all()
    assert len(rows) == 2
    assert {row.delete_reason for row in rows} == {"renewal_fee_bulk_delete"}
    assert all(row.is_deleted is True for row in rows)
