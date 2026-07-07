from __future__ import annotations

from datetime import date


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_domestic_trademark_term_expiry_docket_is_closed_for_renewal_management(
    db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

    matter = db_session.merge(sample_matter)
    mid = _matter_id(sample_matter)
    matter.our_ref = "26TD0001US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    matter.status_red = "Term expired"
    matter.status_red_related_date = "2035-03-13"
    db_session.add(matter)

    docket = DocketItem(
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Term expired",
        name_free="Term expired",
        due_date="2035-03-13",
        memo='{"auto": true, "trigger": "status_red"}',
    )
    db_session.add(docket)
    db_session.commit()

    ensure_mgmt_deadlines_for_matter(mid, commit=True)

    row = DocketItem.query.get(docket.docket_id)
    assert row is not None
    assert row.done_date == date.today().isoformat()


def test_domestic_trademark_term_expiry_name_free_variant_is_closed_for_renewal_management(
    db_session, sample_matter
):
    from app.models.docket import DocketItem
    from app.services.deadlines.mgmt_deadlines import ensure_mgmt_deadlines_for_matter

    matter = db_session.merge(sample_matter)
    mid = _matter_id(sample_matter)
    matter.our_ref = "26TD0002US"
    matter.right_group = "DOM"
    matter.matter_type = "TRADEMARK"
    matter.status_red = "Term expired"
    matter.status_red_related_date = "2023-08-14"
    db_session.add(matter)

    docket = DocketItem(
        matter_id=mid,
        category="MGMT",
        name_ref="MGMT:STATUS_RED:Term expired",
        name_free="Term expired",
        due_date="2035-03-13",
        memo='{"auto": true, "trigger": "status_red"}',
    )
    db_session.add(docket)
    db_session.commit()

    ensure_mgmt_deadlines_for_matter(mid, commit=True)

    row = DocketItem.query.get(docket.docket_id)
    assert row is not None
    assert row.done_date == date.today().isoformat()
