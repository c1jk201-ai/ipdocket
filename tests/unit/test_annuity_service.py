from __future__ import annotations

import uuid
from datetime import date, datetime

from app.models.matter_facts import MatterFacts
from app.models.ip_records import AnnuityItem, Matter, MatterCustomField, MatterEvent
from app.models.system_config import SystemConfig
from app.services.annuity.annuity_service import (
    ensure_annuities_for_all_registered_matters,
    ensure_annuities_for_matter,
)


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_ensure_annuities_restores_soft_deleted_matching_cycle(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26PD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "PATENT"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2023-05-05")
    )
    db_session.add(
        AnnuityItem(
            matter_id=mid,
            cycle_no=4,
            due_date="2026-05-05",
            is_deleted=True,
            delete_reason="manual delete",
        )
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, start_year=4, end_year=4, commit=True)

    row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=4).one()

    assert changed == 1
    assert row.is_deleted is False
    assert row.delete_reason is None
    assert row.deleted_at is None
    assert row.deleted_by is None
    assert row.deleted_op_id is None
    assert row.due_date == "2026-05-05"


def test_ensure_annuities_keeps_user_deleted_cycle_hidden(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26PD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "PATENT"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2023-05-05")
    )
    db_session.add(
        AnnuityItem(
            matter_id=mid,
            cycle_no=4,
            due_date="2026-05-05",
            is_deleted=True,
            delete_reason="case_annuity_delete",
        )
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, start_year=4, end_year=4, commit=True)

    row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=4).one()

    assert changed == 0
    assert row.is_deleted is True
    assert row.delete_reason == "case_annuity_delete"
    assert row.due_date == "2026-05-05"


def test_ensure_annuities_skips_etc_hague_as_foreign(db_session, sample_matter, monkeypatch):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26DO{uuid.uuid4().hex[:4]}US"
    matter.right_group = "ETC"
    matter.matter_type = "HAGUE"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2023-05-05")
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.annuity.annuity_service._get_bool_config",
        lambda key, default=False: False if key == "ANNUITY_ALLOW_FOREIGN" else default,
    )

    changed = ensure_annuities_for_matter(mid, start_year=4, end_year=4, commit=True)

    assert changed == 0
    assert AnnuityItem.query.filter_by(matter_id=mid).count() == 0


def test_ensure_annuities_creates_trademark_renewal_item(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26TD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2025-03-13")
    )
    db_session.add(
        MatterEvent(matter_id=mid, event_key="TERM_EXPIRY_DATE", event_at="2035-03-13")
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, commit=True)

    section8_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=6).one()
    renewal_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=10).one()

    assert changed == 2
    assert AnnuityItem.query.filter_by(matter_id=mid, cycle_no=5).count() == 0
    assert section8_row.due_date == "2031-03-13"
    assert section8_row.extended_due_date == "2031-09-13"
    assert section8_row.renewal_open_date == "2030-03-13"
    assert renewal_row.due_date == "2035-03-13"
    assert renewal_row.extended_due_date == "2035-09-13"
    assert renewal_row.renewal_open_date == "2034-03-13"
    assert renewal_row.annuity_status == "pending"


def test_ensure_annuities_reconciles_stale_trademark_default_term_row(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26TD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2026-05-12")
    )
    db_session.add(
        MatterEvent(matter_id=mid, event_key="TERM_EXPIRY_DATE", event_at="2031-05-12")
    )
    db_session.add(
        AnnuityItem(
            matter_id=mid,
            cycle_no=1,
            due_date="2026-05-12",
            memo='[Text] {"auto": true, "term_expiry_date": "2031-05-12"}',
        )
    )
    db_session.add(
        AnnuityItem(
            matter_id=mid,
            cycle_no=10,
            due_date="2036-05-12",
            extended_due_date="2036-11-12",
            memo='[Text] {"auto": true, "term_expiry_date": "2036-05-12"}',
        )
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, commit=True)

    section8_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=6).one()
    cycle_one = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=1).one()
    renewal_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=10).one()

    assert changed == 3
    assert AnnuityItem.query.filter_by(matter_id=mid, cycle_no=5).count() == 0
    assert section8_row.due_date == "2032-05-12"
    assert section8_row.extended_due_date == "2032-11-12"
    assert section8_row.is_deleted is False
    assert cycle_one.is_deleted is True
    assert cycle_one.delete_reason == "auto_reconcile:trademark_term_expiry_date"
    assert renewal_row.is_deleted is False
    assert renewal_row.due_date == "2036-05-12"


def test_ensure_annuities_uses_uspto_trademark_registration_fee_paid_fallback(
    db_session, sample_matter
):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26TD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(
            matter_id=mid,
            event_key="REGISTRATION_FEE_PAID",
            event_at="2026-05-15",
            event_date=date(2026, 5, 15),
        )
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, commit=True)

    section8_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=6).one()
    renewal_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=10).one()
    facts = MatterFacts.query.get(mid)

    assert changed == 2
    assert section8_row.due_date == "2032-05-15"
    assert section8_row.extended_due_date == "2032-11-15"
    assert renewal_row.due_date == "2036-05-15"
    assert renewal_row.extended_due_date == "2036-11-15"
    assert facts.registration_date == date(2026, 5, 15)
    assert facts.registration_date_source == "reg_fee_paid_date_fallback"


def test_ensure_annuities_creates_foreign_trademark_split_payment_item_for_5year_plan(
    db_session, sample_matter, monkeypatch
):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26TO{uuid.uuid4().hex[:4]}JP"
    matter.right_group = "OUT"
    matter.matter_type = "TRADEMARK"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2025-03-13")
    )
    db_session.add(
        MatterEvent(matter_id=mid, event_key="TERM_EXPIRY_DATE", event_at="2035-03-13")
    )
    db_session.add(
        MatterCustomField(
            matter_id=mid,
            namespace="outgoing_trademark",
            data={"tm_registration_payment_term": "5Text"},
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.annuity.annuity_service._get_bool_config",
        lambda key, default=False: True if key == "ANNUITY_ALLOW_FOREIGN" else default,
    )

    changed = ensure_annuities_for_matter(mid, commit=True)

    split_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=5).one()
    renewal_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=10).one()

    assert changed == 2
    assert split_row.due_date == "2030-03-13"
    assert split_row.extended_due_date in (None, "")
    assert split_row.renewal_open_date == "2029-09-14"
    assert split_row.renewal_notice_due == "2029-12-13"
    assert renewal_row.due_date == "2035-03-13"
    assert renewal_row.extended_due_date == "2035-09-13"


def test_ensure_annuities_prefers_current_term_expiry_status_red_date(db_session, sample_matter):
    matter = db_session.merge(sample_matter)
    matter.our_ref = f"26TD{uuid.uuid4().hex[:4]}US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    matter.status_red = "Term expired"
    matter.status_red_related_date = "2035-04-01"
    db_session.add(matter)

    mid = _matter_id(sample_matter)
    db_session.add(
        MatterEvent(matter_id=mid, event_key="REGISTRATION_DATE", event_at="2025-04-01")
    )
    db_session.add(
        MatterEvent(matter_id=mid, event_key="TERM_EXPIRY_DATE", event_at="2030-04-01")
    )
    db_session.commit()

    changed = ensure_annuities_for_matter(mid, commit=True)

    section8_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=6).one()
    renewal_row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=10).one()

    assert changed == 2
    assert section8_row.due_date == "2031-04-01"
    assert renewal_row.due_date == "2035-04-01"


def test_all_registered_matters_includes_missing_trademark_despite_watermark(
    db_session, monkeypatch
):
    import app.services.annuity.annuity_service as annuity_service

    watermark = datetime(2026, 4, 26, 20, 30)
    missing_tm = f"tm-{uuid.uuid4().hex}"
    old_patent_with_row = f"pt-{uuid.uuid4().hex}"
    old_tm_soft_deleted = f"tm-del-{uuid.uuid4().hex}"
    updated_design = f"ds-{uuid.uuid4().hex}"
    db_session.add_all(
        [
            Matter(
                matter_id=missing_tm,
                our_ref=f"26TD{uuid.uuid4().hex[:6]}US",
                right_group="DOM",
                matter_type="TRADEMARK",
            ),
            Matter(
                matter_id=old_patent_with_row,
                our_ref=f"26PD{uuid.uuid4().hex[:6]}US",
                right_group="DOM",
                matter_type="PATENT",
            ),
            Matter(
                matter_id=old_tm_soft_deleted,
                our_ref=f"26TD{uuid.uuid4().hex[:6]}US",
                right_group="DOM",
                matter_type="TRADEMARK",
            ),
            Matter(
                matter_id=updated_design,
                our_ref=f"26DD{uuid.uuid4().hex[:6]}US",
                right_group="DOM",
                matter_type="DESIGN",
            ),
            MatterFacts(
                matter_id=missing_tm,
                registration_date=date(2025, 3, 13),
                right_type_norm="TRADEMARK",
                updated_at=datetime(2026, 1, 1),
            ),
            MatterFacts(
                matter_id=old_patent_with_row,
                registration_date=date(2025, 3, 13),
                right_type_norm="PATENT",
                updated_at=datetime(2026, 1, 1),
            ),
            MatterFacts(
                matter_id=old_tm_soft_deleted,
                registration_date=date(2025, 3, 13),
                right_type_norm="TRADEMARK",
                updated_at=datetime(2026, 1, 1),
            ),
            MatterFacts(
                matter_id=updated_design,
                registration_date=date(2025, 3, 13),
                right_type_norm="DESIGN",
                updated_at=datetime(2026, 4, 27),
            ),
            AnnuityItem(
                matter_id=old_patent_with_row,
                cycle_no=4,
                due_date="2028-03-13",
            ),
            AnnuityItem(
                matter_id=old_tm_soft_deleted,
                cycle_no=10,
                due_date="2035-03-13",
                is_deleted=True,
                delete_reason="renewal_fee_delete",
            ),
        ]
    )
    SystemConfig.set_config("ANNUITY_AUTOGEN_WATERMARK", watermark.isoformat())
    db_session.commit()

    called: list[str] = []
    monkeypatch.setattr(
        annuity_service,
        "_backfill_matter_facts_for_annuities",
        lambda limit=0: None,
    )
    monkeypatch.setattr(
        annuity_service,
        "ensure_annuities_for_matter",
        lambda mid, **kwargs: called.append(str(mid)) or 0,
    )

    processed, created = ensure_annuities_for_all_registered_matters(commit=False)

    assert processed >= 2
    assert created == 0
    assert missing_tm in called
    assert updated_design in called
    assert old_patent_with_row not in called
    assert old_tm_soft_deleted not in called


def test_all_registered_matters_does_not_advance_watermark_after_failure(db_session, monkeypatch):
    import app.services.annuity.annuity_service as annuity_service

    old_watermark = datetime(2026, 4, 26, 20, 30).isoformat()
    matter_id = f"fail-{uuid.uuid4().hex}"
    db_session.add_all(
        [
            Matter(
                matter_id=matter_id,
                our_ref=f"26PD{uuid.uuid4().hex[:6]}US",
                right_group="DOM",
                matter_type="PATENT",
            ),
            MatterFacts(
                matter_id=matter_id,
                registration_date=date(2025, 3, 13),
                right_type_norm="PATENT",
                updated_at=datetime(2026, 4, 27),
            ),
        ]
    )
    SystemConfig.set_config("ANNUITY_AUTOGEN_WATERMARK", old_watermark)
    db_session.commit()

    def fail_ensure(mid, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        annuity_service,
        "_backfill_matter_facts_for_annuities",
        lambda limit=0: None,
    )
    monkeypatch.setattr(annuity_service, "ensure_annuities_for_matter", fail_ensure)

    processed, created = ensure_annuities_for_all_registered_matters(commit=True)

    assert processed == 0
    assert created == 0
    assert SystemConfig.get_config("ANNUITY_AUTOGEN_WATERMARK") == old_watermark
