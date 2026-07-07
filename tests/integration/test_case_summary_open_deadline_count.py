from __future__ import annotations

import uuid
from datetime import date, timedelta


def _matter_id(sample_matter) -> str:
    return str(getattr(sample_matter, "_test_matter_id", None) or sample_matter.matter_id)


def test_case_summary_open_deadline_count_includes_overdue(
    authenticated_client,
    db_session,
    sample_matter,
):
    from app.models.docket import DocketItem

    mid = _matter_id(sample_matter)
    overdue = (date.today() - timedelta(days=2)).isoformat()
    upcoming = (date.today() + timedelta(days=5)).isoformat()

    db_session.add(
        DocketItem(
            matter_id=mid,
            category="REMINDER",
            name_ref="SUMMARY OVERDUE",
            due_date=overdue,
            done_date=None,
        )
    )
    db_session.add(
        DocketItem(
            matter_id=mid,
            category="REMINDER",
            name_ref="SUMMARY UPCOMING",
            due_date=upcoming,
            done_date=None,
        )
    )
    db_session.add(
        DocketItem(
            matter_id=mid,
            category="REMINDER",
            name_ref="SUMMARY DONE",
            due_date=upcoming,
            done_date=date.today().isoformat(),
        )
    )
    db_session.commit()

    res = authenticated_client.get(f"/api/cases/{mid}/summary")
    assert res.status_code == 200
    body = res.get_json() or {}
    assert body.get("open_deadline_count") == 2
    assert (body.get("next_deadline") or {}).get("date") == upcoming
    assert (body.get("links") or {}).get("section_files") == f"/case/matter/{mid}/section/files"


def test_case_summary_annuity_status_red_uses_renewal_calendar(
    authenticated_client,
    db_session,
    sample_matter,
):
    from app.models.docket import DocketItem

    mid = _matter_id(sample_matter)
    due = (date.today() + timedelta(days=1)).isoformat()

    db_session.query(DocketItem).filter(DocketItem.matter_id == mid).delete()
    db_session.add(
        DocketItem(
            matter_id=mid,
            category="MGMT",
            name_ref="MGMT:STATUS_RED:4Text",
            name_free="4Text",
            due_date=due,
            done_date=None,
        )
    )
    db_session.commit()

    res = authenticated_client.get(f"/api/cases/{mid}/summary")
    assert res.status_code == 200
    body = res.get_json() or {}
    next_deadline = body.get("next_deadline") or {}
    assert next_deadline.get("label") == "4Text"
    assert next_deadline.get("calendar_url") == f"/renewal/calendar/month?date={due}"


def test_case_summary_exposes_public_etc_classification(admin_client, db_session):
    from app.models.ip_records import Matter, MatterCustomField

    matter = Matter(
        matter_id=uuid.uuid4().hex,
        our_ref=f"26TO{uuid.uuid4().hex[:4].upper()}WO",
        right_name="Madrid Summary Matter",
        right_group="OUT",
        matter_type="TRADEMARK",
        inhouse_status="Text",
    )
    db_session.add(matter)
    db_session.flush()
    db_session.add(
        MatterCustomField(
            matter_id=str(matter.matter_id),
            namespace="outgoing_trademark",
            data={"app_route": "Text"},
        )
    )
    db_session.commit()

    res = admin_client.get(f"/api/cases/{matter.matter_id}/summary")
    assert res.status_code == 200
    body = res.get_json() or {}
    assert body.get("division") == "OUT"
    assert body.get("type") == "TRADEMARK"
    assert body.get("display_division") == "ETC"
    assert body.get("display_type") == "MADRID"
