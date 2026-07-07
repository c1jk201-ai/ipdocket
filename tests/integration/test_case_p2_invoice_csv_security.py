from __future__ import annotations

import uuid


def test_tc_to_invoice_csv_filters_worklogs_by_matter(admin_client, db_session, sample_matter):
    from app.models.ip_records import Matter
    from app.models.worklog import WorkLog

    matter_id = getattr(sample_matter, "_test_matter_id", str(sample_matter.matter_id))
    other_matter_id = uuid.uuid4().hex

    db_session.add(
        Matter(
            matter_id=other_matter_id,
            our_ref=f"TEST-{uuid.uuid4().hex[:8]}",
            right_name="Text Text",
            status_red="Text",
            status_blue="Text",
        )
    )
    db_session.flush()

    allowed = WorkLog(matter_id=matter_id, task_name="allowed")
    blocked = WorkLog(matter_id=other_matter_id, task_name="blocked")
    db_session.add_all([allowed, blocked])
    db_session.commit()

    response = admin_client.get(
        f"/case/matter/{matter_id}/tc/to-invoice.csvNewids={allowed.id},{blocked.id}"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "worklog_id,description,qty,unit_price,amount" in body
    assert f"{allowed.id}," in body
    assert f"{blocked.id}," not in body
