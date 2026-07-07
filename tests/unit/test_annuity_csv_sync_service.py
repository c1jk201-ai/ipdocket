from __future__ import annotations

from app.models.ip_records import AnnuityItem
from app.services.annuity.annuity_csv_sync_service import (
    sync_annuities_from_schedule_csv_for_matter,
)


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_csv_sync_marks_domestic_prepaid_cycles_as_paid(db_session, sample_matter, tmp_path):
    matter = db_session.merge(sample_matter)
    matter.right_group = "DOM"
    matter.matter_type = "PATENT"
    db_session.add(matter)
    db_session.commit()

    mid = _matter_id(sample_matter)
    csv_path = tmp_path / "annuity_schedule.csv"
    csv_path.write_text(
        "\n".join(
            [
                "matter_id,cycle_no,due_date,annuity_status",
                f"{mid},1,2026-01-01,pending",
                f"{mid},2,2027-01-01,pending",
                f"{mid},4,2029-01-01,pending",
            ]
        ),
        encoding="utf-8",
    )

    result = sync_annuities_from_schedule_csv_for_matter(
        mid,
        csv_path=csv_path,
        sync_workflows=False,
        commit=True,
    )

    assert result["matched_rows"] == 3
    rows = (
        AnnuityItem.query.filter_by(matter_id=mid)
        .order_by(AnnuityItem.cycle_no.asc(), AnnuityItem.annuity_id.asc())
        .all()
    )
    status_by_cycle = {int(r.cycle_no): (r.annuity_status or "").strip().lower() for r in rows}
    assert status_by_cycle[1] == "paid"
    assert status_by_cycle[2] == "paid"
    assert status_by_cycle[4] == "pending"


def test_csv_sync_restores_soft_deleted_matching_cycle(db_session, sample_matter, tmp_path):
    matter = db_session.merge(sample_matter)
    matter.right_group = "DOM"
    matter.matter_type = "PATENT"
    db_session.add(matter)
    db_session.commit()

    mid = _matter_id(sample_matter)
    existing = AnnuityItem(
        matter_id=mid,
        cycle_no=4,
        due_date="2029-01-01",
        is_deleted=True,
        delete_reason="manual delete",
    )
    db_session.add(existing)
    db_session.commit()
    existing_id = existing.annuity_id

    csv_path = tmp_path / "annuity_schedule.csv"
    csv_path.write_text(
        "\n".join(
            [
                "matter_id,cycle_no,due_date,annuity_status,official_fee",
                f"{mid},4,2029-02-01,pending,55000",
            ]
        ),
        encoding="utf-8",
    )

    result = sync_annuities_from_schedule_csv_for_matter(
        mid,
        csv_path=csv_path,
        sync_workflows=False,
        commit=True,
    )

    db_session.expire_all()
    row = AnnuityItem.query.filter_by(matter_id=mid, cycle_no=4).one()

    assert result["created"] == 0
    assert result["updated"] == 1
    assert row.annuity_id == existing_id
    assert row.is_deleted is False
    assert row.delete_reason is None
    assert row.due_date == "2029-02-01"
    assert row.official_fee == 55000
