from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_open_docket_uniqueness_uses_extended_due_date(db_session, sample_matter):
    from app.models.docket import DocketItem

    mid = _matter_id(sample_matter)
    db_session.add(
        DocketItem(
            docket_id="e" * 32,
            matter_id=mid,
            category="MGMT_WORK",
            name_ref="MGMT:INTERNAL:DUP",
            name_free="internal due",
            due_date=None,
            extended_due_date="2026-04-01",
            done_date=None,
        )
    )
    db_session.flush()
    db_session.add(
        DocketItem(
            docket_id="d" * 32,
            matter_id=mid,
            category="MGMT_WORK",
            name_ref="MGMT:INTERNAL:DUP",
            name_free="internal due duplicate",
            due_date=None,
            extended_due_date="2026-04-01",
            done_date=None,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_mgmt_upsert_reopens_done_row(db_session, sample_matter, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    existing = DocketItem(
        docket_id="a" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:REOPEN:TEST",
        name_free="reopen",
        due_date="2026-01-10",
        done_date="2026-01-20",
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:REOPEN:TEST",
        category="MGMT",
        title="reopen updated",
        due=date(2026, 2, 1),
        owner=None,
        memo=None,
    )
    db_session.flush()
    db_session.refresh(existing)

    assert row.docket_id == existing.docket_id
    assert existing.due_date == "2026-02-01"
    assert not (existing.done_date or "").strip()


def test_mgmt_upsert_preserves_manual_abandoned_done_row(db_session, sample_matter, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    existing = DocketItem(
        docket_id="m" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:MANUAL:LOCKED",
        name_free="manual lock",
        due_date="2026-01-10",
        done_date="AUTO_CANCELLED:2026-01-20",
        memo=json.dumps(
            {
                "manual_abandoned": True,
                "manual_abandoned_at": "2026-01-20",
                "locked": True,
                "lock_reason": "manual_abandon",
            },
            ensure_ascii=False,
        ),
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:MANUAL:LOCKED",
        category="MGMT",
        title="manual lock updated",
        due=date(2026, 2, 1),
        owner=None,
        memo=None,
    )
    db_session.flush()
    db_session.refresh(existing)

    assert row.docket_id == existing.docket_id
    assert existing.due_date == "2026-01-10"
    assert existing.done_date == "AUTO_CANCELLED:2026-01-20"


def test_mgmt_upsert_prefers_matching_open_due_row(db_session, sample_matter, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    older = DocketItem(
        docket_id="f" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:DUE:MATCH:TEST",
        name_free="older",
        due_date="2026-03-01",
        done_date=None,
    )
    matching = DocketItem(
        docket_id="1" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:DUE:MATCH:TEST",
        name_free="matching",
        due_date="2026-04-01",
        done_date=None,
    )
    db_session.add(older)
    db_session.add(matching)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:DUE:MATCH:TEST",
        category="MGMT",
        title="matching",
        due=date(2026, 4, 1),
        owner=None,
        memo=None,
    )
    db_session.flush()
    db_session.refresh(older)
    db_session.refresh(matching)

    assert row.docket_id == matching.docket_id
    assert not (matching.done_date or "").strip()
    assert (older.done_date or "").startswith("AUTO_CANCELLED:")


def test_mgmt_upsert_ignores_soft_deleted_candidate(db_session, sample_matter, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    deleted_row = DocketItem(
        docket_id="c" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:SOFT:DELETED",
        name_free="deleted",
        due_date="2026-01-10",
        done_date=None,
        is_deleted=True,
    )
    db_session.add(deleted_row)
    db_session.commit()

    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:SOFT:DELETED",
        category="MGMT",
        title="active row",
        due=date(2026, 2, 1),
        owner=None,
        memo=None,
    )
    db_session.flush()
    db_session.refresh(deleted_row)

    assert row.docket_id != deleted_row.docket_id
    assert bool(deleted_row.is_deleted) is True


def test_mgmt_upsert_foreign_filing_status_red_forces_mgmt_work(
    db_session, sample_matter, monkeypatch
):
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:STATUS_RED:ForeignFilingDeadline",
        category="DEADLINE",
        title="Text",
        due=date(2026, 8, 19),
        owner=None,
        memo=None,
    )
    db_session.flush()

    assert (row.category or "").strip().upper() == "MGMT_WORK"


def test_mgmt_upsert_registration_status_red_forces_mgmt_work(
    db_session, sample_matter, monkeypatch
):
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:STATUS_RED:RegistrationDeadline",
        category="DEADLINE",
        title="Text",
        due=date(2026, 8, 19),
        owner=None,
        memo=None,
    )
    db_session.flush()

    assert (row.category or "").strip().upper() == "MGMT_WORK"


def test_mgmt_upsert_foreign_filing_notice_stays_mgmt(db_session, sample_matter, monkeypatch):
    from app.services.deadlines import mgmt_deadlines

    mid = _matter_id(sample_matter)
    monkeypatch.setattr(mgmt_deadlines, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    row = mgmt_deadlines._upsert_docket_item(
        matter_id=mid,
        name_ref="MGMT:FOREIGN_FILING_NOTICE_3M",
        category="MGMT",
        title="Text Text(3Text Text)",
        due=date(2026, 5, 19),
        owner=None,
        memo=None,
    )
    db_session.flush()

    assert (row.category or "").strip().upper() == "MGMT"


def test_docket_service_upsert_reopens_done_row(db_session, sample_matter, monkeypatch):
    from app.models.docket import DocketItem
    from app.services.deadlines import docket_service

    mid = _matter_id(sample_matter)
    existing = DocketItem(
        docket_id="b" * 32,
        matter_id=mid,
        category="FILING",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-01-10",
        done_date="2026-01-20",
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(docket_service, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    docket_service._upsert_single_docket(
        mid=mid,
        cat="FILING",
        ref="Text",
        title="Text Text",
        due="2026-02-05",
        owner_user_id=None,
    )
    db_session.flush()
    db_session.refresh(existing)

    assert existing.due_date == "2026-02-05"
    assert not (existing.done_date or "").strip()


def test_complete_exam_request_docket_closes_foreign_email_alias(
    db_session, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.services.deadlines import docket_service

    mid = _matter_id(sample_matter)
    canonical = DocketItem(
        docket_id="x" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Examination requestDeadline",
        name_free="Examination requestDeadline",
        due_date="2027-01-26",
        done_date=None,
    )
    alias = DocketItem(
        docket_id="y" * 32,
        matter_id=mid,
        category="MGMT",
        name_ref="exam_request",
        name_free="Request for substantive examination (statutory due date)",
        due_date="2027-01-26",
        done_date=None,
    )
    db_session.add_all([canonical, alias])
    db_session.commit()

    monkeypatch.setattr(docket_service, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    docket_service.complete_exam_request_docket(mid, "2026-05-11", commit=False)
    db_session.flush()
    db_session.refresh(canonical)
    db_session.refresh(alias)

    assert canonical.done_date == "2026-05-11"
    assert alias.done_date == "2026-05-11"


def test_docket_service_upsert_preserves_manual_abandoned_done_row(
    db_session, sample_matter, monkeypatch
):
    from app.models.docket import DocketItem
    from app.services.deadlines import docket_service

    mid = _matter_id(sample_matter)
    existing = DocketItem(
        docket_id="n" * 32,
        matter_id=mid,
        category="FILING",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-01-10",
        done_date="AUTO_CANCELLED:2026-01-20",
        memo=json.dumps(
            {
                "manual_abandoned": True,
                "manual_abandoned_at": "2026-01-20",
                "locked": True,
                "lock_reason": "manual_abandon",
            },
            ensure_ascii=False,
        ),
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr(docket_service, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    docket_service._upsert_single_docket(
        mid=mid,
        cat="FILING",
        ref="Text",
        title="Text Text",
        due="2026-02-05",
        owner_user_id=None,
    )
    db_session.flush()
    db_session.refresh(existing)

    assert existing.due_date == "2026-01-10"
    assert existing.done_date == "AUTO_CANCELLED:2026-01-20"


def test_docket_service_upsert_prefers_matching_open_due_row(
    db_session,
    sample_matter,
    monkeypatch,
):
    from app.models.docket import DocketItem
    from app.services.deadlines import docket_service

    mid = _matter_id(sample_matter)
    older = DocketItem(
        docket_id="e" * 32,
        matter_id=mid,
        category="FILING",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-03-01",
        done_date=None,
    )
    matching = DocketItem(
        docket_id="2" * 32,
        matter_id=mid,
        category="FILING",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-04-01",
        done_date=None,
    )
    db_session.add(older)
    db_session.add(matching)
    db_session.commit()

    monkeypatch.setattr(docket_service, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    docket_service._upsert_single_docket(
        mid=mid,
        cat="FILING",
        ref="Text",
        title="Text Text",
        due="2026-04-01",
        owner_user_id=None,
    )
    db_session.flush()
    db_session.refresh(older)
    db_session.refresh(matching)

    assert not (matching.done_date or "").strip()
    assert (older.done_date or "").startswith("AUTO_CANCELLED:")


def test_docket_service_upsert_ignores_soft_deleted_candidate(
    db_session,
    sample_matter,
    monkeypatch,
):
    from app.models.docket import DocketItem
    from app.services.deadlines import docket_service

    mid = _matter_id(sample_matter)
    deleted_row = DocketItem(
        docket_id="d" * 32,
        matter_id=mid,
        category="FILING",
        name_ref="Text",
        name_free="Text Text",
        due_date="2026-01-15",
        done_date=None,
        is_deleted=True,
    )
    db_session.add(deleted_row)
    db_session.commit()

    monkeypatch.setattr(docket_service, "enqueue_docket_sync_for_item", lambda **kwargs: None)

    docket_service._upsert_single_docket(
        mid=mid,
        cat="FILING",
        ref="Text",
        title="Text Text",
        due="2026-02-20",
        owner_user_id=None,
    )
    db_session.flush()
    db_session.refresh(deleted_row)

    active_rows = (
        DocketItem.query.filter_by(matter_id=mid, name_ref="Text")
        .filter(or_(DocketItem.is_deleted.is_(False), DocketItem.is_deleted.is_(None)))
        .all()
    )
    assert len(active_rows) == 1
    assert active_rows[0].docket_id != deleted_row.docket_id
    assert bool(deleted_row.is_deleted) is True
